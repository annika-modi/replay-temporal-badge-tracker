"""
server.py — Replay Floor Map Backend
====================================
Single entry point for the entire app. Does three things:
  1. Serves the frontend (public/index.html + map.png)
  2. Receives BLE scan POSTs from ESP32 base stations at /api/scan
  3. Runs trilateration on incoming data and pushes live badge positions
     to all open browser tabs via SSE

HOW TO RUN:
  source venv/bin/activate
  python server.py

HOW TO TEST (simulates one base station posting scan data):
  curl -X POST http://localhost:5000/api/scan \
       -H "Content-Type: application/json" \
       -d '{"table_id": "table-1", "readings": {"AA:BB:CC:DD:EE:FF": -65}}'

HOW TO TEST WITH FAKE DATA (no hardware needed):
  curl -X POST http://localhost:5000/debug/fake
"""

from flask import Flask, request, jsonify, Response, stream_with_context
from config import base_stations
from positioning import locate_all_badges
import json, socket, queue, threading

app = Flask(__name__, static_folder='public', static_url_path='')

# ── Global state ──────────────────────────────────────────────────────────────

# All raw readings received so far, grouped by badge MAC.
# Structure: { "badge_mac": { "table_id": rssi, ... }, ... }
# Example:   { "AA:BB:CC:DD:EE:FF": { "table-1": -65, "table-2": -72 } }
raw_readings = {}

# The most recent computed badge positions pushed to the browser.
# Kept so new browser tabs get an instant snapshot when they connect.
latest_badges = []

# One Queue per open browser tab. broadcast() drops data into all of them.
client_queues = []
client_queues_lock = threading.Lock()

# ── Frontend routes ───────────────────────────────────────────────────────────

@app.route('/')
def index():
    """Serve the map page."""
    return app.send_static_file('index.html')

# ── SSE stream (/events) ──────────────────────────────────────────────────────

@app.route('/events')
def events():
    """
    Browser connects here once on page load and stays connected forever.
    Every time broadcast() is called, all connected tabs get the new data.
    """
    q = queue.Queue()
    with client_queues_lock:
        client_queues.append(q)
    print(f'[sse] browser connected — {len(client_queues)} client(s)')

    # Send the current state immediately so the page isn't blank on load
    q.put(latest_badges)

    def generate():
        try:
            while True:
                badges = q.get(timeout=30)
                data = json.dumps({'type': 'badges', 'data': badges})
                yield f'data: {data}\n\n'
        except queue.Empty:
            # Send a keepalive comment every 30s so the connection doesn't drop
            yield ': keepalive\n\n'
        except GeneratorExit:
            pass
        finally:
            with client_queues_lock:
                if q in client_queues:
                    client_queues.remove(q)
            print(f'[sse] browser disconnected — {len(client_queues)} client(s)')

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Access-Control-Allow-Origin': '*'
        }
    )

# ── Main data ingestion (/api/scan) ───────────────────────────────────────────

@app.route('/api/scan', methods=['POST'])
def api_scan():
    """
    Receives BLE scan data from one ESP32 base station.

    Expected format (Alex's ESP32 endpoint):
        {
            "table_id": "table-1",
            "readings": {
                "AA:BB:CC:DD:EE:FF": -65,
                "11:22:33:44:55:66": -72
            }
        }

    On each POST:
      1. Store/update raw RSSI readings for each badge from this table
      2. Run trilateration across all stored readings
      3. Push results to all open browser tabs via SSE
    """
    global raw_readings, latest_badges

    data = request.get_json()
    if data is None:
        return jsonify({'error': 'No JSON received'}), 400

    table_id  = data.get('table_id')
    readings  = data.get('readings', {})   # { badge_mac: rssi, ... }

    if not table_id:
        return jsonify({'error': 'Missing table_id'}), 400

    # Store each badge's RSSI as seen by this table
    for badge_mac, rssi in readings.items():
        if badge_mac not in raw_readings:
            raw_readings[badge_mac] = {}
        raw_readings[badge_mac][table_id] = rssi

    print(f'[scan] table={table_id}  badges seen: {list(readings.keys())}')

    # Build kismet_data format that positioning.py expects:
    # [{"base_station_mac": ..., "badge_mac": ..., "rssi": ...}, ...]
    kismet_data = []
    for badge_mac, table_readings in raw_readings.items():
        for t_id, rssi in table_readings.items():
            # Look up the real MAC for this table from config
            station = next((s for s in base_stations if s['table'] == t_id), None)
            if station:
                kismet_data.append({
                    'base_station_mac': station['mac'],
                    'badge_mac':        badge_mac,
                    'rssi':             rssi
                })

    # Run trilateration
    positions = locate_all_badges(kismet_data, base_stations)

    # Build badge list for the frontend
    latest_badges = [
        {
            'id':    mac,
            'x':     pos['x'],
            'y':     pos['y'],
            'label': mac[-4:]          # last 4 chars of MAC shown on map
        }
        for mac, pos in positions.items()
    ]

    broadcast(latest_badges)
    return jsonify({'ok': True, 'badges': len(latest_badges)})


