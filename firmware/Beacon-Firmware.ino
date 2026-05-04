// Beacon-Firmware
// Target: Seeed XIAO ESP32S3 (Replay Badge beacon variant)
// Toolchain: Arduino-ESP32 core 3.x
// Libraries (Library Manager): ArduinoJson 7.x
//
// LED codes (RED = primary status, YELLOW = activity):
//   RED slow breathe (~0.6 Hz)   Connecting to WiFi
//   RED single fade in/out       WiFi connected
//   RED fast blink (4 Hz)        WiFi failed; waiting on user (Serial / button)
//   RED solid (during press)     Long-press detected, hold confirmed
//   RED short blinks 1x/2x/3x    Digit 1 / 2 / 3 of the room UID confirmed
//   RED+YELLOW alternating 5 s   Setup complete; entering scan loop
//   YELLOW brief blip            Activity: click registered / BLE scan / HTTP POST
//   All off                      Idle in scan loop between events
//
// Boot sequence:
//   1. Read factory MAC from eFuse; use it as both device ID and BLE adv name.
//   2. Start BLE peripheral advertising as "BCN-<MAC>".
//   3. Connect to WiFi (saved -> default). RED breathes; fades once on success.
//      On full failure, RED fast-blinks and Serial prompts for offline mode.
//   4. NTP sync.
//   5. Room UID: 3 digits, 0-9 each. For each digit, click IO0 then long-press
//      (>=700 ms) to lock in. RED blinks 1x / 2x / 3x to confirm digit 1/2/3.
//      A long-press with no clicks counts as 0. If a room UID is already
//      stored, on boot the firmware prompts over Serial ("Override? Y/N", 5 s)
//      AND accepts a button press as "yes".
//   6. POST /enroll on the LOCAL server (10.231.168.200:5000).
//   7. POST /api/v1/devices on brooklyn.party (creates the device record;
//      created_at is set server-side and stays fixed for this runtime).
//   8. RED+YELLOW alternating strobe for 5 s.
//   9. Every 5 s: BLE active scan -> POST /scanreport on the LOCAL server,
//      AND PATCH /api/v1/devices/<mac> on brooklyn.party (refreshes
//      updated_at without touching created_at). In offline mode the
//      scanreport JSON is printed to Serial instead.
//
// Local server schema (POST /scanreport):
//   { "table_uid": <int>, "timestamp": "<ISO8601 UTC>",
//     "scans": [ { "ble_uid": <str|null>, "mac": "<addr>", "rssi": <int>, "tx_power": <int|null> }, ... ] }
//
// Brooklyn.party schema (POST /api/v1/devices, PATCH /api/v1/devices/<mac>):
//   POST  body: { "mac_address": "<addr>", "loc": <int> }   (server sets created_at)
//   PATCH body: { "loc": <int> }                            (server bumps updated_at)
//
// Type "reset" over Serial at any time to wipe stored config and reboot.

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <ArduinoJson.h>
#include <BLEDevice.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>
#include <BLEAdvertising.h>
#include <esp_mac.h>
#include <time.h>
#include <math.h>

// ---------- Hardware pins ----------
// XIAO ESP32S3 onboard user LED on GPIO21 (active LOW). Treated here as RED.
// If your board exposes a second user LED, set its pin here; otherwise leave -1.
#define LED_RED_PIN     21
#define LED_YELLOW_PIN  -1
#define LED_ACTIVE_LOW  1
#define BUTTON_PIN      0     // BOOT / IO0, active LOW with internal pull-up

// ---------- Network / behavior ----------
static const char*    DEFAULT_SSID         = "admin";
static const char*    DEFAULT_PASS         = "password";
static const char*    LOCAL_BASE           = "http://10.231.168.200:5000";
static const char*    EP_ENROLL            = "/enroll";
static const char*    EP_SCANREPORT        = "/scanreport";
static const char*    BROOKLYN_BASE        = "https://brooklyn.party";
static const char*    EP_DEVICES           = "/api/v1/devices";
static const uint32_t WIFI_CONNECT_TIMEOUT = 15000;
static const uint32_t SCAN_INTERVAL_MS     = 5000;
static const uint32_t SCAN_DURATION_S      = 5;
static const uint32_t HOLD_THRESHOLD_MS    = 700;    // press >= this is a "hold" (locks digit)
static const uint32_t ROOM_OVERRIDE_MS     = 5000;   // press IO0 / send 'y' within this on boot to re-enter room UID
static const char*    NTP_SERVER_1         = "pool.ntp.org";
static const char*    NTP_SERVER_2         = "time.google.com";

