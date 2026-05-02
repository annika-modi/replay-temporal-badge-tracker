"""
Each base station's MAC address, it's (x, y) position on the map, and which table it's at
x and y are just pixel coordinates on a floor plan image - we'll update these with real values after we get the data
"""

base_stations = [
    {"mac": "emulator-table-1", "x": 3.0,  "y": 3.0,  "table": "1"},
    {"mac": "emulator-table-2", "x": 10.0, "y": 3.0,  "table": "2"},
    {"mac": "emulator-table-3", "x": 17.0, "y": 3.0,  "table": "3"},
]