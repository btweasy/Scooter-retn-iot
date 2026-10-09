'use strict';
/*
 * Aplikacja klienta "Hulaj" (PWA) – to, co widzi osoba wypożyczająca hulajnogę.
 *
 * Kolejność ekranów przy uruchomieniu:
 *   1. ekran startowy (logo, ładowanie cennika),
 *   2. wprowadzenie – 3 slajdy, tylko przy pierwszym uruchomieniu,
 *   3. "logowanie" – w prototypie tylko imię (z niego powstaje user_id),
 *   4. mapa + karta na dole. Karta ma 5 widoków, wybieranych przez currentView():
 *        browse   – najbliższe wolne hulajnogi,
 *        scooter  – wybrana hulajnoga: Zarezerwuj / Odblokuj,
 *        reserved – trwająca rezerwacja z odliczaniem,
 *        ride     – trwająca jazda: czas, dystans, koszt, Pauza, Zakończ,
 *        summary  – paragon po zakończeniu (albo przejazd z historii).
 *
 * Dane na żywo: ten sam strumień SSE (/api/stream) co panel operatora. Z całego zrzutu
 * aplikacja wybiera tylko to, co dotyczy zalogowanej osoby (jej rezerwację i wypożyczenie).
 */

// ================================================================ konfiguracja

const CITIES = {
  'Słupsk':   [54.4641, 17.0285],
  'Gdańsk':   [54.3485, 18.6530],
  'Warszawa': [52.2497, 21.0122],
};
const SPLASH_MIN_MS = 1200;          // ekran startowy widać co najmniej tyle (żeby nie "mignął")
const WALKING_M_PER_MIN = 80;        // ok. 5 km/h
const SERVICE_RADIUS_KM = 30;        // dalej od miasta = poza obszarem usługi
const NEARBY_COUNT = 10;
const RING_LENGTH = 326.7;           // obwód pierścienia rezerwacji (2·π·52)

// ================================================================ stan aplikacji

const state = {
  user: null,                 // { id, name }
  tariff: null,               // cennik z /api/tariff
  scooters: new Map(),        // id -> hulajnoga z ostatniego zrzutu
  myRental: null,             // moje trwające wypożyczenie (ze zrzutu)
  myReservation: null,        // moja aktywna rezerwacja (ze zrzutu)
  rideDetails: null,          // szczegóły jazdy z trasą (/api/rentals/{id})
  selectedId: null,           // hulajnoga wybrana na mapie
  summary: null,              // zakończony przejazd pokazany jako paragon
  endBlocked: null,           // komunikat, gdy nie da się zakończyć (strefa zakazu)
  position: CITIES['Słupsk'], // moje położenie (GPS albo środek wybranego miasta)
  // rzeczy już zamknięte w aplikacji – starszy zrzut z serwera nie może ich "wskrzesić"
  closedRentalIds: new Set(),
  closedReservationIds: new Set(),
};

let map = null;
const markers = new Map();    // id hulajnogi -> znacznik
let meMarker = null;
let routeLine = null;
let events = null;
let installPrompt = null;
let loadingRide = false;

// ================================================================ pomocnicze

const $ = (id) => document.getElementById(id);
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function escapeHtml(text) {
  const element = document.createElement('div');
  element.textContent = String(text);
  return element.innerHTML;
}