struct Config {
  String   ssid;
  String   password;
  uint32_t roomUid;   // 3-digit, 0..999. UINT32_MAX = unset.
};

enum ButtonEvent { BTN_NONE, BTN_CLICK, BTN_HOLD };

Preferences         prefs;
Config              cfg;
String              deviceMac;       // "AA:BB:CC:DD:EE:FF"
String              deviceMacFlat;   // "AABBCCDDEEFF"
String              bleName;         // "BCN-AABBCCDDEEFF"
BLEScan*            bleScan = nullptr;
BLEAdvertising*     bleAdv  = nullptr;
uint32_t            lastScan = 0;
bool                offlineMode = false;
bool                brooklynRegistered = false;   // POST /api/v1/devices succeeded at least once

// ---------- LED helpers ----------

static inline void ledWrite(int pin, uint8_t v) {
  if (pin < 0) return;
  analogWrite(pin, LED_ACTIVE_LOW ? (255 - v) : v);
}
static inline void ledOn(int pin)  { ledWrite(pin, 255); }
static inline void ledOff(int pin) { ledWrite(pin, 0); }

void ledInit() {
  pinMode(LED_RED_PIN, OUTPUT);
  ledOff(LED_RED_PIN);
#if LED_YELLOW_PIN >= 0
  pinMode(LED_YELLOW_PIN, OUTPUT);
  ledOff(LED_YELLOW_PIN);
#endif
}

void ledRedOn()     { ledOn(LED_RED_PIN); }
void ledRedOff()    { ledOff(LED_RED_PIN); }
void ledYellowOn()  {
#if LED_YELLOW_PIN >= 0
  ledOn(LED_YELLOW_PIN);
#endif
}
void ledYellowOff() {
#if LED_YELLOW_PIN >= 0
  ledOff(LED_YELLOW_PIN);
#endif
}

void ledAllOff() { ledRedOff(); ledYellowOff(); }

// One sinusoidal "breath" frame on RED. Call repeatedly from a loop.
void ledBreatheTickRed() {
  float phase = (float)(millis() % 1800) / 1800.0f * 2.0f * (float)PI;
  float k = (sinf(phase) + 1.0f) * 0.5f;
  ledWrite(LED_RED_PIN, (uint8_t)(255 * k));
}

// One smooth fade-in / fade-out on RED, blocking ~1 s.
void ledFadeOnceRed() {
  for (int i = 0; i <= 255; i += 8) { ledWrite(LED_RED_PIN, i); delay(8); }
  for (int i = 255; i >= 0; i -= 8) { ledWrite(LED_RED_PIN, i); delay(8); }
  ledRedOff();
}

// N short blinks on RED (digit-confirm pattern).
void ledRedBlinkN(uint8_t n, uint16_t onMs = 180, uint16_t offMs = 220) {
  for (uint8_t i = 0; i < n; i++) {
    ledRedOn();  delay(onMs);
    ledRedOff(); delay(offMs);
  }
}

// Fast steady blink on RED for failure / waiting states.
void ledRedFastBlinkTick() {
  bool on = (millis() / 125) & 1;
  if (on) ledRedOn(); else ledRedOff();
}

// Setup-complete signal: alternate RED/YELLOW (or just RED if no yellow).
void ledStrobeAlternate(uint32_t durationMs) {
  uint32_t start = millis();
  bool which = false;
  while (millis() - start < durationMs) {
#if LED_YELLOW_PIN >= 0
    if (which) { ledRedOn();  ledYellowOff(); }
    else       { ledRedOff(); ledYellowOn();  }
#else
    if (which) ledRedOn(); else ledRedOff();
#endif
    which = !which;
    delay(120);
  }
  ledAllOff();
}

// Brief activity blip on YELLOW (no-op if yellow not configured).
void ledYellowBlip(uint16_t ms = 60) {
#if LED_YELLOW_PIN >= 0
  ledYellowOn();
  delay(ms);
  ledYellowOff();
#else
  (void)ms;
#endif
}

// ---------- Button helpers ----   ------

void buttonInit() {
  pinMode(BUTTON_PIN, INPUT_PULLUP);
}

