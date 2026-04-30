"""
Each base station's MAC address, it's (x, y) position on the map, and which table it's at
x and y are just pixel coordinates on a floor plan image - we'll update these with real values after we get the data
"""

base_stations = [
    {"mac": "AA:BB:CC:DD:EE:01", "x": 100, "y": 200, "table": "Booth 1"},
    {"mac": "AA:BB:CC:DD:EE:02", "x": 300, "y": 200, "table": "Booth 2"},
    {"mac": "AA:BB:CC:DD:EE:03", "x": 200, "y": 400, "table": "Booth 3"},
]