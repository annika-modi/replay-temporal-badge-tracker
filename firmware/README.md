# BLE-Replay-Firmware

Firmware for Seeed XIAO ESP32S3. Periodically scans BLE and POSTs results to a server.

## Endpoints

### `POST /enroll`
Sent once, after WiFi connect.

```json
{
  "mac": "AA:BB:CC:DD:EE:FF",
  "table_uid": 12
}
```

| Field       | Type   | Notes                                     |
|-------------|--------|-------------------------------------------|
| `mac`       | string | Device WiFi MAC, colon-separated, upper.  |
| `table_uid` | int    | Configured at provisioning over Serial.   |

### `POST /scanreport`
Sent every 5 s. One timestamp per batch; one entry per device seen.

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

| Field        | Type           | Notes                                                              |
|--------------|----------------|--------------------------------------------------------------------|
| `table_uid`  | int            | Same as enrollment.                                                |
| `timestamp`  | string         | ISO 8601 UTC, NTP-synced.                                          |
| `scans[]`    | array          | One object per device observed in the scan window.                 |
| `ble_uid`    | string \| null | `<uuid>:<major>:<minor>` for iBeacon advertisements; else `null`.  |
| `mac`        | string         | Advertising address (may be a randomized BLE address).             |
| `rssi`       | int            | dBm.                                                               |
| `tx_power`   | int \| null    | Reported power at 1 m: iBeacon measured-power byte, or BLE TX-Power AD field, else `null`. |

## Offline mode

If no WiFi is reachable on boot, the device prompts `Run in offline mode (Y/N)` over Serial. In offline mode it skips `/enroll` and prints the same `/scanreport` JSON (pretty-printed) to Serial every 5 s.
