"""
server.py  —  Replay Floor Map Backend
=======================================
Single entry point. Does four things:
  1. Serves the frontend (public/index.html + map images)
  2. Receives BLE scan POSTs from ESP32 base stations at POST /scan
  3. Runs trilateration and pushes live badge positions via SSE
  4. Provides admin endpoints for registering table MACs on event day

HOW TO RUN:
  source venv/bin/activate
  python server.py

HOW TO TEST (fake data via emulator):
  python ble_scanner_emulator.py  →  IP: 127.0.0.1  Port: 5000

ADMIN UI:
  http://localhost:5000/admin
"""

from flask import Flask, request, jsonify, Response, stream_with_context
from positioning import locate_all_badges
import json, socket, queue, threading, time, os

app = Flask(__name__, static_folder='public', static_url_path='')

# ── Room / grid constants ─────────────────────────────────────────────────────
ROOM_W_M  = 100.0   # emulator room width in meters  (update for real hardware)
ROOM_H_M  = 75.0    # emulator room height in meters (update for real hardware)
GRID_W    = 200     # frontend grid width  (0-200)
GRID_H    = 100     # frontend grid height (0-100)

# ── Clustering ────────────────────────────────────────────────────────────────
CLUSTER_GRID = 8    # group badges within this many grid units together

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = 'tables.json'

def load_db():
    with open(DB_PATH, 'r') as f:
        return json.load(f)

def save_db(db):
    with open(DB_PATH, 'w') as f:
        json.dump(db, f, indent=2)

db = load_db()

def get_base_stations():
    """
    Build a list of base stations from tables.json for positioning.py.
    Combines mac_registry (MAC → table name) with floor_plan (table name → x,y).
    """
    stations = []
    for mac, table_name in db['mac_registry'].items():
        pos = db['floor_plan'].get(table_name)
        if pos:
            stations.append({
                'mac':   mac,
                'x':     pos['x'],
                'y':     pos['y'],
                'table': table_name,
                'floor': pos.get('floor', 'level00')
            })
    return stations

# ── Global state ──────────────────────────────────────────────────────────────
raw_readings  = {}   # { badge_mac: { table_name: (rssi, timestamp) } }
latest_badges = []
client_queues = []
client_queues_lock = threading.Lock()

# ── Frontend routes ───────────────────────────────────────────────────────────

@app.route('/')
def index():
    return app.send_static_file('index.html')

@app.route('/admin')
def admin():
    return app.send_static_file('admin.html')

# ── SSE stream ────────────────────────────────────────────────────────────────

@app.route('/events')
def events():
    q = queue.Queue()
    with client_queues_lock:
        client_queues.append(q)
    print(f'[sse] browser connected — {len(client_queues)} client(s)')
    q.put(latest_badges)

    def generate():
        try:
            while True:
                badges = q.get(timeout=30)
                data = json.dumps({'type': 'badges', 'data': badges})
                yield f'data: {data}\n\n'
        except queue.Empty:
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
            'Cache-Control':      'no-cache',
            'X-Accel-Buffering':  'no',
            'Access-Control-Allow-Origin': '*'
        }
    )

# ── Main scan endpoint ────────────────────────────────────────────────────────

@app.route('/scan', methods=['POST'])
def scan():
    """
    Receives BLE scan data from one ESP32 base station.

    Emulator format:
        {"table": {"1": {"AC:DE:48:...": -67.2, ...}}}

    Real Kismet format (TBC with Alex):
        {"AC:DE:48:...": -67.2, ...}   posted from a known table MAC

    On each POST:
      1. Identify which table sent this (via MAC registry)
      2. Store RSSI readings with timestamp
      3. Expire readings older than 15s
      4. Trilaterate all badge positions
      5. Cluster nearby badges
      6. Broadcast to all open browser tabs
    """
    global raw_readings, latest_badges

    data = request.get_json()
    if data is None:
        return jsonify({'error': 'No JSON received'}), 400

    now = time.time()

    # ── Detect format ──────────────────────────────────────────────────────
    if 'table' in data:
        # Emulator format: {"table": {"1": {mac: rssi}}}
        table_dict = data['table']
        table_id   = str(list(table_dict.keys())[0])
        readings   = table_dict[table_id]
        # Look up table name from emulator ID (e.g. "1" → "table-1")
        table_name = f"table-{table_id}"
    else:
        # Real Kismet format: flat {mac: rssi} — identify table by sender IP or MAC
        # For now use the source IP to look up which table this is
        sender_ip  = request.remote_addr
        table_name = db['mac_registry'].get(sender_ip, 'unknown')
        readings   = data

    # ── Store readings with timestamp ──────────────────────────────────────
    for badge_mac, rssi in readings.items():
        if badge_mac not in raw_readings:
            raw_readings[badge_mac] = {}
        raw_readings[badge_mac][table_name] = (rssi, now)

    print(f'[scan] table={table_name}  badges seen={len(readings)}')

    # ── Build kismet_data, filtering stale readings (>15s old) ────────────
    base_stations = get_base_stations()
    station_map   = {s['table']: s for s in base_stations}

    kismet_data = []
    for badge_mac, table_readings in raw_readings.items():
        for t_name, (rssi, ts) in table_readings.items():
            if now - ts > 15:
                continue   # skip stale
            station = station_map.get(t_name)
            if station:
                kismet_data.append({
                    'base_station_mac': station['mac'],
                    'badge_mac':        badge_mac,
                    'rssi':             rssi
                })

    # ── Trilaterate ────────────────────────────────────────────────────────
    positions = locate_all_badges(kismet_data, base_stations)

    # ── Scale meter coords → grid coords ──────────────────────────────────
    # Build a lookup: badge_mac → which floor it was most recently seen on