// Edge-triggered, debounced press detector. Returns true once per press.
bool buttonEdgePressed() {
  static int      lastStable = HIGH;
  static int      lastRead   = HIGH;
  static uint32_t lastChange = 0;
  int v = digitalRead(BUTTON_PIN);
  if (v != lastRead) { lastRead = v; lastChange = millis(); }
  if (millis() - lastChange > 30 && v != lastStable) {
    lastStable = v;
    if (v == LOW) return true;   // press edge
  }
  return false;
}

// Polled click/hold detector. Emits BTN_CLICK on a short press+release,
// BTN_HOLD once when the press has been held >= HOLD_THRESHOLD_MS.
ButtonEvent buttonPoll() {
  static int      stable      = HIGH;
  static int      lastRead    = HIGH;
  static uint32_t lastChange  = 0;
  static uint32_t pressStart  = 0;
  static bool     holdEmitted = false;

  int v = digitalRead(BUTTON_PIN);
  if (v != lastRead) { lastRead = v; lastChange = millis(); }

  if (millis() - lastChange > 30 && v != stable) {
    stable = v;
    if (stable == LOW) {
      pressStart  = millis();
      holdEmitted = false;
    } else {
      uint32_t dur = millis() - pressStart;
      if (!holdEmitted && dur < HOLD_THRESHOLD_MS) return BTN_CLICK;
    }
  }

  if (stable == LOW && !holdEmitted &&
      millis() - pressStart >= HOLD_THRESHOLD_MS) {
    holdEmitted = true;
    return BTN_HOLD;
  }

  return BTN_NONE;
}

// ---------- Serial helpers ----------

String serialReadLine(const char* prompt) {
  Serial.print(prompt);
  Serial.flush();
  while (Serial.available()) Serial.read();
  String s;
  while (true) {
    while (!Serial.available()) delay(10);
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') break;
    s += c;
    Serial.print(c);
  }
  Serial.println();
  s.trim();
  return s;
}

bool serialReadYesNo(const char* prompt) {
  while (true) {
    String r = serialReadLine(prompt);
    r.toLowerCase();
    if (r == "y" || r == "yes") return true;
    if (r == "n" || r == "no")  return false;
    Serial.println("Please answer Y or N.");
  }
}

void checkSerialReset() {
  if (!Serial.available()) return;
  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line.equalsIgnoreCase("reset")) {
    Serial.println("Wiping config and rebooting.");
    prefs.begin("beacon", false);
    prefs.clear();
    prefs.end();
    delay(200);
    ESP.restart();
  }
}

// ---------- Config persistence ----------

void loadConfig() {
  prefs.begin("beacon", true);
  cfg.ssid     = prefs.getString("ssid", "");
  cfg.password = prefs.getString("pass", "");
  cfg.roomUid  = prefs.getUInt("room", 0xFFFFFFFFu);
  prefs.end();
}

void saveConfig() {
  prefs.begin("beacon", false);
  prefs.putString("ssid", cfg.ssid);
  prefs.putString("pass", cfg.password);
  prefs.putUInt("room",   cfg.roomUid);
  prefs.end();
}

// ---------- MAC from eFuse ----------

void readFusedMac() {
  uint8_t m[6] = {0};
  // Reads the factory-burned MAC from eFuse BLOCK1. This block is one-time-
  // programmed at the Espressif factory and cannot be modified.
  esp_efuse_mac_get_default(m);
  char buf[18];
  snprintf(buf, sizeof(buf), "%02X:%02X:%02X:%02X:%02X:%02X",
           m[0], m[1], m[2], m[3], m[4], m[5]);
  deviceMac = String(buf);
  char flat[13];
  snprintf(flat, sizeof(flat), "%02X%02X%02X%02X%02X%02X",
           m[0], m[1], m[2], m[3], m[4], m[5]);
  deviceMacFlat = String(flat);
  bleName = String("BCN-") + deviceMacFlat;
  Serial.printf("MAC (eFuse): %s\n", deviceMac.c_str());
  Serial.printf("BLE adv name: %s\n", bleName.c_str());
}

// ---------- WiFi ----------

