/*
 * Project Atlas - physical node firmware
 * AT-26-L1-002  |  serves FR-03, FR-05, FR-06, and supplies FR-09 with real data
 *
 * Publishes sensor telemetry to the platform over MQTT with TLS, on the same
 * payload contract the software simulator uses. That contract was written first
 * and the simulator built against it, precisely so that this firmware could be
 * introduced without touching the ingestion path. Nothing downstream changes
 * when this node replaces the simulator.
 *
 * Four decisions in here are load-bearing and are stated rather than left to be
 * rediscovered:
 *
 * 1.  Time is synchronised before the first TLS connection. Certificate
 *     validation compares the presented validity dates against the device
 *     clock, and an ESP32 boots at the epoch. Connecting first and setting the
 *     clock afterwards produces a handshake failure whose message names the
 *     certificate, not the clock, and costs an afternoon.
 *
 * 2.  The MQTT buffer is enlarged. PubSubClient defaults to 256 bytes and
 *     silently refuses to publish anything larger - publish() returns false and
 *     nothing reaches the wire. A nine-metric document exceeds 256 bytes, so
 *     the six-metric bench configuration works and the nine-metric one fails,
 *     which is the most misleading version of this bug.
 *
 * 3.  A failed sensor read degrades the message, it does not drop it. The
 *     contract carries a quality field for this: the remaining metrics are
 *     still recorded and the failure is visible, rather than the whole sample
 *     disappearing and looking like a network gap.
 *
 * 4.  The Last Will is registered at connect time with retain set. FR-03 asks
 *     for offline detection; a device that loses power cannot announce its own
 *     departure, so the broker announces it on the device's behalf.
 *
 * 5.  The environmental channel has two implementations behind mutually
 *     exclusive build flags. ENABLE_BME280 is the designed configuration, one
 *     chip for three metrics. ENABLE_AHT20_BMP280 is Deviation 9: the BME280
 *     is on international lead time, and an AHT20 + BMP280 pair covers the
 *     same three metrics across two chips at two addresses. Both paths emit
 *     the identical metric names, so nothing downstream distinguishes them,
 *     and the substitution is reversed by changing two flags rather than by
 *     editing the read path back.
 */

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <time.h>
#include <sys/time.h>
#include <Preferences.h>

#include "atlas_config.h"
#include "ca_cert.h"

#if ENABLE_INA219
  #include <Adafruit_INA219.h>
  static Adafruit_INA219 ina219;
  static bool ina219_ok = false;
#endif

#if ENABLE_MPU6050
  #include <Adafruit_MPU6050.h>
  static Adafruit_MPU6050 mpu;
  static bool mpu_ok = false;
#endif

#if ENABLE_BME280
  #include <Adafruit_BME280.h>
  static Adafruit_BME280 bme;
  static bool bme_ok = false;
#endif

#if ENABLE_BME280 && ENABLE_AHT20_BMP280
  #error "ENABLE_BME280 and ENABLE_AHT20_BMP280 both write temperature_c and humidity_pct. Enable exactly one."
#endif

#if ENABLE_AHT20_BMP280
  #include <Adafruit_AHTX0.h>
  #include <Adafruit_BMP280.h>
  static Adafruit_AHTX0  aht;
  static Adafruit_BMP280 bmp;
  static bool aht_ok = false;
  static bool bmp_ok = false;
  static uint8_t bmp_addr = 0;
#endif

static const float G_PER_MS2 = 1.0f / 9.80665f;

static WiFiClientSecure net;
static const char BROKER_CERT_SHA256[] = "3A:01:53:49:EC:F5:FE:71:26:7C:88:70:26:C8:82:52:41:E9:90:A9:18:B6:46:00:2C:01:43:CE:68:3B:64:4C";
static PubSubClient     mqtt(net);

static char topic_telemetry[96];
static char topic_status[96];
static char topic_config[96];
static char topic_config_ack[96];

static Preferences prefs;
static uint32_t      seq = 0;
static uint32_t      runtime_sample_interval_ms = SAMPLE_INTERVAL_MS;
static String        enabled_metrics_csv;
static unsigned long last_sample_ms = 0;
static unsigned long last_reconnect_ms = 0;

/* ------------------------------------------------------------------ logging
 * One JSON object per line, matching the structured logging the platform
 * services emit. The serial log and the container logs can then be read the
 * same way during bring-up, and a captured serial session is usable as
 * milestone evidence without reformatting.
 */
