# ATLAS — IoT and Cyber-Physical Monitoring

An ESP32 publishes electrical and environmental readings over MQTT/TLS. The ingestion service persists samples in PostgreSQL. The anomaly worker evaluates thresholds, rate of change, rolling statistical deviation and stale readings. FastAPI exposes readings, alarms and device administration to the React dashboard.

## Engineering scope

The checked-in PlatformIO configuration enables the INA219 and AHT20/BMP280. The MPU6050 and BME280 drivers are disabled in this configuration. A stored bench trace contains temperature, humidity and pressure readings. The simulator uses the node payload format. Learned detection remains outside the demonstrated pipeline.

## Getting started

For a bench build, install PlatformIO, configure the private node header and CA as described in the firmware guide, then run `pio run` from `firmware/atlas-node`. For the services, copy `.env.example` to `.env`, assign private credentials and provision broker certificates and service identities before starting Compose. The launcher expects the API health check on port 8000 and the dashboard on port 5173.

## Status and verification

Bench prototype with archived physical telemetry.

Physical reading artifact and detector source inspected. Python syntax and Compose configuration checked locally. Live telemetry, board flashing and alarm delivery were not repeated.

## Source and evidence

- [README.md](firmware/atlas-node/README.md)
- [anomaly.py](services/anomaly/anomaly.py)
- [m3_first_physical_reading.txt](docs/evidence/m3_first_physical_reading.txt)
- [SCOPE_COVERAGE.md](docs/SCOPE_COVERAGE.md)
- [atlas_demo_2241.mp4](atlas_demo_2241.mp4)

## Author

Ahmed Mohamed Elsayed Elreefy. Engineering work includes requirements, technical decisions, AI-assisted implementation, debugging and acceptance review.