bool tryConnect(const String& ssid, const String& pass) {
  Serial.printf("Connecting to '%s'...\n", ssid.c_str());
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid.c_str(), pass.c_str());
  uint32_t start = millis();
  uint32_t lastDot = 0;
  while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_CONNECT_TIMEOUT) {
    ledBreatheTickRed();
    if (millis() - lastDot > 250) { Serial.print('.'); lastDot = millis(); }
    delay(20);
  }
  Serial.println();
  ledRedOff();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("Connected. IP: %s\n", WiFi.localIP().toString().c_str());
    ledFadeOnceRed();
    return true;
  }
  WiFi.disconnect(true, true);
  return false;
}

void promptWiFiOverSerial() {
  Serial.println("=== WiFi credentials ===");
  cfg.ssid     = serialReadLine("SSID: ");
  cfg.password = serialReadLine("Password: ");
  saveConfig();
}

void ensureWiFi() {
  if (cfg.ssid.length() > 0 && tryConnect(cfg.ssid, cfg.password)) return;
  if (tryConnect(DEFAULT_SSID, DEFAULT_PASS)) {
    cfg.ssid = DEFAULT_SSID; cfg.password = DEFAULT_PASS;
    saveConfig();
    return;
  }
  // Connection failed: fast-blink red while prompting.
  Serial.println("\nNo reachable WiFi network.");
  uint32_t blinkStart = millis();
  while (millis() - blinkStart < 800) { ledRedFastBlinkTick(); delay(20); }
  ledRedOn();    // hold solid while waiting on serial input
  if (serialReadYesNo("Run in offline mode (BLE scans printed to Serial)? (Y/N): ")) {
    offlineMode = true;
    ledRedOff();
    return;
  }
  while (true) {
    promptWiFiOverSerial();
    if (tryConnect(cfg.ssid, cfg.password)) return;
    ledRedOn();
    Serial.println("Could not connect; try again.");
  }
}

// ---------- NTP ----------

void syncTime() {
  configTime(0, 0, NTP_SERVER_1, NTP_SERVER_2);
  Serial.print("Syncing NTP");
  time_t now = 0;
  for (int i = 0; i < 40; i++) {
    delay(500);
    Serial.print('.');
    time(&now);
    if (now > 1700000000) break;
  }
  Serial.println();
  if (now < 1700000000) {
    Serial.println("WARNING: NTP sync failed; timestamps will be inaccurate.");
    return;
  }
  struct tm tmu;
  gmtime_r(&now, &tmu);
  char buf[32];
  strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &tmu);
  Serial.printf("Time: %s\n", buf);
}

String iso8601Now() {
  time_t now;
  time(&now);
  struct tm tmu;
  gmtime_r(&now, &tmu);
  char buf[32];
  strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &tmu);
  return String(buf);
}

// ---------- HTTP ----------

// Sends `body` to `url` with the given HTTP method ("POST" or "PATCH").
// Picks WiFiClient or WiFiClientSecure based on URL scheme.
int httpSend(const String& method, const String& url, const String& body) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi not connected; skipping request.");
    return -1;
  }
  HTTPClient http;
  WiFiClient        plain;
  WiFiClientSecure  secure;
  bool ok;
  if (url.startsWith("https://")) {
    secure.setInsecure();   // TODO: pin a CA cert for production
    ok = http.begin(secure, url);
  } else {
    ok = http.begin(plain, url);
  }
  if (!ok) {
    Serial.printf("HTTP begin failed for %s\n", url.c_str());
    return -2;
  }
  http.addHeader("Content-Type", "application/json");
  int code = http.sendRequest(method.c_str(), (uint8_t*)body.c_str(), body.length());
  Serial.printf("%s %s -> %d  (%u bytes)\n",
                method.c_str(), url.c_str(), code, body.length());
  if (code > 0) {
    String resp = http.getString();
    if (resp.length() > 0 && resp.length() < 400) {
      Serial.printf("  resp: %s\n", resp.c_str());
    }
  }
  http.end();
  return code;
}

bool postJsonLocal(const char* path, const String& body) {
  int code = httpSend("POST", String(LOCAL_BASE) + path, body);
  return code >= 200 && code < 300;
}

// ---------- Local /enroll ----------

bool enroll() {
  JsonDocument doc;
  doc["mac"]       = deviceMac;
  doc["table_uid"] = (uint32_t)cfg.roomUid;
  String body;
  serializeJson(doc, body);
  return postJsonLocal(EP_ENROLL, body);
}

// ---------- brooklyn.party device record ----------
// POST creates the record (server sets created_at). PATCH updates updated_at
// without disturbing created_at. We POST once per boot, then PATCH on every
// scan cycle so the server sees a fresh updated_at.