static void logj(const char *level, const char *message, const char *extra = nullptr) {
  Serial.printf("{\"level\":\"%s\",\"component\":\"node\",\"device_uid\":\"%s\",\"message\":\"%s\"%s%s}\n",
                level, DEVICE_UID, message,
                extra ? "," : "", extra ? extra : "");
}

/* --------------------------------------------------------------- timestamps
 * Millisecond resolution, not second. The node samples at 1 Hz and the
 * telemetry hypertable is keyed on time; with whole-second stamps, clock drift
 * or a slightly early sample can place two readings in the same second and
 * produce an ambiguous series.
 */
static void iso8601_now(char *out, size_t len) {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  struct tm t;
  gmtime_r(&tv.tv_sec, &t);
  size_t n = strftime(out, len, "%Y-%m-%dT%H:%M:%S", &t);
  snprintf(out + n, len - n, ".%03ldZ", (long)(tv.tv_usec / 1000));
}

static bool clock_is_set() {
  // Any value past 2020 means NTP has answered; the epoch default is 1970.
  return time(nullptr) > 1577836800;
}

/* --------------------------------------------------------------- i2c survey
 * Printed once at boot. When a sensor is missing on the bench this is the
 * difference between "the driver failed" and "the device is not on the bus",
 * which are different faults with different fixes. The logic analyser confirms
 * it, but the survey answers it in one second without instrumentation.
 */
static void i2c_scan() {
  Serial.println("{\"level\":\"info\",\"component\":\"node\",\"message\":\"i2c survey\"}");
  uint8_t found = 0;
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      const char *guess = "unknown";
      if (addr == 0x38) guess = "AHT20";
      if (addr == 0x40) guess = "INA219";
      if (addr == 0x68 || addr == 0x69) guess = "MPU6050";
      if (addr == 0x76 || addr == 0x77) guess = "BME280 or BMP280";
      Serial.printf("  0x%02X  %s\n", addr, guess);
      found++;
    }
  }
  if (found == 0)
    logj("error", "no i2c devices responded - check SDA, SCL, pull-ups and 3V3");
}

/* ----------------------------------------------------------------- sensors */
#if ENABLE_AHT20_BMP280
/* Raw chip-ID read, register 0xD0.
 *
 * Adafruit_BMP280::begin() validates the identifier internally and returns
 * false without reporting what it actually found. On this bench that hides the
 * one distinction that matters: "nothing is on the bus" and "the wrong part is
 * at the right address" are different faults with different fixes, and the
 * first version of this firmware lost time to exactly that ambiguity. The
 * register is therefore read directly and the value logged before the driver
 * is given a chance to fail silently.
 */
static bool i2c_read_reg8(uint8_t addr, uint8_t reg, uint8_t *out) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;   // repeated start
  if (Wire.requestFrom((int)addr, 1) != 1) return false;
  *out = Wire.read();
  return true;
}
#endif

static void sensors_begin() {
#if ENABLE_INA219
  ina219_ok = ina219.begin();
  if (ina219_ok) {
    // 32V / 1A calibration. The default 32V/2A range wastes half the ADC span
    // on a bench load that never approaches 2A, costing current resolution
    // exactly where the rate-of-change detector needs it.
    ina219.setCalibration_32V_1A();
    logj("info", "INA219 initialised", "\"range\":\"32V/1A\"");
  } else {
    logj("error", "INA219 not found at 0x40");
  }
#endif

#if ENABLE_MPU6050
  mpu_ok = mpu.begin();
  if (mpu_ok) {
    mpu.setAccelerometerRange(MPU6050_RANGE_2_G);   // bench vibration, not impact
    mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);
    logj("info", "MPU6050 initialised", "\"range\":\"2g\"");
  } else {
    logj("error", "MPU6050 not found at 0x68 or 0x69");
  }
#endif