# (use the table with the strongest RSSI reading)
    badge_floor = {}
    for badge_mac, table_readings in raw_readings.items():
        best_rssi  = -999
        best_floor = 'level00'
        for t_name, (rssi, ts) in table_readings.items():
            if now - ts > 15:
                continue
            station = station_map.get(t_name)
            if station and rssi > best_rssi:
                best_rssi  = rssi
                best_floor = station.get('floor', 'level00')
        badge_floor[badge_mac] = best_floor

    all_badges = [
        {
            'id':    mac,
            'x':     round((pos['x'] / ROOM_W_M) * GRID_W, 1),
            'y':     round((pos['y'] / ROOM_H_M) * GRID_H, 1),
            'label': mac[-4:],
            'floor': badge_floor.get(mac, 'level00')
        }
        for mac, pos in positions.items()
    ]

    # ── Cluster nearby badges ──────────────────────────────────────────────
    latest_badges = cluster_badges(all_badges)

    broadcast(latest_badges)
    return jsonify({'ok': True, 'badges': len(all_badges), 'clusters': len(latest_badges)})


# ── Clustering ────────────────────────────────────────────────────────────────

def cluster_badges(badges, grid_size=CLUSTER_GRID):
    """
    Groups badges that are close together into clusters.
    Each cluster shows a count instead of individual dots.
    Dramatically reduces frontend rendering load for 2000+ badges.
    """
    clusters = {}
    for b in badges:
        cx = round(b['x'] / grid_size) * grid_size
        cy = round(b['y'] / grid_size) * grid_size
        key = (cx, cy, b.get('floor', 'level00'))
        if key not in clusters:
            clusters[key] = {
                'id':    f'cluster-{cx}-{cy}',
                'x':     cx,
                'y':     cy,
                'count': 0,
                'floor': b.get('floor', 'level00'),
                'label': ''
            }
        clusters[key]['count'] += 1

    for c in clusters.values():
        c['label'] = str(c['count']) if c['count'] > 1 else ''

    return list(clusters.values())


# ── Admin / registration endpoints ────────────────────────────────────────────

@app.route('/register', methods=['POST'])
def register():
    """
    Register a table ESP32's MAC address to a table on the floor plan.
    Body: {"mac": "FF:FF:FF:0F:03:0A", "table": "table-1"}
    Alex calls this on event day as he places each ESP32.
    Persists to tables.json so it survives server restarts.
    """
    global db
    data  = request.get_json()
    mac   = data.get('mac', '').upper().strip()
    table = data.get('table', '').strip()

    if not mac or not table:
        return jsonify({'error': 'mac and table are required'}), 400
    if table not in db['floor_plan']:
        return jsonify({'error': f'unknown table: {table}'}), 400

    db['mac_registry'][mac] = table
    save_db(db)
    print(f'[register] {mac} → {table}')
    return jsonify({'ok': True, 'mac': mac, 'table': table})

@app.route('/register', methods=['DELETE'])
def unregister():
    """Remove a MAC from the registry."""
    global db
    data = request.get_json()
    mac  = data.get('mac', '').upper().strip()
    if mac in db['mac_registry']:
        del db['mac_registry'][mac]
        save_db(db)
    return jsonify({'ok': True})

@app.route('/tables')
def get_tables():
    """Returns full table config — floor plan + current MAC registrations."""
    global db
    db = load_db()
    result = []
    for table_name, pos in db['floor_plan'].items():
        mac = next((m for m, t in db['mac_registry'].items() if t == table_name), None)
        result.append({
            'table': table_name,
            'x':     pos['x'],
            'y':     pos['y'],
            'floor': pos.get('floor', 'level00'),
            'mac':   mac or ''
        })
    return jsonify(result)

@app.route('/badges', methods=['DELETE'])
def clear_badges():
    global raw_readings, latest_badges
    raw_readings  = {}
    latest_badges = []
    broadcast([])
    print('[badges] cleared')
    return jsonify({'ok': True})

# ── Debug ─────────────────────────────────────────────────────────────────────

@app.route('/debug/fake', methods=['POST'])
def debug_fake():
    from fake_data import fake_kismet_data
    global latest_badges
    base_stations = get_base_stations()
    positions     = locate_all_badges(fake_kismet_data, base_stations)
    all_badges    = [
        {'id': mac, 'x': pos['x'], 'y': pos['y'], 'label': mac[-4:], 'floor': 'level00'}
        for mac, pos in positions.items()
    ]
    latest_badges = cluster_badges(all_badges)
    broadcast(latest_badges)
    return jsonify({'ok': True, 'badges': len(all_badges)})

# ── Broadcast ─────────────────────────────────────────────────────────────────

def broadcast(badges):
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
    print(f'\n✅  Replay Map   →  http://localhost:{PORT}')
    print(f'    On network  →  http://{local_ip}:{PORT}')
    print(f'🛠   Admin UI    →  http://localhost:{PORT}/admin')
    print(f'📡  ESP32 POST  →  http://{local_ip}:{PORT}/scan\n')

    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False, threaded=True)