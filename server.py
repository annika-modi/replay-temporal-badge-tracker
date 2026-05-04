"""
server.py  —  Replay Floor Map Backend
=======================================
Single entry point. Does four things:
  1. Serves the frontend (public/index.html + map images)
  2. Receives BLE scan POSTs from ESP32 base stations
  3. Runs trilateration and pushes live badge positions via SSE
  4. Provides admin endpoints for managing table positions

ESP32 Firmware Endpoints:
  POST /enroll      — ESP32 registers itself on boot
  POST /scanreport  — ESP32 posts BLE scan results every 5s

Emulator Endpoint (backward compat):
  POST /scan        — legacy emulator format still works

HOW TO RUN:
  source venv/bin/activate
  python server.py           # normal (preserves scans.db across restarts)
  python server.py --fresh   # wipe scans.db for a clean test run

DEBUG:
  http://localhost:5000/debug/scans     — last 100 raw scan rows
  http://localhost:5000/debug/enrolled  — enrolled ESP32s
  http://localhost:5000/debug/memory    — current in-memory badge state
"""

from flask import Flask, request, jsonify, Response, stream_with_context
from positioning import locate_all_badges
import json, socket, queue, threading, time, os, sqlite3, argparse
import requests

app = Flask(__name__, static_folder='public', static_url_path='')

# ── Room / grid constants ─────────────────────────────────────────────────────
ROOM_W_M  = 100.0   # venue width in meters
ROOM_H_M  = 75.0    # venue height in meters
GRID_W    = 200     # frontend grid width  (0-200)
GRID_H    = 100     # frontend grid height (0-100)

CLUSTER_GRID  = 12  # group badges within this many grid units
STALE_SECONDS = 15  # ignore readings older than this for trilateration

DB_PATH     = 'scans.db'
TABLES_PATH = 'tables.json'

# ── brooklyn.party device registry ────────────────────────────────────────────
# Pollled on a background thread; each entry is whatever the upstream returns
# (mac_address, loc, created_at, updated_at, x, y).
BROOKLYN_BASE       = 'https://brooklyn.party'
BROOKLYN_ENDPOINT   = '/api/v1/devices'
BROOKLYN_POLL_SECS  = 5    # how often the background thread refreshes
BROOKLYN_TIMEOUT    = 4

brooklyn_devices       = []      # list of dicts from upstream
brooklyn_last_fetch    = None    # epoch seconds of last successful fetch
brooklyn_last_error    = None
brooklyn_lock          = threading.Lock()


# ── SQLite ────────────────────────────────────────────────────────────────────

def init_db():
    """
    Create all tables on first run. Safe to re-run — uses IF NOT EXISTS.

    enrolled  — one row per ESP32, written by POST /enroll
    scans     — one row per badge observation, written by POST /scanreport
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS enrolled (
            esp32_mac   TEXT PRIMARY KEY,
            table_uid   INTEGER NOT NULL,
            enrolled_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scans (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   REAL    NOT NULL,
            table_uid   INTEGER NOT NULL,
            badge_mac   TEXT    NOT NULL,
            ble_uid     TEXT,
            rssi        REAL    NOT NULL,
            tx_power    REAL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_scans_badge_table
        ON scans (badge_mac, table_uid, timestamp DESC)
    """)
    conn.commit()
    conn.close()
    print('[db] scans.db initialized')


def write_scan_batch(table_uid, scan_entries, server_ts):
    """Insert a batch of scan rows in one transaction."""
    conn = sqlite3.connect(DB_PATH)
    for entry in scan_entries:
        conn.execute("""
            INSERT INTO scans (timestamp, table_uid, badge_mac, ble_uid, rssi, tx_power)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            server_ts,
            table_uid,
            entry['mac'].upper(),
            entry.get('ble_uid'),
            float(entry['rssi']),
            entry.get('tx_power')
        ))
    conn.commit()
    conn.close()


def load_recent_scans_from_db():
    """
    On startup, restore the last STALE_SECONDS of scans into memory.
    badge_id = ble_uid if available, else badge_mac.
    Returns { badge_id: { table_uid: (rssi, timestamp) } }
    """
    cutoff = time.time() - STALE_SECONDS
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT badge_mac, ble_uid, table_uid, rssi, timestamp
        FROM scans WHERE timestamp > ?
        ORDER BY timestamp ASC
    """, (cutoff,)).fetchall()
    conn.close()

    restored = {}
    for row in rows:
        badge_id = row['ble_uid'] if row['ble_uid'] else row['badge_mac']
        tuid     = row['table_uid']
        if badge_id not in restored:
            restored[badge_id] = {}
        restored[badge_id][tuid] = (row['rssi'], row['timestamp'])

    print(f'[db] restored {len(restored)} badges from recent scans')
    return restored


