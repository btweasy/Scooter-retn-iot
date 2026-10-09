'use strict';
/*
 * Panel operatora floty hulajnóg.
 *
 * Dane na żywo przychodzą strumieniem Server-Sent Events (/api/stream) – co 2 s cały "zrzut" stanu:
 * pojazdy, statystyki, wypożyczenia, rezerwacje, historia i alerty. Funkcja render() rysuje go na mapie
 * i w panelu bocznym. Akcje (rezerwuj, wypożycz, pauza, zakończ...) wysyłamy zwykłymi żądaniami POST.
 */

// ================================================================ konfiguracja

const STATUS = {
  available:   { color: '#1B9E77', label: 'Dostępna' },
  reserved:    { color: '#9C36B5', label: 'Zarezerwowana' },
  in_ride:     { color: '#2F6FDE', label: 'W jeździe' },
  paused:      { color: '#0C8599', label: 'Pauza' },
  low_battery: { color: '#E8A317', label: 'Niski akumulator' },
  offline:     { color: '#8A98A0', label: 'Offline' },
};

const ALERT_COLORS = {
  geofence: '#D6336C', low_battery: '#E8A317', unauthorized: '#7048E8', rental_closed: '#5B6B72',
};

const CITY_VIEWS = {
  'Słupsk':      { center: [54.4641, 17.0285], zoom: 14 },
  'Gdańsk':      { center: [54.3485, 18.6530], zoom: 14 },
  'Warszawa':    { center: [52.2497, 21.0122], zoom: 14 },
  'Cała Polska': { center: [53.3, 18.9], zoom: 6 },
};

const ROUTE_COLOR = '#2F6FDE';

// ================================================================ stan panelu

const state = {
  scooters: new Map(),       // id -> dane pojazdu z ostatniego zrzutu
  rentals: [],               // trwające wypożyczenia
  reservations: [],          // aktywne rezerwacje
  history: [],               // ostatnie zakończone wypożyczenia
  selectedRentalId: null,    // wypożyczenie pokazane w karcie i na mapie
  selectedRental: null,      // jego szczegóły (z trasą) z /api/rentals/{id}
  endBlockedRentalId: null,  // wypożyczenie, którego nie dało się zakończyć w strefie zakazu
};

const markers = new Map();   // id pojazdu -> znacznik Leaflet
let routeLine = null;
let startMarker = null;
let loadingRental = false;

// ================================================================ pomocnicze

const $ = (id) => document.getElementById(id);

function escapeHtml(text) {
  const element = document.createElement('div');
  element.textContent = String(text);
  return element.innerHTML;
}