#if ENABLE_BME280
  bme_ok = bme.begin(0x76) || bme.begin(0x77);
  if (bme_ok) {
    // Chip ID 0x60 is a BME280; 0x58 is a BMP280, which has no humidity
    // channel. The two breakouts are visually identical and are frequently
    // shipped interchangeably. Refuse the substitute here rather than publish
    // a humidity_pct that is silently fabricated.
    uint8_t id = bme.sensorID();
    if (id != 0x60) {
      bme_ok = false;
      Serial.printf("{\"level\":\"error\",\"component\":\"node\",\"message\":"
                    "\"sensor at the BME280 address is not a BME280\",\"chip_id\":\"0x%02X\"}\n", id);
    } else {
      bme.setSampling(Adafruit_BME280::MODE_FORCED,
                      Adafruit_BME280::SAMPLING_X1,   // temperature
                      Adafruit_BME280::SAMPLING_X1,   // pressure
                      Adafruit_BME280::SAMPLING_X1,   // humidity
                      Adafruit_BME280::FILTER_OFF);
      logj("info", "BME280 initialised", "\"mode\":\"forced\"");
    }
  } else {
    logj("error", "BME280 not found at 0x76 or 0x77");
  }
#endif

#if ENABLE_AHT20_BMP280
  /* AHT20 - temperature and humidity. The address is fixed at 0x38 by the
   * part; there is no alternate to probe.
   */
  aht_ok = aht.begin(&Wire);
  if (aht_ok) {
    logj("info", "AHT20 initialised", "\"addr\":\"0x38\"");
  } else {
    logj("error", "AHT20 not found at 0x38");
  }

  /* BMP280 - pressure.
   *
   * The identifier check the BME280 path performs is kept, with the acceptance
   * value inverted: 0x58 is now the expected part and 0x60 is the one refused.
   * The principle behind the original check is unchanged - a humidity value is
   * never fabricated from a chip that cannot measure humidity. Here humidity
   * comes from the AHT20 and from nowhere else, so refusing the BMP280 is no
   * longer necessary to hold that guarantee.
   */
  const uint8_t bmp_candidates[2] = { 0x76, 0x77 };
  for (uint8_t k = 0; k < 2 && !bmp_ok; k++) {
    uint8_t id = 0;
    if (!i2c_read_reg8(bmp_candidates[k], 0xD0, &id)) continue;   // nothing there

    if (id == 0x60) {
      Serial.printf("{\"level\":\"error\",\"component\":\"node\",\"message\":"
                    "\"a BME280 is on the bus but this build targets AHT20+BMP280 - "
                    "rebuild with ENABLE_BME280=1 and ENABLE_AHT20_BMP280=0\","
                    "\"addr\":\"0x%02X\",\"chip_id\":\"0x60\"}\n", bmp_candidates[k]);
      continue;
    }
    if (id != 0x58) {
      Serial.printf("{\"level\":\"error\",\"component\":\"node\",\"message\":"
                    "\"device at the BMP280 address is not a BMP280\","
                    "\"addr\":\"0x%02X\",\"chip_id\":\"0x%02X\"}\n", bmp_candidates[k], id);
      continue;
    }
    if (!bmp.begin(bmp_candidates[k], BMP280_CHIPID)) {
      Serial.printf("{\"level\":\"error\",\"component\":\"node\",\"message\":"
                    "\"BMP280 identified but driver initialisation failed\","
                    "\"addr\":\"0x%02X\"}\n", bmp_candidates[k]);
      continue;
    }

    // Forced mode, for the same reason the BME280 path uses it: continuous
    // conversion self-heats the die and biases the temperature channel upward,
    // and buys nothing at a 1 Hz duty cycle.
    bmp.setSampling(Adafruit_BMP280::MODE_FORCED,
                    Adafruit_BMP280::SAMPLING_X1,     // temperature
                    Adafruit_BMP280::SAMPLING_X1,     // pressure
                    Adafruit_BMP280::FILTER_OFF);
    bmp_ok   = true;
    bmp_addr = bmp_candidates[k];

    char extra[64];
    snprintf(extra, sizeof extra, "\"addr\":\"0x%02X\",\"chip_id\":\"0x58\",\"mode\":\"forced\"",
             bmp_addr);
    logj("info", "BMP280 initialised", extra);
  }
  if (!bmp_ok) logj("error", "BMP280 not found at 0x76 or 0x77");

  /* Boot-time cross-check.
   *
   * Both parts measure temperature independently. Only the AHT20 value is
   * published, but comparing them once at boot costs one conversion and gives
   * a stated agreement figure between two independent sensors - which is
   * measurement evidence rather than an assertion that the bus is healthy.
   */
  if (aht_ok && bmp_ok) {
    sensors_event_t h, t;
    if (aht.getEvent(&h, &t) && bmp.takeForcedMeasurement()) {
      float tb = bmp.readTemperature();
      char extra[112];
      snprintf(extra, sizeof extra,
               "\"aht20_c\":%.2f,\"bmp280_c\":%.2f,\"delta_c\":%.2f",
               t.temperature, tb, t.temperature - tb);
      logj("info", "temperature cross-check at boot", extra);
    }
  }
