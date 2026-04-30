# Replay Floor Map — Frontend

Live BLE badge tracker for the **Replay by Temporal** conference at Moscone South.  
Displays real-time badge positions as gold stars on an interactive floor map.

---

## What It Does

- Renders the Moscone South Level 00 venue floor plan in the browser
- Receives BLE badge scan data from base stations via `POST /stream`
- Pushes live updates to the browser instantly using **Server-Sent Events** — no page refresh needed
- Shows each detected badge as an animated gold ★ labeled with the last 4 chars of its MAC address
- 17 table/base station positions are hardcoded to the floor plan coordinates 
- Click anywhere on the map to get the pixel `(x, y)` coordinate (copied to clipboard)

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3, Flask |
| Live updates | Server-Sent Events (SSE) via Flask streaming `Response` |
| Frontend | Vanilla HTML / CSS / JS — no framework |
| Map serving | Flask static files (`public/`) |

---

## Setup

**Requirements:** Python 3, pip

```bash
# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate       # Mac/Linux
# venv\Scripts\activate        # Windows

# Install dependencies
pip install flask

# Run the server
python server.py
```

Open **http://localhost:5000** in your browser.

You should see the floor map with 17 faint table markers and a **"live"** status indicator in the top bar.

---

## API

### `POST /stream`
Accepts incoming BLE scan data. Call this from the BLE backend whenever a base station detects badges.

**Request body — array of scan readings:**
```json
[
  { "mac": "AA:BB:CC:DD:EE:FF", "rssi": -65, "reader_id": "table-1" },
  { "mac": "11:22:33:44:55:66", "rssi": -72, "reader_id": "table-3" }
]
```

| Field | Type | Description |
|---|---|---|
| `mac` | string | Badge MAC address |
| `rssi` | number | Signal strength in dBm (e.g. `-65`). Less negative = stronger signal. |
| `reader_id` | string | Which table/base station detected this badge (e.g. `"table-1"`) |

If the same badge MAC is reported by multiple tables, the server keeps only the reading with the strongest RSSI (least negative).

**Quick test with curl:**
```bash
curl -X POST http://localhost:5000/stream \
     -H "Content-Type: application/json" \
     -d '[{"mac":"AA:BB:CC:DD:EE:FF","rssi":-65,"reader_id":"table-1"}]'
```

A gold ★ should appear near `table-1` on the map instantly.

---

### `GET /events`
SSE stream — the browser connects here automatically to receive live badge updates. You don't call this manually.

---

### `DELETE /badges`
Clears all badges from the map.

```bash
curl -X DELETE http://localhost:5000/badges
```

---

## Table Coordinates

17 base station tables are hardcoded in `public/index.html` with their pixel positions on the floor plan:

| ID | x | y |
|---|---|---|
| table-1 | 923 | 454 |
| table-2 | 923 | 656 |
| table-3 | 923 | 732 |
| table-4 | 753 | 451 |
| table-5 | 753 | 530 |
| table-6 | 753 | 656 |
| table-7 | 753 | 727 |
| table-8 | 923 | 1026 |
| table-9 | 923 | 1100 |
| table-10 | 923 | 1158 |
| table-11 | 923 | 1214 |
| table-12 | 923 | 1287 |
| table-13 | 855 | 1287 |
| table-14 | 923 | 1320 |
| table-15 | 1091 | 1026 |
| table-16 | 1091 | 1097 |
| table-17 | 1091 | 1156 |

To find coordinates for new tables, click anywhere on the map while the server is running — the `(x, y)` pixel coordinate pops up and copies to your clipboard.

---

## Project Structure

```
replay-frontend/
├── server.py          ← Flask backend — serves page, receives scan data, pushes SSE updates
├── public/
│   ├── index.html     ← Full-screen map UI with SSE client
│   └── map.png        ← Venue floor plan image
└── README.md
```

---

## Part of a Larger System

This repo is the frontend layer. It is designed to sit downstream of a BLE trilateration backend that:
1. Receives raw scan data from ESP32 base stations (one per table)
2. Calculates badge `(x, y)` positions via RSSI trilateration across 3+ base stations
3. Calls `POST /stream` on this server with the computed badge positions

The backend repo will be linked here once available.