function formatNumber(value, digits) {
  return value.toLocaleString('pl-PL', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
const formatKm = (km) => formatNumber(km, 2) + ' km';
const formatMoney = (pln) => formatNumber(pln, 2) + ' zł';
const formatClock = (iso) => new Date(iso).toLocaleTimeString('pl-PL', { hour: '2-digit', minute: '2-digit' });

/** 75 -> "1:15", 3725 -> "1:02:05" */
function formatDuration(totalSeconds) {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = String(seconds % 60).padStart(2, '0');
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, '0')}:${rest}`;
  return `${minutes}:${rest}`;
}

const secondsSince = (iso) => (Date.now() - Date.parse(iso)) / 1000;

function userLabel(userId) {
  return userId.startsWith('sim-') ? 'emulowany użytkownik' : userId;
}

function currentUser() {
  return $('user-id').value.trim() || 'demo-user';
}

function toast(message) {
  $('toast').textContent = message;
}

function batteryBar(battery) {
  const color = battery < 15 ? '#E8A317' : '#1B9E77';
  return `<div class="bar"><i style="width:${battery}%;background:${color}"></i></div>`;
}

// ================================================================ komunikacja z API

class ApiError extends Error {
  constructor(message, code) {
    super(message);
    this.code = code;   // np. "no_parking_zone" – pozwala zareagować na konkretny błąd
  }
}

// token API (gdy backend ma ustawione API_TOKEN) – trzymany tylko w tej przeglądarce
const tokenStore = {
  get() { try { return localStorage.getItem('apiToken') || ''; } catch (e) { return ''; } },
  set(value) { try { localStorage.setItem('apiToken', value); } catch (e) { /* tryb prywatny */ } },
};

async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

async function sendJson(url, body = null, method = 'POST') {
  const headers = { 'Content-Type': 'application/json' };
  const token = tokenStore.get();
  if (token) headers.Authorization = 'Bearer ' + token;

  const response = await fetch(url, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) {
    $('auth').hidden = false;
    $('token').focus();
    throw new ApiError('wymagany token API', 'unauthorized');
  }
  if (!response.ok) throw new ApiError(data.detail || `HTTP ${response.status}`, data.code);
  return data;
}

$('auth').addEventListener('submit', (event) => {
  event.preventDefault();
  tokenStore.set($('token').value.trim());
  $('auth').hidden = true;
  toast('Token zapisany – spróbuj ponownie');
});

// ================================================================ mapa

const map = L.map('map').setView(CITY_VIEWS['Słupsk'].center, CITY_VIEWS['Słupsk'].zoom);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19, attribution: '&copy; OpenStreetMap',
}).addTo(map);

getJson('/api/zones').then((zones) => {
  for (const zone of zones) {
    L.polygon(zone.polygon, { color: '#D6336C', weight: 2, fillOpacity: 0.15 }).bindTooltip(zone.name).addTo(map);
  }
});

function createCityButtons() {
  for (const [name, view] of Object.entries(CITY_VIEWS)) {
    const button = document.createElement('button');
    button.textContent = name;
    button.addEventListener('click', () => {
      map.setView(view.center, view.zoom);
      for (const other of $('cities').children) other.classList.toggle('on', other === button);
    });
    $('cities').appendChild(button);
  }
  $('cities').firstChild.classList.add('on');
}

function createLegend() {
  $('legend').innerHTML = Object.entries(STATUS).map(([status, info]) =>
    `<div><i class="dot" style="background:${info.color}"></i><b id="count-${status}">0</b><span>${info.label}</span></div>`
  ).join('');
}

function updateMarker(scooter) {
  let marker = markers.get(scooter.id);
  if (!marker) {
    marker = L.circleMarker([scooter.lat, scooter.lon], { radius: 7, color: '#fff', weight: 1.5, fillOpacity: 0.95 });
    marker.bindPopup(() => popupHtml(scooter.id));
    marker.addTo(map);
    markers.set(scooter.id, marker);
  }
  marker.setLatLng([scooter.lat, scooter.lon]);

  // obwódka i pulsowanie: pojazd ma wypożyczenie lub rezerwację; wybrany jest większy
  const booked = ['in_ride', 'paused', 'reserved'].includes(scooter.status) && hasBooking(scooter.id);
  const selected = state.selectedRental && state.selectedRental.status !== 'finished'
    && state.selectedRental.scooter_id === scooter.id;
  marker.setStyle({
    fillColor: STATUS[scooter.status].color,
    color: booked ? '#14262B' : '#fff',
    weight: booked ? 3 : 1.5,
  });
  marker.setRadius(selected ? 11 : booked ? 9 : 7);
  const element = marker.getElement();
  if (element) element.classList.toggle('booked', booked);
}

function hasBooking(scooterId) {
  return Boolean(rentalOf(scooterId) || reservationOf(scooterId));
}
const rentalOf = (scooterId) => state.rentals.find((rental) => rental.scooter_id === scooterId);
const reservationOf = (scooterId) => state.reservations.find((reservation) => reservation.scooter_id === scooterId);

// ================================================================ dymek pojazdu (popup)

function popupHtml(scooterId) {
  const scooter = state.scooters.get(scooterId);
  if (!scooter) return '';
  const rental = rentalOf(scooterId);
  const reservation = reservationOf(scooterId);

  let note = '';
  let buttons = '';
  if (rental) {
    note = `Klient: ${escapeHtml(userLabel(rental.user_id))}`;
    if (rental.status === 'active' && scooter.status !== 'in_ride') note += ' · czeka na odblokowanie';
    buttons = actionButton('select', rental.id, 'Pokaż trasę')
      + (rental.status === 'paused' ? actionButton('resume', rental.id, 'Wznów jazdę') : actionButton('pause', rental.id, 'Pauza'))
      + actionButton('end', rental.id, 'Zakończ jazdę', 'primary');
  } else if (reservation) {
    note = `Rezerwacja: ${escapeHtml(userLabel(reservation.user_id))} do ${formatClock(reservation.expires_at)}`;
    buttons = actionButton('start', scooterId, `Wypożycz jako ${escapeHtml(currentUser())}`, 'primary')
      + actionButton('cancelReservation', reservation.id, 'Anuluj rezerwację');
  } else if (scooter.status === 'available') {
    buttons = actionButton('start', scooterId, 'Wypożycz', 'primary') + actionButton('reserve', scooterId, 'Zarezerwuj');
  }

  return `<div class="popup">
    <b>${escapeHtml(scooter.id)}</b> · ${escapeHtml(scooter.city)}<br>
    ${STATUS[scooter.status].label} · ${scooter.speed} km/h<br>
    Bateria ${scooter.battery}% · zasięg ok. ${formatNumber(scooter.range_km, 0)} km
    ${batteryBar(scooter.battery)}
    ${note ? `<div class="note">${note}</div>` : ''}
    ${buttons}
  </div>`;
}

function actionButton(action, id, label, style = '') {
  return `<button class="${style}" data-action="${action}" data-id="${escapeHtml(id)}">${label}</button>`;
}

// ================================================================ akcje operatora

const ACTIONS = {
  async reserve(scooterId) {
    const reservation = await sendJson('/api/reservations', { scooter_id: scooterId, user_id: currentUser() });
    toast(`Zarezerwowano ${scooterId} do ${formatClock(reservation.expires_at)}`);
  },
  async cancelReservation(reservationId) {
    await sendJson(`/api/reservations/${reservationId}`, null, 'DELETE');
    toast('Rezerwacja anulowana');
  },
  async start(scooterId) {
    const rental = await sendJson('/api/rentals/start', { scooter_id: scooterId, user_id: currentUser() });
    toast(`Wypożyczono ${rental.scooter_id}`);
    selectRental(rental.id);
  },
  async pause(rentalId) {
    await sendJson(`/api/rentals/${rentalId}/pause`);
    toast('Jazda wstrzymana – hulajnoga zablokowana, tańsza stawka');
  },
  async resume(rentalId) {
    await sendJson(`/api/rentals/${rentalId}/resume`);
    toast('Jazda wznowiona');
  },
  async end(rentalId) {
    await endRental(rentalId, false);
  },
  async endForce(rentalId) {
    await endRental(rentalId, true);
  },
  async select(rentalId) {
    selectRental(rentalId);
  },
  async ack(alertId) {
    await sendJson(`/api/alerts/${alertId}/ack`);
  },
};

async function endRental(rentalId, force) {
  try {
    const rental = await sendJson(`/api/rentals/${rentalId}/end`, { force });
    state.endBlockedRentalId = null;
    toast(`Zakończono: ${formatKm(rental.distance_km)}, ${formatMoney(rental.cost_pln)}`);
    selectRental(rental.id);   // pokaż paragon i całą trasę
  } catch (error) {
    if (error.code === 'no_parking_zone') {
      // jak w aplikacjach Bolt/Lime: w strefie zakazu nie da się zakończyć; operator może wymusić
      state.endBlockedRentalId = rentalId;
      selectRental(rentalId);
    }
    throw error;
  }
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-action]');
  if (!button) return;
  const action = ACTIONS[button.dataset.action];
  const id = button.dataset.id;
  const numericId = /^\d+$/.test(id) ? Number(id) : id;   // id wypożyczeń są liczbami, pojazdów – tekstem
  try {
    await action(numericId);
    map.closePopup();
    refresh();
  } catch (error) {
    toast('Błąd: ' + error.message);
  }
});

// ================================================================ karta wypożyczenia + trasa

function selectRental(rentalId) {
  if (state.selectedRentalId !== rentalId) {
    removeRoute();
    state.selectedRental = null;
  }
  state.selectedRentalId = rentalId;
  $('ride').hidden = false;
  loadSelectedRental(true);
  renderLists();
}

function closeRentalCard() {
  state.selectedRentalId = null;
  state.selectedRental = null;
  removeRoute();
  $('ride').hidden = true;
  renderLists();
  renderMarkers();
}

async function loadSelectedRental(zoomToRoute) {
  if (state.selectedRentalId === null || loadingRental) return;
  loadingRental = true;
  const rentalId = state.selectedRentalId;
  try {
    const rental = await getJson(`/api/rentals/${rentalId}`);
    if (state.selectedRentalId !== rentalId) return;   // w międzyczasie wybrano inne
    state.selectedRental = rental;
    drawRoute(zoomToRoute);
    renderRentalCard();
    renderMarkers();
  } catch (error) {
    toast('Nie udało się pobrać trasy');
  } finally {
    loadingRental = false;
  }
}

function drawRoute(zoomToRoute) {
  const rental = state.selectedRental;
  const scooter = state.scooters.get(rental.scooter_id);
  const points = rental.track.slice();
  const ongoing = rental.status !== 'finished';
  if (ongoing && scooter) points.push([scooter.lat, scooter.lon]);   // linia kończy się przy znaczniku

  if (!routeLine) {
    routeLine = L.polyline(points, { weight: 5, opacity: 0.85, lineJoin: 'round', lineCap: 'round', interactive: false });
    routeLine.addTo(map);
  } else {
    routeLine.setLatLngs(points);
  }
  // trwająca jazda – ciągła niebieska linia, zakończona – szara przerywana
  routeLine.setStyle({ color: ongoing ? ROUTE_COLOR : '#5B6B72', dashArray: ongoing ? null : '6 8' });

  if (points.length > 0 && !startMarker) {
    startMarker = L.circleMarker(points[0], { radius: 6, color: '#14262B', weight: 2.5, fillColor: '#fff', fillOpacity: 1 });
    startMarker.bindTooltip(`Start ${formatClock(rental.started_at)}`).addTo(map);
  }
  const marker = markers.get(rental.scooter_id);
  if (marker) marker.bringToFront();

  if (zoomToRoute) {
    if (points.length > 1) map.fitBounds(routeLine.getBounds(), { padding: [60, 60], maxZoom: 17 });
    else if (scooter) map.setView([scooter.lat, scooter.lon], 16);
  }
}

function removeRoute() {
  if (routeLine) { routeLine.remove(); routeLine = null; }
  if (startMarker) { startMarker.remove(); startMarker = null; }
}

const RENTAL_STATUS_LABEL = { active: 'w trakcie', paused: 'pauza', finished: 'zakończone' };

function renderRentalCard() {
  const rental = state.selectedRental;
  if (!rental) return;
  const scooter = state.scooters.get(rental.scooter_id);
  const finished = rental.status === 'finished';

  $('ride').className = rental.status;
  $('ride-title').textContent = `${rental.scooter_id} · #${rental.id}`;
  const period = finished ? `${formatClock(rental.started_at)}–${formatClock(rental.ended_at)}` : `od ${formatClock(rental.started_at)}`;
  const automatic = rental.closed_automatically ? ' · zamknięte automatycznie' : '';
  $('ride-sub').innerHTML = `<span class="badge ${rental.status}">${RENTAL_STATUS_LABEL[rental.status]}</span> `
    + `${escapeHtml(userLabel(rental.user_id))} · ${period}${automatic}`;

  $('ride-distance').textContent = formatKm(rental.distance_km);
  $('ride-cost').textContent = formatMoney(rental.receipt.total);
  updateRideClock();

  $('ride-live').innerHTML = !finished && scooter
    ? `${STATUS[scooter.status].label} · ${scooter.speed} km/h · bateria ${scooter.battery}% (ok. ${formatNumber(scooter.range_km, 0)} km)${batteryBar(scooter.battery)}`
    : '';
  $('ride-receipt').innerHTML = receiptHtml(rental.receipt, finished);
  $('ride-actions').innerHTML = rentalCardButtons(rental);
}

