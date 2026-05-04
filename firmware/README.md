# BLE-Replay-Firmware

Firmware for Seeed XIAO ESP32S3. Periodically scans BLE and POSTs results to a server.

## Build & Flash

**Board:** `esp32:esp32:XIAO_ESP32S3` (Arduino-ESP32 core 3.x)
**Library:** ArduinoJson 7.x

One-time setup:

```bash
arduino-cli core update-index --additional-urls https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
arduino-cli core install esp32:esp32 --additional-urls https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
arduino-cli lib install "ArduinoJson"
```

### Compile

`arduino-cli` requires the sketch directory name to match the `.ino` filename, so we stage the sketch into a properly-named dir before compiling:

```bash
# from the repo root
STAGE=$(mktemp -d)/Beacon-Firmware
mkdir -p "$STAGE" && cp firmware/Beacon-Firmware.ino "$STAGE/"
arduino-cli compile \
  --fqbn esp32:esp32:XIAO_ESP32S3 \
  --output-dir firmware/build \
  "$STAGE"
```

Artifacts land in [firmware/build/](build/).

### Upload (arduino-cli)

Plug the badge in. On Linux it enumerates as `/dev/ttyACM0` (macOS: `/dev/cu.usbmodem*`, Windows: `COMx`). Confirm with `arduino-cli board list`.

```bash
arduino-cli upload \
  -p /dev/ttyACM0 \
  --fqbn esp32:esp32:XIAO_ESP32S3 \
  --input-dir firmware/build \
  "$STAGE"
```

If the board is stuck and won't enumerate, hold **BOOT**, tap **RESET**, release **BOOT** to force download mode, then re-run.

### Flash a pre-built binary (esptool)

For a clean reflash with no toolchain installed, use the merged image at [build/Beacon-Firmware.ino.merged.bin](build/Beacon-Firmware.ino.merged.bin) — it bundles bootloader + partition table + app and flashes at offset `0x0`:

```bash
esptool.py --chip esp32s3 --port /dev/ttyACM0 --baud 921600 \
  write_flash -z 0x0 firmware/build/Beacon-Firmware.ino.merged.bin
```

Or flash the three-part split (matches what `arduino-cli upload` does internally):

```bash
esptool.py --chip esp32s3 --port /dev/ttyACM0 --baud 921600 \
  write_flash -z \
  0x0      firmware/build/Beacon-Firmware.ino.bootloader.bin \
  0x8000   firmware/build/Beacon-Firmware.ino.partitions.bin \
  0x10000  firmware/build/Beacon-Firmware.ino.bin
```

### Published binaries

Built against Arduino-ESP32 core 3.1.3, ArduinoJson 7.4.2, default XIAO_ESP32S3 board options (8 MB flash, default 3 MB APP / 1.5 MB SPIFFS partition scheme, hwcdc on boot, 240 MHz, QIO @ 80 MHz):

| File | Offset | Purpose |
|------|--------|---------|
| [Beacon-Firmware.ino.merged.bin](build/Beacon-Firmware.ino.merged.bin) | `0x0` | Full 8 MB image — single-shot flash |
| [Beacon-Firmware.ino.bootloader.bin](build/Beacon-Firmware.ino.bootloader.bin) | `0x0` | 2nd-stage bootloader |
| [Beacon-Firmware.ino.partitions.bin](build/Beacon-Firmware.ino.partitions.bin) | `0x8000` | Partition table |
| [Beacon-Firmware.ino.bin](build/Beacon-Firmware.ino.bin) | `0x10000` | Application image |

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
