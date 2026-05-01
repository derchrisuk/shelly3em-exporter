# shelly3em-exporter
```
Prometheus exporter for Shelly 3EM energy meter

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
```
