"""Pośrednik TCP między urządzeniami a brokerem MQTT, który psuje łącze w kontrolowany sposób.

Dlaczego pośrednik, a nie "utrata pakietów"? MQTT działa na TCP, a TCP sam powtarza zgubione pakiety –
utrata pakietów objawia się więc tylko większym opóźnieniem. Wiadomości giną dopiero wtedy, gdy zrywa się
całe połączenie (zasięg GSM znika w tunelu, modem się restartuje). Dlatego pośrednik umie:

  * delay_ms / jitter_ms – opóźnić każdy fragment danych (w obie strony), zachowując kolejność,
  * outage_every_s / outage_len_s – co jakiś czas zerwać wszystkie połączenia i przez chwilę nie
    przyjmować nowych (symulacja utraty zasięgu).

Działa w osobnym wątku z własną pętlą asyncio. Momenty awarii zapisuje w `outages` (czas Unix),
żeby można je było zaznaczyć na wykresie.
"""
import asyncio
import random
import threading
import time


class NetemProxy:
    def __init__(self, listen_port: int, target_host: str, target_port: int,
                 delay_ms: float = 0, jitter_ms: float = 0,
                 outage_every_s: float = 0, outage_len_s: float = 0, seed: int = 1):
        self.listen_port = listen_port
        self.target_host = target_host
        self.target_port = target_port
        self.delay_s = delay_ms / 1000
        self.jitter_s = jitter_ms / 1000
        self.outage_every_s = outage_every_s
        self.outage_len_s = outage_len_s
        self.rng = random.Random(seed)

        self.outages: list[tuple[float, float]] = []   # (początek, koniec) – czas Unix
        self._link_down = False
        self._writers: set[asyncio.StreamWriter] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    # ---------------------------------------------------------------- sterowanie z zewnątrz

    def start(self):
        self._thread = threading.Thread(target=self._run, name="netem-proxy", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("Pośrednik TCP nie wystartował")

    def stop_outages(self):
        """Koniec pomiaru: łącze ma już działać, żeby zaległe wiadomości QoS 1 mogły dotrzeć."""
        self.outage_every_s = 0

    def stop(self):
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    # ---------------------------------------------------------------- wnętrze (wątek pośrednika)

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        server = self._loop.run_until_complete(
            asyncio.start_server(self._handle_client, "127.0.0.1", self.listen_port))
        if self.outage_every_s > 0:
            self._loop.create_task(self._outage_loop())
        self._ready.set()
        self._loop.run_forever()
        # zatrzymanie: zamykamy połączenia i pozwalamy zadaniom dokończyć bloki finally
        server.close()
        for writer in list(self._writers):
            writer.transport.abort()
        tasks = asyncio.all_tasks(self._loop)
        for task in tasks:
            task.cancel()
        self._loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        self._loop.close()

    async def _outage_loop(self):
        while self.outage_every_s > 0:
            await asyncio.sleep(self.outage_every_s)
            if self.outage_every_s <= 0:
                break
            start = time.time()
            self._link_down = True
            for writer in list(self._writers):   # zerwanie wszystkich połączeń
                writer.transport.abort()
            await asyncio.sleep(self.outage_len_s)
            self._link_down = False
            self.outages.append((start, time.time()))

    async def _handle_client(self, client_reader, client_writer):
        if self._link_down:
            client_writer.transport.abort()   # brak zasięgu – połączenie odrzucone
            return
        try:
            broker_reader, broker_writer = await asyncio.open_connection(self.target_host, self.target_port)
        except OSError:
            client_writer.transport.abort()
            return
        self._writers.update((client_writer, broker_writer))
        await asyncio.gather(self._pipe(client_reader, broker_writer),
                             self._pipe(broker_reader, client_writer),
                             return_exceptions=True)
        for writer in (client_writer, broker_writer):
            self._writers.discard(writer)
            writer.transport.abort()

    async def _pipe(self, reader, writer):
        """Przepisuje dane w jedną stronę. Każdy fragment czeka delay ± jitter, ale nigdy nie wyprzedza
        poprzedniego (TCP nie zmienia kolejności bajtów)."""
        queue: asyncio.Queue = asyncio.Queue()

        async def deliver():
            while True:
                due, data = await queue.get()
                if data is None:
                    return
                wait = due - self._loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                writer.write(data)
                await writer.drain()

        sender = self._loop.create_task(deliver())
        last_due = 0.0
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                delay = self.delay_s + self.rng.uniform(-self.jitter_s, self.jitter_s)
                due = max(self._loop.time() + max(delay, 0), last_due)
                last_due = due
                queue.put_nowait((due, data))
        finally:
            queue.put_nowait((0, None))
            try:
                await sender
            except Exception:
                pass
            writer.transport.abort()
