from __future__ import annotations

from base64 import b64encode
from dataclasses import dataclass
import json
from statistics import fmean
from threading import Event, Lock, Thread
from urllib.error import URLError
from urllib.request import Request, urlopen

from ilvesbench.config import NetioConfig


@dataclass(slots=True)
class NetioReading:
    total_load_watts: float
    socket_load_watts: dict[str, float]
    socket_energy_wh: dict[str, float]
    device_name: str | None = None


class NetioEnergyMonitor:
    """Poll NETIO's JSON API while a benchmark is executing."""

    def __init__(self, config: NetioConfig) -> None:
        self._config = config
        self._readings: list[NetioReading] = []
        self._errors: list[str] = []
        self._lock = Lock()
        self._stop = Event()
        self._thread: Thread | None = None

    @property
    def configured(self) -> bool:
        return bool(self._config.url.strip())

    def start(self) -> None:
        if not self.configured:
            return
        self._capture()
        self._thread = Thread(target=self._poll, name="netio-energy-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> dict:
        if not self.configured:
            return self._not_configured_result()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self._config.timeout_seconds + 1.0))
        self._capture()
        with self._lock:
            readings, errors = list(self._readings), list(self._errors)
        if not readings:
            reason = errors[-1] if errors else "NETIO did not return a reading."
            return {"status": "unavailable", "source": "netio_json_api", "summary": f"NETIO energy measurement hardware was configured but unavailable: {reason}", "error": reason, "sample_count": 0}
        loads = [reading.total_load_watts for reading in readings]
        first, last = readings[0], readings[-1]
        sockets = {}
        for socket_id in sorted({key for reading in readings for key in reading.socket_load_watts}):
            socket_loads = [reading.socket_load_watts.get(socket_id, 0.0) for reading in readings]
            start_energy, end_energy = first.socket_energy_wh.get(socket_id), last.socket_energy_wh.get(socket_id)
            sockets[socket_id] = {"average_load_watts": round(fmean(socket_loads), 3), "energy_delta_wh": round(end_energy - start_energy, 6) if start_energy is not None and end_energy is not None else None}
        total_delta_wh = sum(value["energy_delta_wh"] for value in sockets.values() if value["energy_delta_wh"] is not None)
        return {"status": "measured", "source": "netio_json_api", "device_name": next((reading.device_name for reading in readings if reading.device_name), None), "sample_count": len(readings), "average_watts": round(fmean(loads), 3), "minimum_watts": round(min(loads), 3), "maximum_watts": round(max(loads), 3), "energy_delta_wh": round(total_delta_wh, 6), "sockets": sockets, "summary": f"Measured {round(fmean(loads), 2)} W average power from NETIO across {len(readings)} sample(s).", "warnings": errors}

    def _poll(self) -> None:
        interval = max(0.2, float(self._config.poll_interval_seconds))
        while not self._stop.wait(interval):
            self._capture()

    def _capture(self) -> None:
        try:
            reading = self._fetch_reading()
            with self._lock:
                self._readings.append(reading)
        except (OSError, ValueError, URLError, TimeoutError) as exc:
            with self._lock:
                self._errors.append(str(exc))

    def _fetch_reading(self) -> NetioReading:
        url = self._config.url.strip()
        if not url.endswith("netio.json"):
            url = url.rstrip("/") + "/netio.json"
        request = Request(url, method="GET")
        if self._config.username or self._config.password:
            credentials = f"{self._config.username}:{self._config.password}".encode()
            request.add_header("Authorization", "Basic " + b64encode(credentials).decode())
        with urlopen(request, timeout=max(0.1, float(self._config.timeout_seconds))) as response:
            payload = json.loads(response.read().decode("utf-8"))
        outputs = payload.get("Outputs", [])
        socket_loads = {str(item.get("ID")): float(item.get("Load") or 0) for item in outputs}
        socket_energy = {str(item.get("ID")): float(item.get("Energy")) for item in outputs if item.get("Energy") is not None}
        global_measure = payload.get("GlobalMeasure") or {}
        total_load = global_measure.get("TotalLoad")
        return NetioReading(float(total_load) if total_load is not None else sum(socket_loads.values()), socket_loads, socket_energy, (payload.get("Agent") or {}).get("DeviceName"))

    @staticmethod
    def _not_configured_result() -> dict:
        return {"status": "no_hardware", "source": "none", "summary": "No energy measurement hardware was found. Configure [netio] to measure energy.", "sample_count": 0}
