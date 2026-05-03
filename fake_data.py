#!/usr/bin/env python3
"""
fake_data.py — BLE Scanner Emulator
=====================================
Simulates 20 ESP32 base stations posting scan data to the local server,
mirroring the behavior of the BLE-Replay-Firmware.

Endpoints:
  POST /enroll      — called once per table on startup
  POST /scanreport  — called every 5s per table with badge sightings

Usage:
  python server.py --fresh   # start server first
  python fake_data.py        # run in a second terminal

Press Ctrl+C to stop.
"""

import json
import math
import random
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────

SERVER        = "http://localhost:5000"
SCAN_INTERVAL = 5.0  # seconds between scanreports
STAGGER       = 2.0  # max seconds to offset table start times

# ── Virtual tables ────────────────────────────────────────────────────────────
# table_uid integers correspond to uid_registry entries in tables.json.
# x_m / y_m are positions in meters on the venue floor plan.

TABLES = [
    {"table_uid": 1,  "x_m": 10.0, "y_m":  8.0},
    {"table_uid": 2,  "x_m": 10.0, "y_m": 18.0},
    {"table_uid": 3,  "x_m": 10.0, "y_m": 22.0},
    {"table_uid": 4,  "x_m": 28.0, "y_m":  8.0},
    {"table_uid": 5,  "x_m": 28.0, "y_m": 14.0},
    {"table_uid": 6,  "x_m": 28.0, "y_m": 18.0},
    {"table_uid": 7,  "x_m": 28.0, "y_m": 22.0},
    {"table_uid": 8,  "x_m": 10.0, "y_m": 42.0},
    {"table_uid": 9,  "x_m": 10.0, "y_m": 46.0},
    {"table_uid": 10, "x_m": 10.0, "y_m": 50.0},
    {"table_uid": 11, "x_m": 10.0, "y_m": 54.0},
    {"table_uid": 12, "x_m": 10.0, "y_m": 58.0},
    {"table_uid": 13, "x_m": 18.0, "y_m": 58.0},
    {"table_uid": 14, "x_m": 10.0, "y_m": 62.0},
    {"table_uid": 15, "x_m": 30.0, "y_m": 42.0},
    {"table_uid": 16, "x_m": 30.0, "y_m": 46.0},
    {"table_uid": 17, "x_m": 30.0, "y_m": 50.0},
    {"table_uid": 18, "x_m": 50.0, "y_m": 20.0},
    {"table_uid": 19, "x_m": 35.0, "y_m": 45.0},
    {"table_uid": 20, "x_m": 60.0, "y_m": 45.0},
]

# Assign a unique fake WiFi MAC to each virtual ESP32
for i, t in enumerate(TABLES):
    t["esp32_mac"] = f"AA:BB:CC:DD:EE:{i+1:02X}"

# ── Virtual badges ────────────────────────────────────────────────────────────
# iBeacon badges carry a ble_uid (uuid:major:minor).
# Generic BLE devices have ble_uid = None and are identified by MAC only.

IBEACON_UUID = "f7826da6-4fa2-4e98-8024-bc5b71e0893e"

BADGES = [
    {"mac": "c1:9d:4b:22:08:01", "ble_uid": f"{IBEACON_UUID}:100:1", "x_m": 12.0, "y_m": 10.0},
    {"mac": "c1:9d:4b:22:08:02", "ble_uid": f"{IBEACON_UUID}:100:2", "x_m": 25.0, "y_m": 20.0},
    {"mac": "c1:9d:4b:22:08:03", "ble_uid": f"{IBEACON_UUID}:100:3", "x_m":  8.0, "y_m": 45.0},
    {"mac": "c1:9d:4b:22:08:04", "ble_uid": f"{IBEACON_UUID}:100:4", "x_m": 30.0, "y_m": 55.0},
    {"mac": "c1:9d:4b:22:08:05", "ble_uid": f"{IBEACON_UUID}:100:5", "x_m": 15.0, "y_m": 60.0},
    {"mac": "5e:a3:11:0c:7d:01", "ble_uid": None, "x_m": 20.0, "y_m": 15.0},
    {"mac": "5e:a3:11:0c:7d:02", "ble_uid": None, "x_m": 10.0, "y_m": 50.0},
    {"mac": "5e:a3:11:0c:7d:03", "ble_uid": None, "x_m": 28.0, "y_m": 44.0},
]

# ── RSSI simulation ───────────────────────────────────────────────────────────

TX_POWER  = -59   # calibrated RSSI at 1 metre (dBm)
PATH_LOSS =  2.5  # indoor path loss exponent (2.0 = free space, 2.5-3.5 = indoors)
NOISE_STD =  3.0  # Gaussian noise standard deviation (dBm)


def distance_to_rssi(dist_m):
    """Convert a physical distance in metres to a simulated RSSI reading (dBm)."""
    dist_m = max(dist_m, 0.1)
    rssi = TX_POWER - 10 * PATH_LOSS * math.log10(dist_m)
    rssi += random.gauss(0, NOISE_STD)
    return round(rssi, 1)


def badges_visible_from_table(table):
    """
    Return scan entries for all badges within BLE range (~15 m) of a table.
    RSSI is derived from the simulated distance between badge and table.
    """
    entries = []
    for badge in BADGES:
        dist = math.sqrt(
            (badge["x_m"] - table["x_m"]) ** 2 +
            (badge["y_m"] - table["y_m"]) ** 2
        )
        if dist > 15.0:
            continue
        entries.append({
            "ble_uid":  badge["ble_uid"],
            "mac":      badge["mac"],
            "rssi":     int(distance_to_rssi(dist)),
            "tx_power": TX_POWER if badge["ble_uid"] else None,
        })
    return entries


# ── HTTP ──────────────────────────────────────────────────────────────────────

def post_json(path, payload):
    """POST a JSON payload to the local server. Returns HTTP status or None on failure."""
    url  = SERVER + path
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.URLError as e:
        print(f"  [!] POST {path} failed: {e.reason}")
        return None


# ── Per-table thread ──────────────────────────────────────────────────────────

def run_table(table):
    """
    Simulates one ESP32 base station.
    Enrolls on startup, then posts a scanreport every SCAN_INTERVAL seconds.
    """
    uid   = table["table_uid"]
    mac   = table["esp32_mac"]
    label = f"[table {uid:2d}]"

    status = post_json("/enroll", {"mac": mac, "table_uid": uid})
    print(f"{label} enrolled  [HTTP {status}]")

    time.sleep(random.uniform(0, STAGGER))

    while True:
        scans = badges_visible_from_table(table)
        payload = {
            "table_uid": uid,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "scans":     scans,
        }
        status = post_json("/scanreport", payload)
        print(f"{label} scanreport → {len(scans)} badge(s)  [HTTP {status}]")
        time.sleep(SCAN_INTERVAL)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    print(f"Emulator starting → {SERVER}")
    print(f"  {len(TABLES)} tables · {len(BADGES)} badges · {SCAN_INTERVAL}s interval")
    print("  Ctrl+C to stop\n")

    for table in TABLES:
        t = threading.Thread(target=run_table, args=(table,), daemon=True)
        t.start()
        time.sleep(0.1)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()