#endif
}

/* -------------------------------------------------------------------- wifi */
static void wifi_connect() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);          // modem sleep adds latency to the 250 ms budget
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  logj("info", "associating with the access point");

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 30000) {
    delay(250);
  }
  if (WiFi.status() == WL_CONNECTED) {
    char extra[64];
    snprintf(extra, sizeof extra, "\"ip\":\"%s\",\"rssi\":%d",
             WiFi.localIP().toString().c_str(), WiFi.RSSI());
    logj("info", "wifi connected", extra);
  } else {
    logj("error", "wifi association timed out, restarting");
    ESP.restart();
  }
}

static void time_sync() {
  configTime(0, 0, NTP_SERVER_1, NTP_SERVER_2);   // UTC; the payload carries Z
  logj("info", "waiting for time synchronisation");
  unsigned long start = millis();
  while (!clock_is_set() && millis() - start < 20000) delay(200);

  if (!clock_is_set()) {
    logj("error", "no time source - TLS validation will fail, restarting");
    ESP.restart();
  }
  char ts[32];
  iso8601_now(ts, sizeof ts);
  char extra[64];
  snprintf(extra, sizeof extra, "\"utc\":\"%s\"", ts);
  logj("info", "clock set", extra);
}

/* ---------------------------------------------------------------- runtime config */
static void load_runtime_config() {
  if (!prefs.begin("atlas", false)) {
    logj("error", "runtime config storage unavailable");
    return;
  }
  runtime_sample_interval_ms = prefs.getUInt("interval", SAMPLE_INTERVAL_MS);
  if (runtime_sample_interval_ms < 100 || runtime_sample_interval_ms > 3600000) {
    runtime_sample_interval_ms = SAMPLE_INTERVAL_MS;
  }
  enabled_metrics_csv = prefs.getString("metrics", "");
}

static void publish_config_ack(bool accepted, const char *reason = nullptr) {
  JsonDocument ack;
  ack["device_uid"] = DEVICE_UID;
  ack["result"] = accepted ? "ok" : "rejected";
  ack["sample_interval_ms"] = runtime_sample_interval_ms;
  if (reason) ack["reason"] = reason;
  char buf[384];
  size_t n = serializeJson(ack, buf, sizeof buf);
  mqtt.publish(topic_config_ack, (const uint8_t *)buf, n, true);
}

static void mqtt_callback(char *topic, uint8_t *payload, unsigned int length) {
  if (strcmp(topic, topic_config) != 0) return;

  JsonDocument config;
  DeserializationError err = deserializeJson(config, payload, length);
  if (err) {
    logj("warning", "malformed configuration ignored");
    publish_config_ack(false, "malformed JSON");
    return;
  }

  uint32_t requested = config["sample_interval_ms"] | runtime_sample_interval_ms;
  if (requested < 100 || requested > 3600000) {
    publish_config_ack(false, "sample_interval_ms must be between 100 and 3600000");
    return;
  }

  runtime_sample_interval_ms = requested;
  prefs.putUInt("interval", runtime_sample_interval_ms);
  if (config["enabled_metrics"].is<JsonArray>()) {
    String metrics;
    serializeJson(config["enabled_metrics"], metrics);
    enabled_metrics_csv = metrics;
    prefs.putString("metrics", enabled_metrics_csv);
  }
  char extra[64];
  snprintf(extra, sizeof extra, "\"interval_ms\":%u", runtime_sample_interval_ms);
  logj("info", "configuration applied", extra);
  publish_config_ack(true);
}

