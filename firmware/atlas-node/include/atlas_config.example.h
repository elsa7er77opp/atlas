// Project Atlas - node configuration
// AT-26-L1-002
//
// Copy to atlas_config.h and fill in. atlas_config.h is gitignored; this
// template carries the variable names only, mirroring the .env / .env.example
// arrangement used by the platform services.
#pragma once

// ------------------------------------------------------------------ network
#define WIFI_SSID          ""
#define WIFI_PASSWORD      ""

// ------------------------------------------------------------------ identity
// DEVICE_UID must match the device_uid registered through the API and the
// username in the broker password file. The ACL is written against it, so a
// mismatch produces a successful TLS handshake followed by a silent publish
// refusal - the least obvious failure mode on the bench.
#define DEVICE_UID         "atlas-node-01"
#define SITE               "lab-a"
#define FIRMWARE_VERSION   "0.1.0"

// ------------------------------------------------------------------ broker
// The host LAN address, which must appear in the broker certificate's Subject
// Alternative Name. Run tools/reissue_broker_cert.sh first.
#define MQTT_HOST          "192.168.1.20"
#define MQTT_PORT          8883
#define MQTT_USERNAME      DEVICE_UID
#define MQTT_PASSWORD      ""

// ------------------------------------------------------------------ sampling
#define SAMPLE_INTERVAL_MS 1000
#define I2C_SDA_PIN        21
#define I2C_SCL_PIN        22

// Certificate validation compares the presented validity dates against the
// device clock, so a node that has not synchronised its time cannot complete a
// handshake. Time is therefore acquired before the first connection attempt,
// not after it.
#define NTP_SERVER_1       "pool.ntp.org"
#define NTP_SERVER_2       "time.google.com"