# ── tables.json ───────────────────────────────────────────────────────────────

def load_tables():
    with open(TABLES_PATH, 'r') as f:
        return json.load(f)

def save_tables(data):
    with open(TABLES_PATH, 'w') as f:
        json.dump(data, f, indent=2)

tables_cfg = load_tables()


def get_base_stations():
    """
    Build the base station list for positioning.py.

    Real hardware: uses uid_registry (table_uid int → table name → x,y)
    Emulator:      uses floor_plan table names directly as fake identifiers

    Coordinates scaled from grid (0-200 x 0-100) to meters.
    """
    stations = []
    seen = set()
    uid_map = tables_cfg.get('uid_registry', {})  # {"12": "table-1", ...}

    for uid_str, table_name in uid_map.items():
        pos = tables_cfg['floor_plan'].get(table_name)
        if pos:
            stations.append({
                'mac':   int(uid_str),          # table_uid as identifier
                'x':     pos['x'] / GRID_W * ROOM_W_M,
                'y':     pos['y'] / GRID_H * ROOM_H_M,
                'table': table_name,
                'floor': pos.get('floor', 'level00'),
                'uid':   int(uid_str)
            })
            seen.add(table_name)

    # Emulator fallback — table name as fake MAC
    for table_name, pos in tables_cfg['floor_plan'].items():
        if table_name not in seen:
            stations.append({
                'mac':   table_name,
                'x':     pos['x'] / GRID_W * ROOM_W_M,
                'y':     pos['y'] / GRID_H * ROOM_H_M,
                'table': table_name,
                'floor': pos.get('floor', 'level00'),
                'uid':   None
            })

    return stations


# ── Global state ──────────────────────────────────────────────────────────────
# raw_readings: { badge_id: { table_uid_or_name: (rssi, timestamp) } }
raw_readings    = {}
latest_badges   = []
client_queues   = []
client_queues_lock = threading.Lock()
enrolled_tables = {}   # { esp32_mac: table_uid }


# ── Frontend ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return app.send_static_file('index.html')

@app.route('/admin')
def admin():
    return app.send_static_file('admin.html')


# ── SSE ───────────────────────────────────────────────────────────────────────

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
                yield f'data: {json.dumps({"type": "badges", "data": badges})}\n\n'
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
            'Cache-Control':     'no-cache',
            'X-Accel-Buffering': 'no',
            'Access-Control-Allow-Origin': '*'
        }
    )


# ── POST /enroll ──────────────────────────────────────────────────────────────

