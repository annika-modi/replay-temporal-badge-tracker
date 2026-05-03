#!/usr/bin/env python3
"""
fake_data.py — BLE Scanner Emulator
=====================================
Simulates ESP32 base stations on two floors posting scan data to the local server.

Floor assignment:
  table_uid 1–17  → Level 00   (floor = "level00")
  table_uid 18–20 → Concourse  (floor = "concourse")

100 badges per floor, all positioned within valid detection zones:
  Level 00:   x in [13, 170], y in [25, 90]
  Concourse:  y in [35, 75]   (full width)
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
SCAN_INTERVAL = 5.0
STAGGER       = 2.0

ROOM_W_M = 100.0
ROOM_H_M = 75.0
GRID_W   = 200
GRID_H   = 100

# ── Helpers ───────────────────────────────────────────────────────────────────

def grid_to_m(gx, gy):
    return gx / GRID_W * ROOM_W_M, gy / GRID_H * ROOM_H_M

def random_level00_pos():
    """Random position within valid Level 00 zone: x=[13,170], y=[25,90]"""
    return grid_to_m(random.uniform(13, 170), random.uniform(25, 90))

def random_concourse_pos():
    """Random position within valid Concourse zone: y=[35,75]"""
    return grid_to_m(random.uniform(5, 195), random.uniform(35, 75))

# ── Tables ────────────────────────────────────────────────────────────────────

TABLES_LEVEL00 = [
    {"table_uid": 1,  "floor": "level00", "x_m": 6.5,  "y_m": 22.5},
    {"table_uid": 2,  "floor": "level00", "x_m": 6.5,  "y_m": 30.0},
    {"table_uid": 3,  "floor": "level00", "x_m": 6.5,  "y_m": 37.5},
    {"table_uid": 4,  "floor": "level00", "x_m": 18.5, "y_m": 22.5},
    {"table_uid": 5,  "floor": "level00", "x_m": 18.5, "y_m": 30.0},
    {"table_uid": 6,  "floor": "level00", "x_m": 18.5, "y_m": 37.5},
    {"table_uid": 7,  "floor": "level00", "x_m": 37.0, "y_m": 22.5},
    {"table_uid": 8,  "floor": "level00", "x_m": 37.0, "y_m": 30.0},
    {"table_uid": 9,  "floor": "level00", "x_m": 37.0, "y_m": 37.5},
    {"table_uid": 10, "floor": "level00", "x_m": 55.0, "y_m": 22.5},
    {"table_uid": 11, "floor": "level00", "x_m": 55.0, "y_m": 30.0},
    {"table_uid": 12, "floor": "level00", "x_m": 55.0, "y_m": 37.5},
    {"table_uid": 13, "floor": "level00", "x_m": 70.0, "y_m": 22.5},
    {"table_uid": 14, "floor": "level00", "x_m": 70.0, "y_m": 30.0},
    {"table_uid": 15, "floor": "level00", "x_m": 70.0, "y_m": 37.5},
    {"table_uid": 16, "floor": "level00", "x_m": 82.0, "y_m": 27.0},
    {"table_uid": 17, "floor": "level00", "x_m": 82.0, "y_m": 37.5},
]

TABLES_CONCOURSE = [
    {"table_uid": 18, "floor": "concourse", "x_m": 20.0, "y_m": 36.0},
    {"table_uid": 19, "floor": "concourse", "x_m": 50.0, "y_m": 36.0},
    {"table_uid": 20, "floor": "concourse", "x_m": 80.0, "y_m": 36.0},
]

ALL_TABLES = TABLES_LEVEL00 + TABLES_CONCOURSE

for i, t in enumerate(ALL_TABLES):
    t["esp32_mac"] = f"AA:BB:CC:DD:EE:{i+1:02X}"

# ── Generate 100 badges per floor ─────────────────────────────────────────────

IBEACON_UUID = "f7826da6-4fa2-4e98-8024-bc5b71e0893e"

def make_badges(floor, count):
    badges = []
    pos_fn = random_level00_pos if floor == "level00" else random_concourse_pos
    for i in range(count):
        x_m, y_m = pos_fn()
        # First 50 per floor are iBeacon (conference badges), rest are generic BLE
        if i < 50:
            mac     = f"C1:9D:4B:{floor[:2].upper()}:{i//256:02X}:{i%256:02X}"
            ble_uid = f"{IBEACON_UUID}:{200 if floor=='level00' else 201}:{i+1}"
        else:
            mac     = f"5E:A3:11:{floor[:2].upper()}:{i//256:02X}:{i%256:02X}"
            ble_uid = None
        badges.append({
            "mac":     mac,
            "ble_uid": ble_uid,
            "floor":   floor,
            "x_m":     x_m,
            "y_m":     y_m,
        })
    return badges

BADGES_LEVEL00   = make_badges("level00",   100)
BADGES_CONCOURSE = make_badges("concourse", 100)

BADGES_BY_FLOOR = {
    "level00":   BADGES_LEVEL00,
    "concourse": BADGES_CONCOURSE,
}

# ── RSSI simulation ───────────────────────────────────────────────────────────

TX_POWER  = -59
PATH_LOSS =  2.5
NOISE_STD =  3.0

def distance_to_rssi(dist_m):
    dist_m = max(dist_m, 0.1)
    return round(TX_POWER - 10 * PATH_LOSS * math.log10(dist_m) + random.gauss(0, NOISE_STD), 1)

def badges_visible_from_table(table):
    """Return scan entries for badges on the same floor within BLE range (~15m)."""
    badges  = BADGES_BY_FLOOR.get(table["floor"], [])
    entries = []
    for badge in badges:
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
    uid   = table["table_uid"]
    mac   = table["esp32_mac"]
    floor = table["floor"]
    label = f"[table {uid:2d} / {floor[:4]}]"

    status = post_json("/enroll", {"mac": mac, "table_uid": uid})
    print(f"{label} enrolled  [HTTP {status}]")

    time.sleep(random.uniform(0, STAGGER))

    while True:
        scans  = badges_visible_from_table(table)
        status = post_json("/scanreport", {
            "table_uid": uid,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "scans":     scans,
        })
        print(f"{label} scanreport → {len(scans)} badge(s)  [HTTP {status}]")
        time.sleep(SCAN_INTERVAL)

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    print(f"Emulator starting → {SERVER}")
    print(f"  Level 00:   {len(TABLES_LEVEL00)} tables · {len(BADGES_LEVEL00)} badges")
    print(f"  Concourse:  {len(TABLES_CONCOURSE)} tables · {len(BADGES_CONCOURSE)} badges")
    print(f"  Interval:   {SCAN_INTERVAL}s · Ctrl+C to stop\n")

    for table in ALL_TABLES:
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