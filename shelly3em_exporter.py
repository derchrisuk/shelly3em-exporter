#!/usr/bin/env python3
"""
Prometheus exporter for Shelly 3EM energy meter.

Scrapes /status and /emeter/0..2 endpoints and exposes metrics
on a configurable HTTP port for Prometheus to scrape.

Usage:
    pip install -r requirements.txt
    python shelly3em_exporter.py --host 192.168.1.100 --port 9924

Environment variables (alternative to CLI args):
    SHELLY_HOST     IP or hostname of the Shelly 3EM
    SHELLY_PORT     HTTP port of the device (default: 80)
    SHELLY_USER     Username for HTTP basic auth (optional)
    SHELLY_PASS     Password for HTTP basic auth (optional)
    EXPORTER_PORT   Port to expose metrics on (default: 9924)
"""

import argparse
import logging
import os
import sys
import time
from typing import Optional

import requests
from prometheus_client import Counter, Gauge, Info, start_http_server

log = logging.getLogger("shelly3em_exporter")

# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------

CHANNEL_LABELS = ["channel"]

# Per-channel emeter metrics
POWER = Gauge("shelly3em_power_watts", "Active power in watts", CHANNEL_LABELS)
APPARENT_POWER = Gauge(
    "shelly3em_apparent_power_va", "Apparent power in volt-amperes", CHANNEL_LABELS
)
REACTIVE_POWER = Gauge(
    "shelly3em_reactive_power_var", "Reactive power in VAr", CHANNEL_LABELS
)
VOLTAGE = Gauge("shelly3em_voltage_volts", "RMS voltage in volts", CHANNEL_LABELS)
CURRENT = Gauge("shelly3em_current_amps", "RMS current in amperes", CHANNEL_LABELS)
POWER_FACTOR = Gauge("shelly3em_power_factor", "Power factor (0–1)", CHANNEL_LABELS)
TOTAL_ENERGY = Gauge(
    "shelly3em_total_energy_wh",
    "Total consumed energy in watt-hours (lifetime counter)",
    CHANNEL_LABELS,
)
TOTAL_RETURNED = Gauge(
    "shelly3em_total_returned_energy_wh",
    "Total returned/exported energy in watt-hours (lifetime counter)",
    CHANNEL_LABELS,
)
CHANNEL_VALID = Gauge(
    "shelly3em_channel_valid",
    "1 if the channel reading is valid, 0 otherwise",
    CHANNEL_LABELS,
)

# Device-level metrics from /status
UPTIME = Gauge("shelly3em_uptime_seconds", "Device uptime in seconds")
WIFI_RSSI = Gauge("shelly3em_wifi_rssi_dbm", "WiFi signal strength in dBm")
RAM_FREE = Gauge("shelly3em_ram_free_bytes", "Free RAM in bytes")
FS_FREE = Gauge("shelly3em_fs_free_bytes", "Free filesystem space in bytes")
CLOUD_CONNECTED = Gauge(
    "shelly3em_cloud_connected", "1 if the device is connected to Shelly cloud"
)
MQTT_CONNECTED = Gauge(
    "shelly3em_mqtt_connected", "1 if the device is connected via MQTT"
)
TOTAL_POWER = Gauge(
    "shelly3em_total_power_watts",
    "Sum of active power across all channels in watts",
)

# Scrape health
SCRAPE_DURATION = Gauge(
    "shelly3em_scrape_duration_seconds", "Time spent scraping the device"
)
SCRAPE_SUCCESS = Gauge(
    "shelly3em_scrape_success", "1 if the last scrape succeeded, 0 otherwise"
)
SCRAPE_ERRORS = Counter(
    "shelly3em_scrape_errors_total", "Total number of scrape errors"
)