@app.route('/enroll', methods=['POST'])
def enroll():
    """
    Called once by each ESP32 on boot after WiFi connects.
    Payload: {"mac": "AA:BB:CC:DD:EE:FF", "table_uid": 12}

    Registers ESP32 WiFi MAC → table_uid so we know which
    physical table each scanner is sitting at.
    """
    global tables_cfg
    data = request.get_json()
    if not data or 'mac' not in data or 'table_uid' not in data:
        return jsonify({'error': 'Need mac and table_uid'}), 400

    esp32_mac = data['mac'].upper().strip()
    table_uid = int(data['table_uid'])

    # Memory
    enrolled_tables[esp32_mac] = table_uid

    # SQLite
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO enrolled (esp32_mac, table_uid, enrolled_at)
        VALUES (?, ?, ?)
        ON CONFLICT(esp32_mac) DO UPDATE
        SET table_uid=excluded.table_uid, enrolled_at=excluded.enrolled_at
    """, (esp32_mac, table_uid, time.time()))
    conn.commit()
    conn.close()

    # Update uid_registry in tables.json if needed
    uid_str = str(table_uid)
    if 'uid_registry' not in tables_cfg:
        tables_cfg['uid_registry'] = {}
    if uid_str not in tables_cfg['uid_registry']:
        # Auto-placeholder — update uid_registry in tables.json to assign a real table position
        placeholder = f'table-uid-{table_uid}'
        tables_cfg['uid_registry'][uid_str] = placeholder
        save_tables(tables_cfg)
        print(f'[enroll] {esp32_mac} → uid {table_uid} '
              f'(new placeholder "{placeholder}" — add x,y to floor_plan)')
    else:
        print(f'[enroll] {esp32_mac} → uid {table_uid} '
              f'({tables_cfg["uid_registry"][uid_str]})')

    return jsonify({'ok': True, 'table_uid': table_uid}), 200


# ── POST /scanreport ──────────────────────────────────────────────────────────

@app.route('/scanreport', methods=['POST'])
def scanreport():
    """
    Called every 5s by each ESP32 with BLE scan results.

    Payload:
    {
      "table_uid": 12,
      "timestamp": "2026-05-02T14:23:05Z",
      "scans": [
        {"ble_uid": "uuid:major:minor", "mac": "c1:9d:...", "rssi": -67, "tx_power": -59},
        {"ble_uid": null,               "mac": "5e:a3:...", "rssi": -82, "tx_power": null}
      ]
    }

    badge_id = ble_uid if available (stable), else mac (may be randomized)
    """
    global raw_readings

    data = request.get_json()
    if not data or 'table_uid' not in data or 'scans' not in data:
        return jsonify({'error': 'Need table_uid and scans'}), 400

    table_uid = int(data['table_uid'])
    scan_list = data['scans']
    server_ts = time.time()

    # Persist to SQLite
    write_scan_batch(table_uid, scan_list, server_ts)

    # Update in-memory readings
    for entry in scan_list:
        badge_id = entry.get('ble_uid') or entry['mac'].upper()
        if badge_id not in raw_readings:
            raw_readings[badge_id] = {}
        raw_readings[badge_id][table_uid] = (float(entry['rssi']), server_ts)

    print(f'[scanreport] table_uid={table_uid}  badges={len(scan_list)}')

    _run_trilateration()
    return jsonify({'ok': True, 'received': len(scan_list)}), 200


# ── POST /scan (emulator backward compat) ─────────────────────────────────────

@app.route('/scan', methods=['POST'])
def scan_legacy():
    """
    Legacy emulator format: {"table": {"1": {"MAC": rssi}}}
    Kept so the emulator still works without changes.
    """
    global raw_readings

    data = request.get_json()
    if not data or 'table' not in data:
        return jsonify({'error': 'expected emulator format'}), 400

    table_dict = data['table']
    table_id   = str(list(table_dict.keys())[0])
    table_name = f'table-{table_id}'
    server_ts  = time.time()
    readings   = table_dict[table_id]

    for badge_mac, rssi in readings.items():
        badge_mac = badge_mac.upper()
        if badge_mac not in raw_readings:
            raw_readings[badge_mac] = {}
        raw_readings[badge_mac][table_name] = (float(rssi), server_ts)

    print(f'[scan/emulator] table={table_name}  badges={len(readings)}')

    _run_trilateration()
    return jsonify({'ok': True, 'badges': len(readings)}), 200


# ── Trilateration ─────────────────────────────────────────────────────────────

def _run_trilateration():
    """
    Shared by /scanreport and /scan (emulator).
    1. Expire stale readings
    2. Build kismet_data
    3. Trilaterate
    4. Scale + clamp to grid
    5. Assign floor
    6. Cluster + broadcast
    """
    global raw_readings, latest_badges

    now    = time.time()
    cutoff = now - STALE_SECONDS

    # Expire stale
    for bid in list(raw_readings.keys()):
        raw_readings[bid] = {
            t: (r, ts) for t, (r, ts) in raw_readings[bid].items()
            if ts > cutoff
        }
        if not raw_readings[bid]:
            del raw_readings[bid]

    base_stations   = get_base_stations()
    station_by_uid  = {s['uid']:   s for s in base_stations if s['uid'] is not None}
    station_by_name = {s['table']: s for s in base_stations}

    kismet_data = []
    for badge_id, table_readings in raw_readings.items():
        for table_key, (rssi, ts) in table_readings.items():
            station = (station_by_uid.get(table_key)
                       if isinstance(table_key, int)
                       else station_by_name.get(table_key))
            if station:
                kismet_data.append({
                    'base_station_mac': station['mac'],
                    'badge_mac':        badge_id,
                    'rssi':             rssi
                })

    positions = locate_all_badges(kismet_data, base_stations)

    # Floor assignment — use table with strongest signal
    badge_floor = {}
    for badge_id, table_readings in raw_readings.items():
        best_rssi  = -999
        best_floor = 'level00'
        for table_key, (rssi, ts) in table_readings.items():
            station = (station_by_uid.get(table_key)
                       if isinstance(table_key, int)
                       else station_by_name.get(table_key))
            if station and rssi > best_rssi:
                best_rssi  = rssi
                best_floor = station.get('floor', 'level00')
        badge_floor[badge_id] = best_floor

    all_badges = []
    for badge_id, pos in positions.items():
        x = max(0, min(GRID_W, round((pos['x'] / ROOM_W_M) * GRID_W, 1)))
        y = max(0, min(GRID_H, round((pos['y'] / ROOM_H_M) * GRID_H, 1)))
        all_badges.append({
            'id':    badge_id,
            'x':     x,
            'y':     y,
            'label': badge_id[-4:],
            'floor': badge_floor.get(badge_id, 'level00')
        })

    latest_badges = cluster_badges(all_badges)
    broadcast(latest_badges)

# ── Closest table ─────────────────────────────────────────────────────────────

def get_closest_table(badge_id):
    """
    Returns the table a badge is closest to, based on strongest RSSI reading.

    Strongest RSSI (least negative) = physically closest table.
    This is simpler and often more reliable than trilateration for the
    "which booth is this person at" question.

    Returns a dict like:
      {"badge_id": "...", "table": "table-3", "table_uid": 3, "rssi": -67, "floor": "level00"}
    or None if the badge has no recent readings.
    """
    if badge_id not in raw_readings or not raw_readings[badge_id]:
        return None

    base_stations = get_base_stations()
    station_by_uid  = {s['uid']:   s for s in base_stations if s['uid'] is not None}
    station_by_name = {s['table']: s for s in base_stations}

    best_rssi    = -9999
    best_station = None

    for table_key, (rssi, ts) in raw_readings[badge_id].items():
        station = (station_by_uid.get(table_key)
                   if isinstance(table_key, int)
                   else station_by_name.get(table_key))
        if station and rssi > best_rssi:
            best_rssi    = rssi
            best_station = station

    if not best_station:
        return None

    return {
        'badge_id':  badge_id,
        'table':     best_station['table'],
        'table_uid': best_station['uid'],
        'rssi':      best_rssi,
        'floor':     best_station.get('floor', 'level00'),
    }


def get_all_closest_tables():
    """
    Returns closest table for every currently active badge.
    Used by GET /closest to power the "who is at which booth" view.
    """
    results = []
    for badge_id in raw_readings:
        entry = get_closest_table(badge_id)
        if entry:
            results.append(entry)

    # Sort by table so organizers can see all badges grouped by booth
    results.sort(key=lambda x: (x['floor'], x['table']))
    return results


# ── GET /closest ──────────────────────────────────────────────────────────────

@app.route('/closest', methods=['GET'])
def closest():
    """
    Returns the closest table for every active badge.

    Optional query params:
      floor  — filter by floor name (e.g. ?floor=level00)
      table  — filter by table name (e.g. ?table=table-3)

    Response:
    [
      {"badge_id": "c1:9d:...", "table": "table-3", "table_uid": 3, "rssi": -67, "floor": "level00"},
      ...
    ]
    """
    results = get_all_closest_tables()

    floor_filter = request.args.get('floor')
    table_filter = request.args.get('table')

    if floor_filter:
        results = [r for r in results if r['floor'] == floor_filter]
    if table_filter:
        results = [r for r in results if r['table'] == table_filter]

    return jsonify(results), 200


# ── GET /closest/<badge_id> ───────────────────────────────────────────────────

@app.route('/closest/<path:badge_id>', methods=['GET'])
def closest_one(badge_id):
    """
    Returns the closest table for a single badge.

    Example: GET /closest/c1:9d:4b:22:08:01
    """
    entry = get_closest_table(badge_id)
    if not entry:
        return jsonify({'error': f'No active readings for badge {badge_id}'}), 404
    return jsonify(entry), 200


# ── Clustering ────────────────────────────────────────────────────────────────

def cluster_badges(badges, grid_size=CLUSTER_GRID):
    clusters = {}
    for b in badges:
        cx  = round(b['x'] / grid_size) * grid_size
        cy  = round(b['y'] / grid_size) * grid_size
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


# ── Broadcast ─────────────────────────────────────────────────────────────────

def broadcast(badges):
    with client_queues_lock:
        for q in client_queues:
            q.put(badges)


# ── brooklyn.party poller ─────────────────────────────────────────────────────

def _fetch_brooklyn_once():
    """One GET against brooklyn.party. Caller already inside the thread loop."""
    global brooklyn_devices, brooklyn_last_fetch, brooklyn_last_error
    url = BROOKLYN_BASE + BROOKLYN_ENDPOINT
    try:
        r = requests.get(url, timeout=BROOKLYN_TIMEOUT)
        r.raise_for_status()
        payload = r.json()
        # Schema: {"devices": [{mac_address, loc, created_at, updated_at, x, y}, ...]}
        devices = payload.get('devices', payload) if isinstance(payload, dict) else payload
        if not isinstance(devices, list):
            raise ValueError(f'unexpected payload shape: {type(devices).__name__}')
        with brooklyn_lock:
            brooklyn_devices    = devices
            brooklyn_last_fetch = time.time()
            brooklyn_last_error = None
        print(f'[brooklyn] {len(devices)} device(s) registered')
    except Exception as e:
        with brooklyn_lock:
            brooklyn_last_error = str(e)
        print(f'[brooklyn] fetch failed: {e}')


def _brooklyn_loop():
    while True:
        _fetch_brooklyn_once()
        time.sleep(BROOKLYN_POLL_SECS)


def start_brooklyn_poller():
    t = threading.Thread(target=_brooklyn_loop, daemon=True, name='brooklyn-poller')
    t.start()
    print(f'[brooklyn] poller started '
          f'(every {BROOKLYN_POLL_SECS}s -> {BROOKLYN_BASE}{BROOKLYN_ENDPOINT})')


# ── GET /devices  (cached brooklyn.party snapshot) ────────────────────────────

@app.route('/devices')
def get_devices_route():
    """
    Returns the most recent snapshot of brooklyn.party's device list along
    with the freshness of that snapshot.
    """
    with brooklyn_lock:
        return jsonify({
            'fetched_at': brooklyn_last_fetch,
            'age_s':      (time.time() - brooklyn_last_fetch) if brooklyn_last_fetch else None,
            'error':      brooklyn_last_error,
            'devices':    list(brooklyn_devices),
        })


# ── Admin endpoints ───────────────────────────────────────────────────────────

@app.route('/tables')
def get_tables_route():
    """
    Hardcoded table positions from tables.json, joined with brooklyn.party
    registration info (matched by loc == table_uid).
    """
    cfg     = load_tables()
    uid_map = cfg.get('uid_registry', {})

    # Build loc -> brooklyn record lookup
    with brooklyn_lock:
        bk_by_loc = {}
        for d in brooklyn_devices:
            try:
                bk_by_loc[int(d.get('loc'))] = d
            except (TypeError, ValueError):
                continue

    result = []
    for table_name, pos in cfg['floor_plan'].items():
        uid_str = next((k for k, v in uid_map.items() if v == table_name), None)
        uid_int = int(uid_str) if uid_str is not None else None
        bk = bk_by_loc.get(uid_int) if uid_int is not None else None
        result.append({
            'table':       table_name,
            'x':           pos['x'],
            'y':           pos['y'],
            'floor':       pos.get('floor', 'level00'),
            'uid':         uid_str or '',
            'registered':  bk is not None,
            'mac_address': bk.get('mac_address') if bk else None,
            'updated_at':  bk.get('updated_at')  if bk else None,
            'created_at':  bk.get('created_at')  if bk else None,
        })
    return jsonify(result)

@app.route('/badges', methods=['DELETE'])
def clear_badges():
    global raw_readings, latest_badges
    raw_readings  = {}
    latest_badges = []
    broadcast([])
    print('[badges] cleared in-memory')
    return jsonify({'ok': True})


# ── Debug endpoints ───────────────────────────────────────────────────────────

@app.route('/debug/scans')
def debug_scans():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute('SELECT * FROM scans ORDER BY id DESC LIMIT 100').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/debug/enrolled')
def debug_enrolled():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute('SELECT * FROM enrolled ORDER BY enrolled_at DESC').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/debug/memory')
def debug_memory():
    return jsonify({
        'badge_count': len(raw_readings),
        'badges': {bid: list(tables.keys()) for bid, tables in raw_readings.items()}
    })


# ── Startup ───────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--fresh', action='store_true',
                        help='Wipe scans.db before starting (clean test run)')
    args = parser.parse_args()

    if args.fresh and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print('[db] wiped for fresh run')

    init_db()
    raw_readings = load_recent_scans_from_db()
    start_brooklyn_poller()

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = '127.0.0.1'

    PORT = 5000
    print(f'\n  Replay Map    →  http://localhost:{PORT}')
    print(f'  On network    →  http://{local_ip}:{PORT}')
    print(f'  ESP32 enroll  →  POST http://{local_ip}:{PORT}/enroll')
    print(f'  ESP32 scan    →  POST http://{local_ip}:{PORT}/scanreport')
    print(f'  Debug scans   →  http://localhost:{PORT}/debug/scans')
    print(f'  Debug memory  →  http://localhost:{PORT}/debug/memory')
    print(f'  Enrolled      →  http://localhost:{PORT}/debug/enrolled')
    print(f'  Brooklyn      →  http://localhost:{PORT}/devices')
    print(f'  Tables (live) →  http://localhost:{PORT}/tables\n')

    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False, threaded=True)