function receiptHtml(receipt, finished) {
  const title = finished ? 'Paragon' : 'Gdyby zakończyć teraz';
  let rows = `<tr><td colspan="2" class="muted">${title}</td></tr>`
    + `<tr><td>Odblokowanie</td><td>${formatMoney(receipt.unlock_fee)}</td></tr>`
    + `<tr><td>Jazda: ${receipt.ride_minutes} min</td><td>${formatMoney(receipt.ride_cost)}</td></tr>`;
  if (receipt.pause_minutes > 0) {
    rows += `<tr><td>Pauza: ${receipt.pause_minutes} min</td><td>${formatMoney(receipt.pause_cost)}</td></tr>`;
  }
  rows += `<tr class="total"><td>Razem</td><td>${formatMoney(receipt.total)}</td></tr>`;
  return rows;
}

function rentalCardButtons(rental) {
  let buttons = actionButton('select', rental.id, 'Pokaż trasę');
  if (rental.status === 'active') buttons += actionButton('pause', rental.id, 'Pauza');
  if (rental.status === 'paused') buttons += actionButton('resume', rental.id, 'Wznów jazdę');
  if (rental.status !== 'finished') {
    buttons += actionButton('end', rental.id, 'Zakończ jazdę', 'primary');
    if (state.endBlockedRentalId === rental.id) {
      buttons += actionButton('endForce', rental.id, 'Zakończ mimo strefy (operator)', 'warn');
    }
  }
  return buttons;
}

