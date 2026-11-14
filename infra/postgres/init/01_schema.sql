-- Project Atlas - initial schema
-- AT-26-L1-002  |  FR-08, FR-16..FR-21, FR-23

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE roles (
    id          SERIAL PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    description TEXT
);
INSERT INTO roles (name, description) VALUES
    ('administrator','Full access including device and user management'),
    ('operator','Configure devices and acknowledge alarms'),
    ('viewer','Read-only access');

CREATE TABLE users (
    id            SERIAL PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,          -- Argon2id.  FR-23
    email         TEXT,
    role_id       INT NOT NULL REFERENCES roles(id),
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE devices (
    id            SERIAL PRIMARY KEY,
    device_uid    TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,
    site          TEXT NOT NULL DEFAULT 'lab-a',
    location      TEXT,
    mqtt_username TEXT NOT NULL,          -- identifier only, never the secret
    status        TEXT NOT NULL DEFAULT 'offline'
                  CHECK (status IN ('online','offline','degraded')),
    last_seen_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at    TIMESTAMPTZ
);

CREATE TABLE device_config (
    id                 SERIAL PRIMARY KEY,
    device_id          INT NOT NULL REFERENCES devices(id),
    sample_interval_ms INT NOT NULL DEFAULT 1000,
    enabled_metrics    TEXT[] NOT NULL DEFAULT '{}',
    updated_by         TEXT,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE thresholds (
    id                 SERIAL PRIMARY KEY,
    device_id          INT NOT NULL REFERENCES devices(id),
    metric             TEXT NOT NULL,
    min_value          DOUBLE PRECISION,
    max_value          DOUBLE PRECISION,
    max_rate_of_change DOUBLE PRECISION,
    z_window           INT DEFAULT 300,
    z_sigma            DOUBLE PRECISION DEFAULT 3.0,
    confirm_samples    INT DEFAULT 3,     -- see report 10.2
    UNIQUE (device_id, metric)
);

-- Telemetry: the only high-volume table.  FR-08
CREATE TABLE telemetry (
    time      TIMESTAMPTZ NOT NULL,
    device_id INT NOT NULL REFERENCES devices(id),
    metric    TEXT NOT NULL,
    value     DOUBLE PRECISION NOT NULL,
    quality   TEXT NOT NULL DEFAULT 'ok'
);
SELECT create_hypertable('telemetry','time', chunk_time_interval => INTERVAL '1 day');
CREATE INDEX ON telemetry (device_id, time DESC);

CREATE TABLE events (
    id             SERIAL PRIMARY KEY,
    device_id      INT NOT NULL REFERENCES devices(id),
    severity       TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
    detector       TEXT NOT NULL,
    metric         TEXT,
    observed_value DOUBLE PRECISION,
    message        TEXT NOT NULL,
    occurred_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE alarms (
    id              SERIAL PRIMARY KEY,
    event_id        INT NOT NULL REFERENCES events(id),
    device_id       INT NOT NULL REFERENCES devices(id),
    state           TEXT NOT NULL DEFAULT 'active'
                    CHECK (state IN ('active','acknowledged','cleared')),
    severity        TEXT NOT NULL,
    raised_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    acknowledged_by TEXT,
    cleared_at      TIMESTAMPTZ
);

-- Audit trail: records before AND after.  FR-19, FR-20
CREATE TABLE audit_log (
    id          SERIAL PRIMARY KEY,
    user_id     INT REFERENCES users(id),
    action      TEXT NOT NULL,
    entity_type TEXT,
    entity_id   INT,
    before      JSONB,
    after       JSONB,
    ip_address  TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed the simulator so we can work before hardware arrives
INSERT INTO devices (device_uid, name, site, mqtt_username, location)
VALUES ('atlas-sim-01','Simulator Node 1','lab-a','atlas-sim-01','software simulator');
INSERT INTO device_config (device_id, enabled_metrics)
SELECT id, ARRAY['bus_voltage_v','current_ma','power_mw','accel_x_g','accel_y_g','accel_z_g']
FROM devices WHERE device_uid='atlas-sim-01';
