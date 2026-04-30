import math
import numpy as np

def rssi_to_distance(rssi, tx_power=-59):
    """
    Converts signal strength (RSSI in dBm) to estimated distance in meters.
    tx_power is the expected RSSI reading at exactly 1 meter away. 
    We'll calibrate this constant when we have real hardware.
    """
    
    if rssi >= 0:
        return 0.0
    ratio = rssi/tx_power
    
    if ratio < 1.0:
        return math.pow(ratio, 10)
    else:
        return (0.89976) * math.pow(ratio, 7.7095) + 0.111
    

def trilaterate(station_readings):
    """
    Estimates a badge's (x, y) position from signal readings at known stations.
    
    station readings: list of dicts like: 
        [{"x": 100, "y": 200, "distance": 2.3}]
    
    Needs at least 2 stations, works best with 3+.
    Returns (x, y) tuple or None if not enough data.
    
    How it works:
    Each station gives you a circle equation: (x - sx)^2 + (y - sy)^2 = d^2
    If you subtract the first station's equation from each of the others, the squared x and y terms cancel out, leaving plain linear equations.
    That system of linear equations is: A * [x, y] = b
    
    numpy solves it with lstsq (least squares - handles more than 2 stations).
    """
    
    if len(station_readings) < 2:
        return None
    
    # Use the first station as our reference point to subtract from
    ref = station_readings[0]
    x1, y1, d1 = ref["x"], ref["y"], ref["distance"]
    
    A_rows = []
    b_rows = []
    
    for s in station_readings[1:]:
        x2, y2, d2 = s["x"], s["y"], s["distance"]
        
        # This is the result of subtracting circle equations and simplifying:
        #  2*(x2 - x1)*x + 2*(y2 - y1)*y = d1^2 - d2^2 + x2^2 - x1^2 + y2^2 - y1^2
        
        A_rows.append([2 * (x2 - x1),  2 * (y2 - y1)])
        b_rows.append(d1**2 - d2**2 + x2**2 - x1**2 + y2**2 - y1**2)
        
    A = np.array(A_rows, dtype=float)
    b = np.array(b_rows, dtype=float)
    
    # lstsq = "least squares" - finds the best-fit solution even if equations
    # are slightly contradictory (which they always are with noisy RSSI data)
    
    lstsq_out = np.linalg.lstsq(A, b, rcond=None)
    result = lstsq_out[0]
    
    return round(float(result[0]), 1), round(float(result[1]), 1)

def locate_all_badges(kismet_data, base_stations):
    """
    Top-level function. Takes raw readings + station config, returns badge positions.
    
    kismet_data: [{"base_station_mac": ..., "badge_mac": ..., "rssi": ...}, ...]
    base_stations: [{"mac": ..., "x": ..., "y": ..., "table": ...}, ...]
    
    Returns: {"badge_mac": {"x": ..., "y": ...}, ...}
    """
    
    # Build a fast lookup dict: MAC address -> station info
    station_map = {s["mac"]: s for s in base_stations}
    
    # Group readings by badge MAC
    badge_readings = {}
    for reading in kismet_data:
        badge = reading["badge_mac"]
        station = station_map.get(reading["base_station_mac"])
        
        if station is None:
            continue # reading from unregistered station, skip
        
        distance = rssi_to_distance(reading["rssi"])
        
        if badge not in badge_readings:
            badge_readings[badge] = []
        
        badge_readings[badge].append({"x": station["x"], "y": station["y"], "distance": distance})
        
    # Trilaterate each badge
    results = {}
    for badge_mac, readings in badge_readings.items():
        position = trilaterate(readings)
        if position:
            results[badge_mac] = {"x": position[0], "y": position[1]}
    
    return results
        
    
    
    