/** Zegar jazdy odświeżany co sekundę, bez czekania na serwer. */
function updateRideClock() {
  const rental = state.selectedRental;
  if (rental) {
    const seconds = rental.status === 'finished' ? rental.elapsed_s : secondsSince(rental.started_at);
    $('ride-time').textContent = formatDuration(seconds);
  }
  for (const element of document.querySelectorAll('time[data-since]')) {
    element.textContent = formatDuration(secondsSince(element.dataset.since));
  }
  for (const element of document.querySelectorAll('time[data-until]')) {
    element.textContent = formatDuration(-secondsSince(element.dataset.until));
  }
}

$('ride-close').addEventListener('click', closeRentalCard);

// ================================================================ listy w panelu bocznym

function renderLists() {
  renderRentalList();
  renderReservationList();
  renderHistoryList();
  updateRideClock();
}

function rentalRow(rental, rightSide) {
  const selected = rental.id === state.selectedRentalId ? 'selected' : '';
  const paused = rental.status === 'paused' ? ' <span class="badge paused">pauza</span>' : '';
  return `<li><button data-action="select" data-id="${rental.id}" class="${selected}">
    <i class="ring"></i><b>${escapeHtml(rental.scooter_id)}</b>${paused}
    <span class="who">${escapeHtml(userLabel(rental.user_id))}</span>${rightSide}
  </button></li>`;
}