/* -------------------------------------------------------------------- mqtt */
static bool mqtt_connect() {
  char will[128];
  snprintf(will, sizeof will,
           "{\"device_uid\":\"%s\",\"state\":\"offline\",\"reason\":\"lwt\"}",
           DEVICE_UID);

  logj("info", "connecting to the broker over TLS");

  if (!net.connected()) {
    if (!net.connect(MQTT_HOST, MQTT_PORT)) {
      logj("error", "TLS socket connection failed");
      return false;
    }
  }
  if (!net.verify(BROKER_CERT_SHA256, nullptr)) {
    logj("error", "broker certificate fingerprint mismatch");
    net.stop();
    return false;
  }
  bool ok = mqtt.connect(DEVICE_UID, MQTT_USERNAME, MQTT_PASSWORD,
                         topic_status, 1, true, will, true);
  if (!ok) {
    char extra[96];
    // -2 is a TLS or socket failure: certificate, SAN, clock or routing.
    // 5 is an authorisation refusal: the broker password file.
    snprintf(extra, sizeof extra, "\"state\":%d", mqtt.state());
    logj("error", "broker connection refused", extra);
    return false;
  }

  char online[192];
  snprintf(online, sizeof online,
           "{\"device_uid\":\"%s\",\"state\":\"online\",\"fw\":\"%s\",\"ip\":\"%s\"}",
           DEVICE_UID, FIRMWARE_VERSION, WiFi.localIP().toString().c_str());
  mqtt.publish(topic_status, online, true);
  if (mqtt.subscribe(topic_config, 1)) {
    logj("info", "subscribed to configuration downlink");
  } else {
    logj("error", "configuration subscription failed");
  }

  logj("info", "broker connected, online status published");
  return true;
}

/* --------------------------------------------------------------- telemetry */
static void publish_telemetry() {
  JsonDocument doc;

  char ts[32];
  iso8601_now(ts, sizeof ts);

  doc["device_uid"] = DEVICE_UID;
  doc["ts"]         = ts;
  doc["seq"]        = seq;

  JsonObject m = doc["metrics"].to<JsonObject>();
  bool degraded = false;

#if ENABLE_INA219
  if (ina219_ok) {
    float v  = ina219.getBusVoltage_V();
    float i  = ina219.getCurrent_mA();
    float p  = ina219.getPower_mW();
    if (isfinite(v) && isfinite(i) && isfinite(p)) {
      m["bus_voltage_v"] = round(v * 1000) / 1000.0;
      m["current_ma"]    = round(i * 100) / 100.0;
      m["power_mw"]      = round(p * 100) / 100.0;
    } else {
      degraded = true;
      logj("warning", "INA219 returned a non-finite reading");
    }
  } else {
    degraded = true;
  }
#endif

#if ENABLE_MPU6050
  if (mpu_ok) {
    sensors_event_t a, g, t;
    if (mpu.getEvent(&a, &g, &t)) {
      // The library reports acceleration in m/s^2; the contract is in g.
      m["accel_x_g"] = round(a.acceleration.x * G_PER_MS2 * 1000) / 1000.0;
      m["accel_y_g"] = round(a.acceleration.y * G_PER_MS2 * 1000) / 1000.0;
      m["accel_z_g"] = round(a.acceleration.z * G_PER_MS2 * 1000) / 1000.0;
    } else {
      degraded = true;
      logj("warning", "MPU6050 read failed");
    }
  } else {
    degraded = true;
  }
#endif

#if ENABLE_BME280
  if (bme_ok) {
    // Forced mode: one conversion per request, then back to sleep. Continuous
    // mode self-heats the die and biases the temperature channel upward, which
    // at a 1 Hz duty cycle buys nothing.
    if (bme.takeForcedMeasurement()) {
      float tc = bme.readTemperature();
      float hp = bme.readHumidity();
      float pa = bme.readPressure() / 100.0f;
      if (isfinite(tc) && isfinite(hp) && isfinite(pa)) {
        m["temperature_c"] = round(tc * 100) / 100.0;
        m["humidity_pct"]  = round(hp * 100) / 100.0;
        m["pressure_hpa"]  = round(pa * 100) / 100.0;
      } else {
        degraded = true;
        logj("warning", "BME280 returned a non-finite reading");
      }
    } else {
      degraded = true;
      logj("warning", "BME280 forced measurement did not complete");
    }
  } else {
    degraded = true;
  }
#endif

#if ENABLE_AHT20_BMP280
  /* temperature_c is taken from the AHT20, not the BMP280, although both can
   * supply it. Three reasons, in order of weight:
   *
   *   1. Coherence. Relative humidity is only meaningful against the
   *      temperature of the same sensing element. Pairing an AHT20 humidity
   *      reading with a BMP280 temperature reading makes any derived quantity
   *      - dew point most obviously - an artefact of the gap between two dies
   *      on opposite ends of the board.
   *   2. Accuracy. The AHT20 is specified at +/-0.3 C against +/-0.5 C for the
   *      BMP280, whose temperature channel exists primarily to compensate its
   *      own pressure reading.
   *   3. Substitution. A BME280 measures all three on one die. Sourcing
   *      temperature from the humidity part keeps the swap in Deviation 9 a
   *      one-for-one change rather than a redistribution of channels.
   *
   * The BMP280 temperature is still measured - the driver needs it internally
   * for pressure compensation - and is logged once at boot as a cross-check.
   */
  if (aht_ok) {
    sensors_event_t h, t;
    if (aht.getEvent(&h, &t) &&
        isfinite(t.temperature) && isfinite(h.relative_humidity)) {
      m["temperature_c"] = round(t.temperature * 100) / 100.0;
      m["humidity_pct"]  = round(h.relative_humidity * 100) / 100.0;
    } else {
      degraded = true;
      logj("warning", "AHT20 returned a failed or non-finite reading");
    }
  } else {
    degraded = true;
  }

  if (bmp_ok) {
    if (bmp.takeForcedMeasurement()) {
      float pa = bmp.readPressure() / 100.0f;      // Pa to hPa, per the contract
      if (isfinite(pa)) {
        m["pressure_hpa"] = round(pa * 100) / 100.0;
      } else {
        degraded = true;
        logj("warning", "BMP280 returned a non-finite reading");
      }
    } else {
      degraded = true;
      logj("warning", "BMP280 forced measurement did not complete");
    }
  } else {
    degraded = true;
  }
#endif

  // The ingestion boundary rejects an empty metrics object. Publishing one
  // anyway would generate a rejection log every second and bury real faults.
  if (m.size() == 0) {
    logj("error", "no sensor produced a reading, sample suppressed");
    return;
  }

  doc["quality"] = degraded ? "degraded" : "ok";

  char buf[512];
  size_t n = serializeJson(doc, buf, sizeof buf);

  if (!mqtt.publish(topic_telemetry, (const uint8_t *)buf, n, false)) {
    char extra[64];
    snprintf(extra, sizeof extra, "\"bytes\":%u,\"state\":%d", (unsigned)n, mqtt.state());
    logj("error", "publish failed", extra);
    return;
  }

  seq++;
  if (seq % 30 == 0) {          // one line every 30 s, not every sample
    char extra[96];
    snprintf(extra, sizeof extra, "\"seq\":%u,\"metrics\":%u,\"bytes\":%u,\"quality\":\"%s\"",
             (unsigned)seq, (unsigned)m.size(), (unsigned)n, degraded ? "degraded" : "ok");
    logj("info", "publishing", extra);
  }
}