function formatNumber(value, digits) {
  return Number(value).toLocaleString('pl-PL', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
const formatMoney = (pln) => formatNumber(pln, 2) + ' zł';
const formatKm = (km) => formatNumber(km, 2) + ' km';
const formatDate = (iso) => new Date(iso).toLocaleString('pl-PL', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
const secondsSince = (iso) => (Date.now() - Date.parse(iso)) / 1000;

/** 75 -> "1:15", 3725 -> "1:02:05" */
function formatDuration(totalSeconds) {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = String(seconds % 60).padStart(2, '0');
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, '0')}:${rest}`;
  return `${minutes}:${rest}`;
}

/** Odległość w metrach między dwoma punktami [lat, lon] (wzór haversine). */
function distanceM(a, b) {
  const toRad = (degrees) => degrees * Math.PI / 180;
  const dLat = toRad(b[0] - a[0]);
  const dLon = toRad(b[1] - a[1]);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(toRad(a[0])) * Math.cos(toRad(b[0])) * Math.sin(dLon / 2) ** 2;
  return 2 * 6371000 * Math.asin(Math.sqrt(h));
}

function formatWalk(meters) {
  const minutes = Math.max(1, Math.round(meters / WALKING_M_PER_MIN));
  const distance = meters < 1000 ? `${Math.round(meters / 10) * 10} m` : formatKm(meters / 1000);
  return { distance, minutes: `${minutes} min` };
}

/** Numer na znaczniku: ostatnia część identyfikatora bez zer na początku, "SC-SLP-006" -> "6".
 *  W obrębie miasta jest unikalny; pełny identyfikator widać w karcie i w podpowiedzi znacznika. */
function scooterNumber(scooterId) {
  const parts = scooterId.split('-');
  const last = parts[parts.length - 1];
  if (/^\d+$/.test(last)) return String(Number(last));   // "006" -> "6", "018" -> "18"
  return last.slice(0, 4);                                // np. "PI-A" z Raspberry Pi – bez zmian
}

function nameToUserId(name) {
  let id = name.trim().toLowerCase().replace(/\s+/g, '-').slice(0, 40);
  if (id.startsWith('sim-')) id = 'u-' + id;   // "sim-" zarezerwowane dla emulatora
  return id;
}

let toastTimer = null;
function toast(message) {
  $('toast').textContent = message;
  $('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 3500);
}

// pamięć przeglądarki (w trybie prywatnym może nie działać – wtedy aplikacja i tak działa)
const storage = {
  get(key) { try { return JSON.parse(localStorage.getItem('hulaj.' + key)); } catch (e) { return null; } },
  set(key, value) { try { localStorage.setItem('hulaj.' + key, JSON.stringify(value)); } catch (e) { /* trudno */ } },
  remove(key) { try { localStorage.removeItem('hulaj.' + key); } catch (e) { /* trudno */ } },
};

// ================================================================ komunikacja z API

class ApiError extends Error {
  constructor(message, code) {
    super(message);
    this.code = code;   // np. "no_parking_zone", "reserved"
  }
}

async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new ApiError(`HTTP ${response.status}`);
  return response.json();
}

async function sendJson(url, body = null, method = 'POST') {
  const headers = { 'Content-Type': 'application/json' };
  const token = storage.get('token');
  if (token) headers.Authorization = 'Bearer ' + token;

  const response = await fetch(url, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) {
    $('token-form').hidden = false;
    $('backdrop').hidden = false;
    throw new ApiError('Serwer wymaga tokenu API', 'unauthorized');
  }
  if (!response.ok) throw new ApiError(data.detail || `HTTP ${response.status}`, data.code);
  return data;
}

// ================================================================ 1-3. uruchomienie: start, wprowadzenie, logowanie

async function boot() {
  registerServiceWorker();
  const tariff = getJson('/api/tariff').catch(() => null);
  await wait(SPLASH_MIN_MS);
  state.tariff = await tariff;
  state.user = storage.get('user');

  if (!storage.get('onboarded')) showOnboarding();
  else if (!state.user) showLogin();
  else startApp();
  hideSplash();
}

function hideSplash() {
  $('splash').classList.add('fade');
  setTimeout(() => { $('splash').hidden = true; }, 500);
}

function registerServiceWorker() {
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => { /* np. brak HTTPS – aplikacja działa dalej */ });
  }
}

function showOnboarding() {
  $('onboarding').hidden = false;
  $('slides').scrollLeft = 0;
  updateDots();
}

function currentSlide() {
  const slides = $('slides');
  return Math.round(slides.scrollLeft / slides.clientWidth);
}

function updateDots() {
  const index = currentSlide();
  const dots = $('dots').children;
  for (let i = 0; i < dots.length; i++) dots[i].classList.toggle('on', i === index);
  const last = index === dots.length - 1;
  $('onboarding-next').textContent = last ? 'Zaczynamy' : 'Dalej';
}

$('slides').addEventListener('scroll', updateDots);

function showLogin() {
  $('login').hidden = false;
  $('login-name').focus();
}

$('login-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const name = $('login-name').value.trim();
  if (!name) return;
  state.user = { name, id: nameToUserId(name) };
  storage.set('user', state.user);
  $('login').hidden = true;
  startApp();
});

// ================================================================ 4. aplikacja

function startApp() {
  $('app').hidden = false;
  if (!map) createMap();
  showGreeting();
  connectLiveData();
}

function showGreeting() {
  const hour = new Date().getHours();
  $('hello-small').textContent = hour < 18 ? 'Dzień dobry' : 'Dobry wieczór';
  $('hello-name').textContent = state.user.name;
  const initial = state.user.name.charAt(0).toUpperCase();
  $('avatar').textContent = initial;
  $('menu-avatar').textContent = initial;
  $('menu-name').textContent = state.user.name;
  $('menu-stats').textContent = `id: ${state.user.id}`;
  $('price-line').textContent = priceText();
}

function priceText() {
  const tariff = state.tariff;
  if (!tariff) return '';
  return `${formatMoney(tariff.unlock_fee)} za odblokowanie · ${formatMoney(tariff.price_per_min)}/min`;
}

function connectLiveData() {
  refresh();
  if (events) events.close();
  // jeden strumień SSE; EventSource sam wznawia połączenie po przerwie
  events = new EventSource('/api/stream');
  events.onmessage = (event) => onSnapshot(JSON.parse(event.data));
  events.onerror = () => { $('offline').hidden = false; };
}

/** Świeży zrzut od razu po akcji (strumień SSE dostarczyłby go dopiero za chwilę). */
function refresh() {
  getJson('/api/snapshot').then(onSnapshot).catch(() => { $('offline').hidden = false; });
}

// ================================================================ mapa

function createMap() {
  map = L.map('map', { zoomControl: false }).setView(state.position, 15);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19, attribution: '&copy; OpenStreetMap',
  }).addTo(map);

  map.on('click', () => {
    if (currentView() === 'scooter') closeScooter();
  });

  getJson('/api/zones').then((zones) => {
    for (const zone of zones) {
      L.polygon(zone.polygon, { color: '#E03131', weight: 2, dashArray: '6 6', fillOpacity: 0.12 })
        .bindTooltip(`Zakaz parkowania: ${zone.name}`).addTo(map);
    }
  });

  for (const city of Object.keys(CITIES)) $('city').add(new Option(city, city));
  $('city').addEventListener('change', () => moveTo(CITIES[$('city').value], 15));

  meMarker = L.marker(state.position, {
    icon: L.divIcon({ className: '', html: '<div class="me"></div>', iconSize: [18, 18] }),
    interactive: false, keyboard: false,
  }).addTo(map);

  // przyciski na mapie przesuwają się razem z wysokością karty na dole
  new ResizeObserver(() => {
    document.documentElement.style.setProperty('--sheet-height', $('sheet').offsetHeight + 'px');
  }).observe($('sheet'));
}

function moveTo(position, zoom) {
  state.position = position;
  meMarker.setLatLng(position);
  map.setView(position, zoom);
  renderSheet();
}

/** Hulajnogi widoczne dla klienta: wolne + moja (zarezerwowana / w jeździe). Jak w Bolt/Lime. */
function isVisible(scooter) {
  return scooter.status === 'available' || isMine(scooter.id);
}

function isMine(scooterId) {
  return (state.myRental && state.myRental.scooter_id === scooterId)
    || (state.myReservation && state.myReservation.scooter_id === scooterId);
}

function pinClass(scooter) {
  if (state.myRental && state.myRental.scooter_id === scooter.id) {
    return state.myRental.status === 'paused' ? 'paused' : 'riding';
  }
  if (isMine(scooter.id)) return 'mine';
  return scooter.battery < 30 ? 'low' : '';
}

function renderMarkers() {
  for (const [id, marker] of markers) {
    const scooter = state.scooters.get(id);
    if (!scooter || !isVisible(scooter)) {
      marker.remove();
      markers.delete(id);
    }
  }
  for (const scooter of state.scooters.values()) {
    if (isVisible(scooter)) showMarker(scooter);
  }
}

function showMarker(scooter) {
  const selected = scooter.id === state.selectedId ? ' selected' : '';
  const html = `<div class="pin ${pinClass(scooter)}${selected}"><span>${escapeHtml(scooterNumber(scooter.id))}</span></div>`;

  let marker = markers.get(scooter.id);
  if (!marker) {
    marker = L.marker([scooter.lat, scooter.lon], { title: scooter.id });
    marker.on('click', () => selectScooter(scooter.id));
    marker.addTo(map);
    markers.set(scooter.id, marker);
  }
  marker.setLatLng([scooter.lat, scooter.lon]);
  if (marker.options.html !== html) {   // nowa ikona tylko wtedy, gdy coś się zmieniło
    marker.options.html = html;
    marker.setIcon(L.divIcon({ className: '', html, iconSize: [38, 38], iconAnchor: [19, 40] }));
  }
}

function drawRoute(track, finished) {
  removeRoute();
  if (track.length < 2) return;
  routeLine = L.polyline(track, {
    color: finished ? '#66777D' : '#2F6FDE', weight: 6, opacity: 0.85,
    dashArray: finished ? '8 10' : null, lineCap: 'round', interactive: false,
  }).addTo(map);
}

function removeRoute() {
  if (routeLine) { routeLine.remove(); routeLine = null; }
}

/** Pokazuje fragment mapy nad kartą (żeby karta niczego nie zasłaniała). */
function fitAboveSheet(bounds) {
  const wideScreen = window.innerWidth >= 720;   // na komputerze karta jest po lewej, w telefonie – na dole
  const sheet = $('sheet');
  map.fitBounds(bounds, {
    paddingTopLeft: wideScreen ? [sheet.offsetWidth + 60, 100] : [40, 100],
    paddingBottomRight: wideScreen ? [60, 60] : [40, sheet.offsetHeight + 30],
    maxZoom: 16,
  });
}

// ================================================================ dane na żywo

function onSnapshot(snapshot) {
  $('offline').hidden = true;
  state.scooters = new Map(snapshot.scooters.map((scooter) => [scooter.id, scooter]));

  const previousRental = state.myRental;
  state.myRental = snapshot.rentals.find((rental) =>
    rental.user_id === state.user.id && !state.closedRentalIds.has(rental.id)) || null;
  state.myReservation = snapshot.reservations.find((reservation) =>
    reservation.user_id === state.user.id && !state.closedReservationIds.has(reservation.id)) || null;

  // jazda zniknęła, choć jej nie kończyliśmy (np. serwer zamknął ją automatycznie) – pokaż paragon
  if (previousRental && !state.myRental && !state.closedRentalIds.has(previousRental.id)) {
    state.closedRentalIds.add(previousRental.id);
    showFinishedRental(previousRental.id);
  }
  if (state.myRental) loadRideDetails();

  renderMarkers();
  renderSheet();
}

async function loadRideDetails() {
  if (loadingRide) return;
  loadingRide = true;
  const rentalId = state.myRental.id;
  try {
    const details = await getJson(`/api/rentals/${rentalId}`);
    if (!state.myRental || state.myRental.id !== rentalId) return;
    const firstTime = !state.rideDetails || state.rideDetails.id !== rentalId;
    state.rideDetails = details;
    drawRoute(details.track, false);
    if (firstTime && routeLine) fitAboveSheet(routeLine.getBounds());
    renderSheet();
  } catch (error) {
    /* spróbujemy przy następnym zrzucie */
  } finally {
    loadingRide = false;
  }
}

// ================================================================ karta na dole

function currentView() {
  if (state.summary) return 'summary';
  if (state.myRental) return 'ride';
  if (state.myReservation) return 'reserved';
  if (state.selectedId && state.scooters.has(state.selectedId)) return 'scooter';
  return 'browse';
}

function renderSheet() {
  const view = currentView();
  for (const element of document.querySelectorAll('[data-view]')) {
    element.hidden = element.dataset.view !== view;
  }
  if (view === 'browse') renderBrowse();
  if (view === 'scooter') renderScooter();
  if (view === 'reserved') renderReservation();
  if (view === 'ride') renderRide();
  if (view === 'summary') renderSummary();
}

function renderBrowse() {
  const available = [...state.scooters.values()]
    .filter((scooter) => scooter.status === 'available')
    .map((scooter) => ({ scooter, meters: distanceM(state.position, [scooter.lat, scooter.lon]) }))
    .sort((a, b) => a.meters - b.meters);

  $('available-count').textContent = `${available.length} wolnych`;
  const nearby = available.slice(0, NEARBY_COUNT);
  $('nearby').innerHTML = nearby.length
    ? nearby.map(({ scooter, meters }) => {
        const walk = formatWalk(meters);
        return `<button class="card" data-ui="select" data-id="${escapeHtml(scooter.id)}">
          <b>${escapeHtml(scooter.id)}</b>
          <div class="km">${formatNumber(scooter.range_km, 0)} <small>km zasięgu</small></div>
          <span>${walk.distance} · ${walk.minutes} pieszo</span>
        </button>`;
      }).join('')
    : '<p class="empty">Brak wolnych hulajnóg – spróbuj za chwilę.</p>';
}

function renderScooter() {
  const scooter = state.scooters.get(state.selectedId);
  const walk = formatWalk(distanceM(state.position, [scooter.lat, scooter.lon]));
  const available = scooter.status === 'available';

  $('sc-id').textContent = scooter.id;
  $('sc-where').textContent = available ? `${scooter.city} · gotowa do jazdy` : `${scooter.city} · niedostępna`;
  $('sc-battery').textContent = `${Math.round(scooter.battery)}%`;
  $('sc-range').textContent = `${formatNumber(scooter.range_km, 0)} km`;
  $('sc-walk').textContent = walk.minutes;
  setBattery($('sc-battery-bar'), scooter.battery);
  $('sc-price').textContent = priceText();
  for (const button of document.querySelectorAll('[data-view="scooter"] .actions .btn')) button.disabled = !available;
}

function renderReservation() {
  const reservation = state.myReservation;
  $('res-id').textContent = reservation.scooter_id;
  updateCountdown();
}

function updateCountdown() {
  const reservation = state.myReservation;
  if (!reservation || currentView() !== 'reserved') return;
  const total = (Date.parse(reservation.expires_at) - Date.parse(reservation.created_at)) / 1000;
  const left = Math.max(0, -secondsSince(reservation.expires_at));
  $('res-left').textContent = formatDuration(left);
  $('res-ring').style.strokeDashoffset = RING_LENGTH * (1 - left / total);
}

function renderRide() {
  const rental = state.myRental;
  const scooter = state.scooters.get(rental.scooter_id);
  const details = state.rideDetails && state.rideDetails.id === rental.id ? state.rideDetails : null;

  // status jazdy: czekamy na odblokowanie / jedziemy / pauza
  const chip = $('ride-chip');
  if (rental.status === 'paused') {
    chip.className = 'chip paused';
    chip.innerHTML = '<i></i>Pauza – hulajnoga zablokowana';
  } else if (scooter && scooter.status !== 'in_ride') {
    chip.className = 'chip waiting';
    chip.innerHTML = '<i></i>Odblokowywanie…';
  } else {
    chip.className = 'chip';
    chip.innerHTML = '<i></i>W trasie';
  }
  $('ride-id').textContent = rental.scooter_id;

  $('ride-distance').textContent = formatKm(details ? details.distance_km : rental.distance_km);
  $('ride-cost').textContent = formatMoney(details ? details.receipt.total : rental.cost_pln);
  if (scooter) {
    $('ride-speed').textContent = `${Math.round(scooter.speed)} km/h`;
    $('ride-battery').textContent = `Bateria ${Math.round(scooter.battery)}% · ok. ${formatNumber(scooter.range_km, 0)} km`;
    setBattery($('ride-battery-bar'), scooter.battery);
  }

  const paused = rental.status === 'paused';
  $('ride-pause').textContent = paused ? 'Wznów jazdę' : 'Pauza';
  $('ride-pause').classList.toggle('primary', paused);

  $('ride-warning').hidden = !state.endBlocked;
  if (state.endBlocked) {
    $('ride-warning').innerHTML = `${escapeHtml(state.endBlocked)}`
      + '<button class="btn" data-ui="forceEnd">Zakończ mimo to (tryb operatora)</button>';
  }
  updateRideTimer();
}

function updateRideTimer() {
  if (currentView() === 'ride') $('ride-timer').textContent = formatDuration(secondsSince(state.myRental.started_at));
}

function setBattery(bar, battery) {
  bar.style.width = `${battery}%`;
  bar.classList.toggle('low', battery < 30);
}

function renderSummary() {
  const rental = state.summary;
  const seconds = (Date.parse(rental.ended_at) - Date.parse(rental.started_at)) / 1000;
  const free = rental.cost_pln === 0;

  if (rental.fromHistory) {
    $('sum-title').textContent = `Przejazd ${rental.scooter_id}`;
  } else if (rental.closed_automatically) {
    $('sum-title').textContent = 'Jazda zamknięta automatycznie';
  } else {
    $('sum-title').textContent = 'Dziękujemy za jazdę!';
  }
  let subtitle = formatDate(rental.started_at);
  if (rental.closed_automatically && free) subtitle = 'Hulajnoga się nie odblokowała – nie pobraliśmy opłaty';
  $('sum-sub').textContent = subtitle;

  $('sum-time').textContent = formatDuration(seconds);
  $('sum-distance').textContent = formatKm(rental.distance_km);
  $('sum-total').textContent = formatMoney(rental.cost_pln);
  $('sum-receipt').innerHTML = receiptRows(rental.receipt);
}

function receiptRows(receipt) {
  let rows = `<tr><td>Odblokowanie</td><td>${formatMoney(receipt.unlock_fee)}</td></tr>`
    + `<tr><td>Jazda · ${receipt.ride_minutes} min</td><td>${formatMoney(receipt.ride_cost)}</td></tr>`;
  if (receipt.pause_minutes > 0) {
    rows += `<tr><td>Pauza · ${receipt.pause_minutes} min</td><td>${formatMoney(receipt.pause_cost)}</td></tr>`;
  }
  return rows + `<tr class="total"><td>Razem</td><td>${formatMoney(receipt.total)}</td></tr>`;
}

// ================================================================ akcje użytkownika

function selectScooter(scooterId) {
  if (state.myRental || state.myReservation) return;   // mam już swoją hulajnogę
  const scooter = state.scooters.get(scooterId);
  if (!scooter) return;
  state.selectedId = scooterId;
  renderMarkers();
  renderSheet();
  fitAboveSheet(L.latLngBounds([state.position, [scooter.lat, scooter.lon]]));
}

function closeScooter() {
  state.selectedId = null;
  renderMarkers();
  renderSheet();
}

async function startRide(scooterId) {
  const rental = await sendJson('/api/rentals/start', { scooter_id: scooterId, user_id: state.user.id });
  state.selectedId = null;
  state.endBlocked = null;
  if (state.myReservation) state.closedReservationIds.add(state.myReservation.id);
  toast('Hulajnoga odblokowana – miłej jazdy!');
  state.myRental = rental;
}

async function endRide(force) {
  const rentalId = state.myRental.id;
  try {
    const rental = await sendJson(`/api/rentals/${rentalId}/end`, { force });
    state.closedRentalIds.add(rentalId);
    state.endBlocked = null;
    state.myRental = null;
    state.summary = rental;
    const details = await getJson(`/api/rentals/${rentalId}`).catch(() => null);
    if (details) drawRoute(details.track, true);
  } catch (error) {
    if (error.code !== 'no_parking_zone') throw error;
    // jak w Bolt/Lime: w strefie zakazu parkowania jazdy zakończyć nie można
    state.endBlocked = error.message + ' Przejedź poza czerwony obszar i spróbuj ponownie.';
    if (navigator.vibrate) navigator.vibrate(200);
  }
}

/** Przejazd zakończony bez udziału aplikacji (np. automatycznie) – pobierz i pokaż paragon. */
async function showFinishedRental(rentalId) {
  try {
    const details = await getJson(`/api/rentals/${rentalId}`);
    state.summary = details;
    drawRoute(details.track, true);
    renderSheet();
  } catch (error) { /* pominięte – brak sieci */ }
}

async function openHistory() {
  closeOverlays();
  $('history').hidden = false;
  $('backdrop').hidden = false;
  $('history-list').innerHTML = '<li class="empty">Ładowanie…</li>';
  const params = new URLSearchParams({ status: 'finished', user_id: state.user.id, limit: 30 });
  try {
    const rentals = await getJson('/api/rentals?' + params);
    $('history-list').innerHTML = rentals.length
      ? rentals.map((rental) => `<li><button data-ui="showHistoryRide" data-id="${rental.id}">
          <b>${escapeHtml(rental.scooter_id)}</b><span class="amount">${formatMoney(rental.cost_pln)}</span>
          <small>${formatDate(rental.started_at)} · ${formatKm(rental.distance_km)}</small>
        </button></li>`).join('')
      : '<li class="empty">Nie masz jeszcze przejazdów. Pierwszy czeka na mapie!</li>';
  } catch (error) {
    $('history-list').innerHTML = '<li class="empty">Nie udało się pobrać historii.</li>';
  }
}

function closeOverlays() {
  for (const id of ['backdrop', 'menu', 'history', 'token-form']) $(id).hidden = true;
}

/** Przyciski z atrybutem data-ui="nazwa" wywołują funkcję o tej nazwie (jak ACTIONS w panelu operatora). */
const UI = {
  nextSlide() {
    const index = currentSlide();
    const count = $('dots').children.length;
    if (index < count - 1) $('slides').scrollTo({ left: (index + 1) * $('slides').clientWidth, behavior: 'smooth' });
    else UI.finishOnboarding();
  },
  finishOnboarding() {
    storage.set('onboarded', true);
    $('onboarding').hidden = true;
    if (!state.user) showLogin();
  },
  showOnboarding() {
    closeOverlays();
    showOnboarding();
  },
  select(scooterId) { selectScooter(scooterId); },
  closeScooter() { closeScooter(); },

  async reserve() {
    const reservation = await sendJson('/api/reservations', { scooter_id: state.selectedId, user_id: state.user.id });
    state.selectedId = null;
    state.myReservation = reservation;
    toast('Zarezerwowano – masz 10 minut na dojście');
  },
  async cancelReservation() {
    const reservation = state.myReservation;
    await sendJson(`/api/reservations/${reservation.id}`, null, 'DELETE');
    state.closedReservationIds.add(reservation.id);
    state.myReservation = null;
    toast('Rezerwacja anulowana');
  },
  async unlock() { await startRide(state.selectedId); },
  async unlockReserved() { await startRide(state.myReservation.scooter_id); },

  async pauseOrResume() {
    const rental = state.myRental;
    const action = rental.status === 'paused' ? 'resume' : 'pause';
    state.myRental = await sendJson(`/api/rentals/${rental.id}/${action}`);
    toast(action === 'pause' ? 'Pauza: hulajnoga zablokowana, stawka niższa' : 'Jazda wznowiona');
  },
  async endRide() { await endRide(false); },
  async forceEnd() { await endRide(true); },

  closeSummary() {
    state.summary = null;
    removeRoute();
  },
  openMenu() {
    $('menu').hidden = false;
    $('backdrop').hidden = false;
  },
  openHistory() { openHistory(); },
  async showHistoryRide(rentalId) {
    const details = await getJson(`/api/rentals/${rentalId}`);
    closeOverlays();
    state.summary = { ...details, fromHistory: true };
    drawRoute(details.track, true);
    if (routeLine) fitAboveSheet(routeLine.getBounds());
  },
  closeOverlays() { closeOverlays(); },

  locateMe() {
    if (!navigator.geolocation) {
      toast('Przeglądarka nie udostępnia lokalizacji');
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (position) => useMyLocation([position.coords.latitude, position.coords.longitude]),
      () => {
        toast('Brak dostępu do lokalizacji – pokazuję wybrane miasto');
        moveTo(CITIES[$('city').value], 15);
      },
      { enableHighAccuracy: true, timeout: 8000 },
    );
  },
  async install() {
    if (!installPrompt) return;
    installPrompt.prompt();
    await installPrompt.userChoice;
    installPrompt = null;
    $('install').hidden = true;
  },
  logout() {
    storage.remove('user');
    location.reload();
  },
};

function useMyLocation(position) {
  // usługa działa w 3 miastach; kto jest daleko, widzi najbliższe z nich
  let nearestCity = null;
  let nearestKm = Infinity;
  for (const [city, center] of Object.entries(CITIES)) {
    const km = distanceM(position, center) / 1000;
    if (km < nearestKm) { nearestKm = km; nearestCity = city; }
  }
  $('city').value = nearestCity;
  if (nearestKm <= SERVICE_RADIUS_KM) {
    moveTo(position, 16);
  } else {
    toast(`Jesteś poza obszarem usługi – pokazuję ${nearestCity}`);
    moveTo(CITIES[nearestCity], 15);
  }
}

// jeden nasłuch kliknięć dla wszystkich przycisków data-ui (także tych dorysowanych później)
document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-ui]');
  if (!button || button.disabled) return;
  const action = UI[button.dataset.ui];
  const id = button.dataset.id;
  const argument = id && /^\d+$/.test(id) ? Number(id) : id;   // id wypożyczeń są liczbami

  button.disabled = true;
  try {
    await action(argument);
    renderMarkers();
    renderSheet();
    if (state.user && !$('app').hidden) refresh();
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});

$('token-form').addEventListener('submit', (event) => {
  event.preventDefault();
  storage.set('token', $('token').value.trim());
  closeOverlays();
  toast('Token zapisany – spróbuj ponownie');
});

window.addEventListener('beforeinstallprompt', (event) => {
  event.preventDefault();          // pokażemy własny przycisk w menu
  installPrompt = event;
  $('install').hidden = false;
});

setInterval(() => {
  updateRideTimer();
  updateCountdown();
}, 1000);

boot();