# Static device info (updated on every successful scrape)
DEVICE_INFO = Info("shelly3em_device", "Static information about the Shelly 3EM")


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class Shelly3EMScraper:
    def __init__(
        self,
        host: str,
        port: int = 80,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: float = 10.0,
    ):
        self.base_url = f"http://{host}:{port}"
        self.auth = (username, password) if username else None
        self.timeout = timeout
        self._session = requests.Session()
        if self.auth:
            self._session.auth = self.auth

    def _get(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        resp = self._session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def scrape(self) -> None:
        start = time.monotonic()
        try:
            self._scrape_status()
            for channel in range(3):
                self._scrape_emeter(channel)
            SCRAPE_SUCCESS.set(1)
        except Exception as exc:
            log.error("Scrape failed: %s", exc)
            SCRAPE_SUCCESS.set(0)
            SCRAPE_ERRORS.inc()
        finally:
            SCRAPE_DURATION.set(time.monotonic() - start)

    def _scrape_status(self) -> None:
        data = self._get("/status")

        UPTIME.set(data.get("uptime", 0))

        wifi = data.get("wifi_sta", {})
        WIFI_RSSI.set(wifi.get("rssi", 0))

        ram = data.get("ram_free", 0)
        RAM_FREE.set(ram)

        fs = data.get("fs_free", 0)
        FS_FREE.set(fs)

        cloud = data.get("cloud", {})
        CLOUD_CONNECTED.set(1 if cloud.get("connected") else 0)

        mqtt = data.get("mqtt", {})
        MQTT_CONNECTED.set(1 if mqtt.get("connected") else 0)

        # total_power is the sum across all emeters, provided by the device
        TOTAL_POWER.set(data.get("total_power", 0))

        # Static device info – grab from the update/device block if present
        update = data.get("update", {})
        DEVICE_INFO.info(
            {
                "firmware": data.get("update", {}).get("old_version", "unknown"),
                "new_firmware_available": str(update.get("has_update", False)),
                "mac": data.get("mac", "unknown"),
                "host": self.base_url,
            }
        )

    def _scrape_emeter(self, channel: int) -> None:
        data = self._get(f"/emeter/{channel}")
        label = str(channel)

        POWER.labels(channel=label).set(data.get("power", 0))
        APPARENT_POWER.labels(channel=label).set(data.get("apparent_power", 0))
        REACTIVE_POWER.labels(channel=label).set(data.get("reactive_power", 0))
        VOLTAGE.labels(channel=label).set(data.get("voltage", 0))
        CURRENT.labels(channel=label).set(data.get("current", 0))
        POWER_FACTOR.labels(channel=label).set(data.get("pf", 0))
        TOTAL_ENERGY.labels(channel=label).set(data.get("total", 0))
        TOTAL_RETURNED.labels(channel=label).set(data.get("total_returned", 0))
        CHANNEL_VALID.labels(channel=label).set(1 if data.get("is_valid") else 0)


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------


def polling_loop(scraper: Shelly3EMScraper, interval: float) -> None:
    """Continuously scrape the device every `interval` seconds."""
    while True:
        scraper.scrape()
        time.sleep(interval)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Prometheus exporter for Shelly 3EM")
    p.add_argument(
        "--host",
        default=os.environ.get("SHELLY_HOST", ""),
        help="IP or hostname of the Shelly 3EM (env: SHELLY_HOST)",
    )
    p.add_argument(
        "--device-port",
        type=int,
        default=int(os.environ.get("SHELLY_PORT", "80")),
        help="HTTP port of the Shelly device (env: SHELLY_PORT, default: 80)",
    )
    p.add_argument(
        "--user",
        default=os.environ.get("SHELLY_USER", ""),
        help="Username for HTTP basic auth (env: SHELLY_USER)",
    )
    p.add_argument(
        "--password",
        default=os.environ.get("SHELLY_PASS", ""),
        help="Password for HTTP basic auth (env: SHELLY_PASS)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("EXPORTER_PORT", "9924")),
        help="Port to expose /metrics on (env: EXPORTER_PORT, default: 9924)",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("SCRAPE_INTERVAL", "15")),
        help="Seconds between background scrapes (default: 15)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("SCRAPE_TIMEOUT", "10")),
        help="HTTP request timeout in seconds (default: 10)",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.host:
        log.error("No Shelly host specified. Use --host or set SHELLY_HOST.")
        sys.exit(1)

    scraper = Shelly3EMScraper(
        host=args.host,
        port=args.device_port,
        username=args.user or None,
        password=args.password or None,
        timeout=args.timeout,
    )

    # Perform an initial scrape so metrics are populated immediately
    log.info("Performing initial scrape of %s …", args.host)
    scraper.scrape()

    # Background polling thread
    import threading

    t = threading.Thread(
        target=polling_loop, args=(scraper, args.interval), daemon=True
    )
    t.start()

    log.info("Starting exporter on port %d (scrape interval: %ss)", args.port, args.interval)
    start_http_server(args.port)

    # Keep the main thread alive
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()