bool brooklynRegister() {
  JsonDocument doc;
  doc["mac_address"] = deviceMac;
  doc["loc"]         = (uint32_t)cfg.roomUid;
  String body;
  serializeJson(doc, body);
  int code = httpSend("POST", String(BROOKLYN_BASE) + EP_DEVICES, body);
  // Treat 2xx and 409 (already exists) as registered.
  if ((code >= 200 && code < 300) || code == 409) {
    brooklynRegistered = true;
    return true;
  }
  return false;
}

bool brooklynPatch() {
  JsonDocument doc;
  doc["loc"] = (uint32_t)cfg.roomUid;
  String body;
  serializeJson(doc, body);
  String url = String(BROOKLYN_BASE) + EP_DEVICES + "/" + deviceMac;
  int code = httpSend("PATCH", url, body);
  // If PATCH 404s, the record was lost; re-POST so we don't go silent.
  if (code == 404) {
    Serial.println("brooklyn PATCH 404; re-registering.");
    return brooklynRegister();
  }
  return code >= 200 && code < 300;
}

// ---------- Room UID input via button ----------

uint8_t readDigitFromButton(uint8_t digitIndex) {
  Serial.printf("Digit %u: click IO0 (0-9 times), then long-press to confirm.\n",
                digitIndex);
  uint8_t count = 0;
  while (true) {
    ButtonEvent e = buttonPoll();
    if (e == BTN_CLICK) {
      if (count < 9) count++;
      Serial.printf("  click -> %u\n", count);
      ledYellowBlip();
    } else if (e == BTN_HOLD) {
      Serial.printf("  hold -> digit %u = %u\n", digitIndex, count);
      ledRedOn();                                               // hold confirmation
      while (digitalRead(BUTTON_PIN) == LOW) delay(5);          // wait for release
      ledRedOff();
      return count;
    }
    delay(5);
  }
}

uint32_t readRoomUidFromButton() {
  Serial.println("=== Room UID (3 digits, each 0-9; long-press with no clicks = 0) ===");
  uint8_t d1 = readDigitFromButton(1);
  ledRedBlinkN(1);
  uint8_t d2 = readDigitFromButton(2);
  ledRedBlinkN(2);
  uint8_t d3 = readDigitFromButton(3);
  ledRedBlinkN(3);
  uint32_t room = (uint32_t)d1 * 100 + (uint32_t)d2 * 10 + (uint32_t)d3;
  Serial.printf("Room UID set: %u\n", room);
  return room;
}

// Wait up to windowMs for either an IO0 click OR a 'y'/'Y' over Serial.
// Returns true if user wants to override the stored room UID.
bool waitForOverride(uint32_t windowMs) {
  Serial.printf("Stored room UID: %u. Press IO0 OR send 'y' within %lus to change.\n",
                cfg.roomUid, (unsigned long)(windowMs / 1000));
  uint32_t start = millis();
  while (millis() - start < windowMs) {
    if (buttonEdgePressed()) return true;
    while (Serial.available()) {
      char c = (char)Serial.read();
      if (c == 'y' || c == 'Y') return true;
      if (c == 'n' || c == 'N') return false;
    }
    delay(5);
  }
  return false;
}

// ---------- BLE ----------

// iBeacon: manufacturer data == 0x4C 0x00 0x02 0x15 + 16-byte UUID + 2 major + 2 minor + 1 measured-power
static bool parseIBeacon(const std::string& md, String& uidOut, int8_t& txOut) {
  if (md.length() < 25) return false;
  if ((uint8_t)md[0] != 0x4C || (uint8_t)md[1] != 0x00) return false;
  if ((uint8_t)md[2] != 0x02 || (uint8_t)md[3] != 0x15) return false;
  char uuid[37];
  snprintf(uuid, sizeof(uuid),
    "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
    (uint8_t)md[4],  (uint8_t)md[5],  (uint8_t)md[6],  (uint8_t)md[7],
    (uint8_t)md[8],  (uint8_t)md[9],  (uint8_t)md[10], (uint8_t)md[11],
    (uint8_t)md[12], (uint8_t)md[13], (uint8_t)md[14], (uint8_t)md[15],
    (uint8_t)md[16], (uint8_t)md[17], (uint8_t)md[18], (uint8_t)md[19]);
  uint16_t major = ((uint8_t)md[20] << 8) | (uint8_t)md[21];
  uint16_t minor = ((uint8_t)md[22] << 8) | (uint8_t)md[23];
  char buf[64];
  snprintf(buf, sizeof(buf), "%s:%u:%u", uuid, major, minor);
  uidOut = String(buf);
  txOut  = (int8_t)md[24];
  return true;
}

