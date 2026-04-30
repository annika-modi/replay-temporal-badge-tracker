"""
This simulates the data Kismet would give us from 3 base stations
Each entry means: "this base station saw this badge at this signal strength"
Signal (RSSI) is in dBm - closer to 0 means closer in distance (e.g. -50 is close, -90 is far)
"""

fake_kismet_data = [
    {"base_station_mac": "AA:BB:CC:DD:EE:01", "badge_mac": "11:22:33:44:55:66", "rssi": -55},
    {"base_station_mac": "AA:BB:CC:DD:EE:02", "badge_mac": "11:22:33:44:55:66", "rssi": -72},
    {"base_station_mac": "AA:BB:CC:DD:EE:03", "badge_mac": "11:22:33:44:55:66", "rssi": -80},
]