function renderRentalList() {
  // najpierw klienci "prawdziwi", potem emulowani; w grupie – najnowsze na górze
  const isSimulated = (rental) => rental.user_id.startsWith('sim-');
  const sorted = state.rentals.slice().sort((a, b) => (isSimulated(a) - isSimulated(b)) || (b.id - a.id));
  $('rentals-count').textContent = sorted.length ? `(${sorted.length})` : '';
  $('rentals').innerHTML = sorted.length
    ? sorted.map((rental) => rentalRow(rental, `<time data-since="${rental.started_at}"></time>`)).join('')
    : '<li class="empty">Brak aktywnych wypożyczeń</li>';
}

function renderReservationList() {
  const reservations = state.reservations;
  $('reservations-count').textContent = reservations.length ? `(${reservations.length})` : '';
  $('reservations').innerHTML = reservations.length
    ? reservations.map((reservation) => `<li class="row">
        <i class="dot" style="background:${STATUS.reserved.color}"></i><b>${escapeHtml(reservation.scooter_id)}</b>
        <span class="who">${escapeHtml(userLabel(reservation.user_id))} · zostało</span>
        <time data-until="${reservation.expires_at}"></time>
        <button data-action="cancelReservation" data-id="${reservation.id}" title="Anuluj rezerwację" aria-label="Anuluj rezerwację">×</button>
      </li>`).join('')
    : '<li class="empty">Brak rezerwacji</li>';
}