void initBleStack() {
  BLEDevice::init(bleName.c_str());
  bleAdv = BLEDevice::getAdvertising();
  BLEAdvertisementData advData;
  advData.setName(bleName.c_str());
  advData.setFlags(0x06);
  bleAdv->setAdvertisementData(advData);
  bleAdv->setMinInterval(0x20);
  bleAdv->setMaxInterval(0x40);
  bleAdv->start();
  Serial.println("BLE advertising started.");
}

void initBleScanner() {
  bleScan = BLEDevice::getScan();
  bleScan->setActiveScan(true);
  bleScan->setInterval(100);
  bleScan->setWindow(99);
}

void scanAndReport() {
  BLEScanResults* results = bleScan->start(SCAN_DURATION_S, false);
  String ts = iso8601Now();

  JsonDocument doc;
  doc["table_uid"] = (uint32_t)cfg.roomUid;
  doc["timestamp"] = ts;
  JsonArray arr = doc["scans"].to<JsonArray>();

  int n = results ? results->getCount() : 0;
  for (int i = 0; i < n; i++) {
    BLEAdvertisedDevice d = results->getDevice(i);
    JsonObject o = arr.add<JsonObject>();

    String  uid;
    int8_t  tx     = 0;
    bool    haveTx = false;

    if (d.haveManufacturerData()) {
      std::string md = std::string(d.getManufacturerData().c_str(), d.getManufacturerData().length());
      if (parseIBeacon(md, uid, tx)) haveTx = true;
    }
    if (!haveTx && d.haveTXPower()) {
      tx     = d.getTXPower();
      haveTx = true;
    }

    if (uid.length() > 0) o["ble_uid"] = uid;
    else                  o["ble_uid"] = nullptr;

    o["mac"]  = d.getAddress().toString().c_str();
    o["rssi"] = d.getRSSI();
    if (haveTx) o["tx_power"] = tx;
    else        o["tx_power"] = nullptr;
  }

  ledYellowBlip();
  if (offlineMode) {
    serializeJsonPretty(doc, Serial);
    Serial.println();
  } else {
    String body;
    serializeJson(doc, body);
    postJsonLocal(EP_SCANREPORT, body);
    // Heartbeat to brooklyn.party so updated_at stays fresh.
    if (brooklynRegistered) brooklynPatch();
    else                    brooklynRegister();
  }
  bleScan->clearResults();
}

// ---------- Setup / loop ----------

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println();
  Serial.println("Beacon firmware starting.");
  Serial.println("Type 'reset' over Serial to wipe config.");

  ledInit();
  buttonInit();
  loadConfig();
  readFusedMac();
  initBleStack();        // start advertising before WiFi

  ensureWiFi();
  if (!offlineMode) syncTime();

  // Room UID: prompt via button if unset, or allow override (button or serial) on boot.
  if (cfg.roomUid <= 999) {
    if (waitForOverride(ROOM_OVERRIDE_MS)) {
      cfg.roomUid = readRoomUidFromButton();
      saveConfig();
    } else {
      Serial.println("Using stored room UID.");
    }
  } else {
    cfg.roomUid = readRoomUidFromButton();
    saveConfig();
  }
  Serial.printf("table_uid for /enroll + /scanreport: %u\n", cfg.roomUid);

  if (!offlineMode) {
    if (!enroll()) {
      Serial.println("Local /enroll failed; retrying in 5s.");
      delay(5000);
      enroll();
    }
    if (!brooklynRegister()) {
      Serial.println("brooklyn.party POST failed; will retry on first scan cycle.");
    }
  }

  Serial.println("Setup complete; running 5s LED strobe.");
  ledStrobeAlternate(5000);

  initBleScanner();
  Serial.println("Entering scan loop.");
}

void loop() {
  checkSerialReset();

  if (!offlineMode && WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi dropped; reconnecting.");
    ensureWiFi();
  }

  uint32_t now = millis();
  if (now - lastScan >= SCAN_INTERVAL_MS) {
    lastScan = now;
    scanAndReport();
  }
  delay(50);
}