# ── Legacy /stream endpoint (keeps curl test commands working) ────────────────

@app.route('/stream', methods=['POST'])
def stream_legacy():
    """
    Backwards-compatible endpoint matching the original test format:
        [{"mac": "...", "rssi": -65, "reader_id": "table-1"}, ...]

    Converts to the new format and calls api_scan logic directly.
    """
    global raw_readings, latest_badges

    data = request.get_json()
    if data is None:
        return jsonify({'error': 'No JSON received'}), 400

    readings_list = data if isinstance(data, list) else [data]

    for r in readings_list:
        mac       = r.get('mac')
        rssi      = r.get('rssi', -999)
        table_id  = r.get('reader_id')
        if not mac or not table_id:
            continue
        if mac not in raw_readings:
            raw_readings[mac] = {}
        raw_readings[mac][table_id] = rssi

    # Rebuild kismet_data and re-run trilateration (same as api_scan)
    kismet_data = []
    for badge_mac, table_readings in raw_readings.items():
        for t_id, rssi in table_readings.items():
            station = next((s for s in base_stations if s['table'] == t_id), None)
            if station:
                kismet_data.append({
                    'base_station_mac': station['mac'],
                    'badge_mac':        badge_mac,
                    'rssi':             rssi
                })

    positions = locate_all_badges(kismet_data, base_stations)
    latest_badges = [
        {'id': mac, 'x': pos['x'], 'y': pos['y'], 'label': mac[-4:]}
        for mac, pos in positions.items()
    ]

    broadcast(latest_badges)
    return jsonify({'ok': True, 'badges': len(latest_badges)})


# ── Station config routes (from app.py) ───────────────────────────────────────

@app.route('/stations')
def get_stations():
    """Returns current base station config. Useful for debugging."""
    return jsonify(base_stations)

@app.route('/stations/register', methods=['POST'])
def register_station():
    """
    Dynamically add a new base station without restarting the server.
    Body: {"mac": "...", "x": 100, "y": 200, "table": "table-1"}
    """
    data = request.get_json()
    new_station = {
        'mac':   data['mac'],
        'x':     data['x'],
        'y':     data['y'],
        'table': data['table']
    }
    base_stations.append(new_station)
    print(f'[stations] registered: {new_station}')
    return jsonify({'status': 'ok', 'registered': new_station})


# ── Debug / testing routes ────────────────────────────────────────────────────

@app.route('/debug/fake', methods=['POST'])
def debug_fake():
    """
    Injects fake scan data for all 3 configured base stations so you can
    test trilateration without any hardware.
    Hit this with: curl -X POST http://localhost:5000/debug/fake
    """
    from fake_data import fake_kismet_data
    global latest_badges

    positions = locate_all_badges(fake_kismet_data, base_stations)
    latest_badges = [
        {'id': mac, 'x': pos['x'], 'y': pos['y'], 'label': mac[-4:]}
        for mac, pos in positions.items()
    ]
    broadcast(latest_badges)
    print(f'[debug] fake data injected — {len(latest_badges)} badge(s)')
    return jsonify({'ok': True, 'badges': len(latest_badges), 'positions': positions})

@app.route('/badges', methods=['DELETE'])
def clear_badges():
    """Wipes all badge data and clears the map."""
    global raw_readings, latest_badges
    raw_readings  = {}
    latest_badges = []
    broadcast([])
    print('[badges] cleared')
    return jsonify({'ok': True})


# ── Broadcast helper ──────────────────────────────────────────────────────────

def broadcast(badges):
    """Push new badge data to every connected browser tab simultaneously."""
    with client_queues_lock:
        for q in client_queues:
            q.put(badges)


# ── Startup ───────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
        s.close()
    except:
        local_ip = '127.0.0.1'

    PORT = 5000
    print(f'\n✅  Replay Map      →  http://localhost:{PORT}')
    print(f'    On network      →  http://{local_ip}:{PORT}')
    print(f'\n📡  ESP32 endpoint  →  POST http://{local_ip}:{PORT}/api/scan')
    print(f'🧪  Fake data test  →  curl -X POST http://localhost:{PORT}/debug/fake\n')

    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False, threaded=True)