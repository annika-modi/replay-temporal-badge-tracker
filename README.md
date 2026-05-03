# Conference Tracker — BLE Badge Positioning Backend

Live BLE badge tracker for the **Replay by Temporal** conference at Moscone South.  
Receives scan data from ESP32 base stations, trilaterates badge positions, and streams them live to the browser.

---

## What It Does

- Receives BLE scan POSTs from ESP32 base stations (`/enroll` + `/scanreport`)
- Stores all scan data in a local SQLite database (`scans.db`)
- Runs trilateration to estimate each badge's (x, y) position on the floor plan
- Pushes live badge positions to the browser via Server-Sent Events — no polling
- Serves the frontend map at `http://localhost:5000`
- Table/base station positions are defined in `tables.json` — not hardcoded

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3, Flask |
| Positioning | Trilateration via `positioning.py` (numpy) |
| Database | SQLite (`scans.db`) |
| Live updates | Server-Sent Events (SSE) |
| Frontend | Vanilla HTML/CSS/JS (`public/index.html`) |

---

## Setup

**Requirements:** Python 3, pip

```bash
# Clone and enter the repo
git clone <repo-url>
cd conference-tracker

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate       # Mac/Linux
venv\Scripts\activate          # Windows

# Install dependencies
pip install -r requirements.txt

# Run the server (creates scans.db automatically on first run)
python server.py

# For a clean test run (wipes scans.db before starting)
python server.py --fresh
```

Open **http://localhost:5000** in your browser — you should see the floor map.

---

## ESP32 Firmware Endpoints

These match the [BLE-Replay-Firmware](https://github.com/your-link-here) spec exactly.

### `POST /enroll`
Sent once by each ESP32 on boot after connecting to WiFi.

```json
{
  "mac": "AA:BB:CC:DD:EE:FF",
  "table_uid": 12
}
```

### `POST /scanreport`
Sent every 5 seconds. One batch per ESP32 per scan window.

```json
{
  "table_uid": 12,
  "timestamp": "2026-05-02T14:23:05Z",
  "scans": [
    {
      "ble_uid": "f7826da6-4fa2-4e98-8024-bc5b71e0893e:100:42",
      "mac": "c1:9d:4b:22:08:fa",
      "rssi": -67,
      "tx_power": -59
    },
    {
      "ble_uid": null,
      "mac": "5e:a3:11:0c:7d:90",
      "rssi": -82,
      "tx_power": null
    }
  ]
}
```

`ble_uid` is used as the badge identifier when present (iBeacon UUID:major:minor). Falls back to `mac` if null.

---

## Debug Endpoints

| URL | What it shows |
|---|---|
| `http://localhost:5000/debug/scans` | Last 100 raw scan rows from SQLite |
| `http://localhost:5000/debug/enrolled` | All enrolled ESP32s and their table UIDs |
| `http://localhost:5000/debug/memory` | Current in-memory badge state used for trilateration |

These are the first place to check if badges aren't showing up on the map.

---

## Table Configuration (`tables.json`)

Base station positions are defined in `tables.json` — **not hardcoded**. Each entry maps a table name to its pixel coordinates on the floor plan image and its integer `table_uid` from the firmware.

```json
{
  "tables": {
    "table-1": { "x": 923, "y": 454, "table_uid": 1 },
    "table-2": { "x": 923, "y": 656, "table_uid": 2 }
  }
}
```

To find pixel coordinates for a table: run the server, open `http://localhost:5000`, and click anywhere on the map — the `(x, y)` coordinate is copied to your clipboard.

---

## Project Structure

```
conference-tracker/
├── server.py          ← Flask backend: receives scans, runs trilateration, serves SSE + frontend
├── positioning.py     ← Trilateration logic (RSSI → distance → x,y)
├── config.py          ← Shared constants (room dimensions, staleness window, etc.)
├── fake_data.py       ← Emulator for local testing without hardware
├── tables.json        ← Base station positions on the floor plan
├── requirements.txt   ← Python dependencies
├── public/
│   ├── index.html     ← Map frontend (SSE client, badge rendering)
│   ├── admin.html     ← Admin panel
│   └── *.PNG          ← Floor plan images
└── scans.db           ← SQLite database (auto-created, gitignored)
```

---

## Running Without Hardware (Emulator)

To test the full pipeline locally without any ESP32s:

```bash
# Terminal 1 — start the server
python server.py --fresh

# Terminal 2 — run the emulator (sends fake scan data)
python fake_data.py
```

Open `http://localhost:5000` — badges should appear and move on the map.