function renderHistoryList() {
  $('history').innerHTML = state.history.length
    ? state.history.map((rental) => rentalRow(rental, `<span class="amount">${formatMoney(rental.cost_pln)}</span>`)).join('')
    : '<li class="empty">Brak zakończonych wypożyczeń</li>';
}

function renderAlerts(alerts) {
  $('alerts').innerHTML = alerts.length
    ? alerts.map((alert) => `<li>
        <i class="dot" style="background:${ALERT_COLORS[alert.kind] || '#8A98A0'};margin-top:5px"></i>
        <span>${escapeHtml(alert.message)}<br><time>${new Date(alert.ts).toLocaleTimeString('pl-PL')}</time></span>
        <button data-action="ack" data-id="${alert.id}" title="Potwierdź alert">OK</button>
      </li>`).join('')
    : '<li class="empty">Brak otwartych alertów</li>';
}

function renderStats(stats) {
  for (const status of Object.keys(STATUS)) $(`count-${status}`).textContent = stats.by_status[status] ?? 0;
  $('meta').textContent = `Pojazdów: ${stats.total} · średnia bateria ${stats.avg_battery}% · `
    + `aktywne wypożyczenia: ${stats.active_rentals} · ${stats.messages_per_s} wiad./s`;
}

function renderMarkers() {
  for (const scooter of state.scooters.values()) updateMarker(scooter);
}

// ================================================================ odświeżanie

function render(snapshot) {
  $('connection').textContent = '';
  state.rentals = snapshot.rentals;
  state.reservations = snapshot.reservations;
  state.history = snapshot.history;
  for (const scooter of snapshot.scooters) state.scooters.set(scooter.id, scooter);

  renderMarkers();
  renderStats(snapshot.stats);
  renderAlerts(snapshot.alerts);
  renderLists();

  // wybrane wypożyczenie: trwające – dociągamy trasę; zakończone – już się nie zmienia
  if (state.selectedRentalId !== null) {
    if (state.selectedRental && state.selectedRental.status === 'finished') renderRentalCard();
    else loadSelectedRental(false);
  }
}

function showDisconnected() {
  $('connection').textContent = '· brak połączenia z serwerem';
}

function refresh() {
  getJson('/api/snapshot').then(render).catch(showDisconnected);
}

createCityButtons();
createLegend();
refresh();
setInterval(updateRideClock, 1000);

// jeden strumień SSE zamiast odpytywania wielu endpointów; EventSource sam wznawia połączenie
const events = new EventSource('/api/stream');
events.onmessage = (event) => render(JSON.parse(event.data));
events.onerror = showDisconnected;
