#include <Arduino.h>
#include <Wire.h>

#define SDA_PIN 25
#define SCL_PIN 13

void setup() {
  Serial.begin(115200);
  delay(1500);
  Serial.println("alive");

  Wire.begin(SDA_PIN, SCL_PIN, 100000);
  delay(100);

  int solid = 0;
  for (uint8_t addr = 1; addr < 127; addr++) {
    int hits = 0;
    for (int i = 0; i < 5; i++) {
      Wire.beginTransmission(addr);
      if (Wire.endTransmission() == 0) hits++;
      delay(5);
    }
    if (hits == 5) {
      Serial.printf("SOLID 0x%02X\n", addr);
      solid++;
    } else if (hits > 0) {
      Serial.printf("flaky 0x%02X %d/5\n", addr, hits);
    }
  }
  Serial.printf("solid total: %d\n", solid);
}

void loop() {}
