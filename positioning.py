import math
import numpy as np


def rssi_to_distance(rssi, tx_power=-59):
    """
    Converts RSSI (dBm) to an estimated distance in meters.

    tx_power: expected RSSI at 1 meter. Default -59 dBm is standard for
              iBeacon hardware; adjust based on measured calibration data.
    """
    if rssi >= 0:
        return 0.0

    ratio = rssi / tx_power

    if ratio < 1.0:
        return math.pow(ratio, 10)
    else:
        return (0.89976) * math.pow(ratio, 7.7095) + 0.111


def trilaterate(station_readings):
    """
    Estimates a badge's (x, y) position from distance readings at known stations.

    station_readings: list of dicts:
        [{"x": 100, "y": 200, "distance": 2.3}, ...]

    Requires at least 2 stations; accuracy improves with 3 or more.
    Returns (x, y) tuple, or None if insufficient data.

    Method:
      Each station defines a circle: (x - sx)^2 + (y - sy)^2 = d^2
      Subtracting the reference station's equation from each subsequent one
      cancels the squared terms, yielding a linear system A * [x, y] = b.
      numpy.linalg.lstsq solves this in the least-squares sense, which
      handles the overdetermined case (3+ stations) and noisy RSSI data.
    """
    if len(station_readings) < 2:
        return None

    ref = station_readings[0]
    x1, y1, d1 = ref["x"], ref["y"], ref["distance"]

    A_rows = []
    b_rows = []

    for s in station_readings[1:]:
        x2, y2, d2 = s["x"], s["y"], s["distance"]
        A_rows.append([2 * (x2 - x1), 2 * (y2 - y1)])
        b_rows.append(d1**2 - d2**2 + x2**2 - x1**2 + y2**2 - y1**2)

    A = np.array(A_rows, dtype=float)
    b = np.array(b_rows, dtype=float)

    result = np.linalg.lstsq(A, b, rcond=None)[0]

    return round(float(result[0]), 1), round(float(result[1]), 1)


def locate_all_badges(kismet_data, base_stations):
    """
    Top-level function: takes raw scan readings and station positions,
    returns estimated positions for all observed badges.

    kismet_data:   [{"base_station_mac": ..., "badge_mac": ..., "rssi": ...}, ...]
    base_stations: [{"mac": ..., "x": ..., "y": ..., "table": ...}, ...]

    Returns: {"badge_mac": {"x": ..., "y": ...}, ...}
    """
    station_map = {s["mac"]: s for s in base_stations}

    badge_readings = {}
    for reading in kismet_data:
        badge   = reading["badge_mac"]
        station = station_map.get(reading["base_station_mac"])

        if station is None:
            continue  # reading from an unregistered station — skip

        distance = rssi_to_distance(reading["rssi"])

        if badge not in badge_readings:
            badge_readings[badge] = []

        badge_readings[badge].append({
            "x": station["x"],
            "y": station["y"],
            "distance": distance
        })

    results = {}
    for badge_mac, readings in badge_readings.items():
        position = trilaterate(readings)
        if position:
            results[badge_mac] = {"x": position[0], "y": position[1]}

    return results