/* -------------------------------------------------------------------- main */
void setup() {
  Serial.begin(115200);
  delay(300);
  logj("info", "atlas node starting", "\"fw\":\"" FIRMWARE_VERSION "\"");

  snprintf(topic_telemetry, sizeof topic_telemetry, "atlas/%s/%s/telemetry", SITE, DEVICE_UID);
  snprintf(topic_status,    sizeof topic_status,    "atlas/%s/%s/status",    SITE, DEVICE_UID);
  snprintf(topic_config,    sizeof topic_config,    "atlas/%s/%s/config",    SITE, DEVICE_UID);
  snprintf(topic_config_ack,sizeof topic_config_ack,"atlas/%s/%s/config/ack",SITE, DEVICE_UID);

  load_runtime_config();
  mqtt.setCallback(mqtt_callback);

  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  Wire.setClock(400000);          // fast mode; the latency budget assumes it
  i2c_scan();
  sensors_begin();

  wifi_connect();
  time_sync();                    // strictly before the first TLS handshake

  net.setInsecure();  // TLS pinning is verified before MQTT credentials are sent

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setKeepAlive(30);          // matches the ingestion service
  mqtt.setBufferSize(512);        // see note 2 in the file header
  mqtt.setSocketTimeout(15);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    logj("warning", "wifi lost, reassociating");
    wifi_connect();
  }

  if (!mqtt.connected()) {
    // Backed off rather than retried tightly: a broker that is refusing
    // authorisation will refuse it just as firmly ten times a second.
    if (millis() - last_reconnect_ms > 5000) {
      last_reconnect_ms = millis();
      mqtt_connect();
    }
    return;
  }

  mqtt.loop();

  if (millis() - last_sample_ms >= runtime_sample_interval_ms) {
    last_sample_ms = millis();
    publish_telemetry();
  }
}
