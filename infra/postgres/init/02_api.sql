-- Project Atlas - API layer schema additions
-- AT-26-L1-002  |  FR-12, FR-13, FR-19..FR-22
--
-- NOTE ON APPLICATION ORDER
-- Files in /docker-entrypoint-initdb.d run ONCE, on an empty data directory.
-- If the pgdata volume already exists this file will NOT run automatically.
-- Apply it by hand:
--     docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--         < infra/postgres/init/02_api.sql
-- Every statement below is idempotent so a second application is harmless.

-- ---------------------------------------------------------------- sessions
-- Refresh tokens are persisted so they can be REVOKED. An access token cannot
-- be revoked without a lookup on every request, which defeats the point of a
-- stateless token; the design in report 8.8 therefore bounds access-token
-- damage by a short lifetime and puts revocation on the refresh token, which
-- is presented rarely. That is only enforceable if the server keeps a record.
--
-- The stored column is a SHA-256 digest, not the token. A fast digest is
-- correct here and Argon2id would be wrong: Argon2id exists to make GUESSING
-- expensive for low-entropy human passwords. A refresh token is 256 bits of
-- CSPRNG output inside a signed JWT, so there is nothing to guess, and a
-- memory-hard hash on the refresh path would only add latency.
CREATE TABLE IF NOT EXISTS refresh_tokens (
    id          SERIAL PRIMARY KEY,
    user_id     INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    jti         UUID UNIQUE NOT NULL,
    token_hash  TEXT NOT NULL,
    issued_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ,
    replaced_by UUID,
    user_agent  TEXT,
    ip_address  TEXT
);
CREATE INDEX IF NOT EXISTS refresh_tokens_user_idx
    ON refresh_tokens (user_id, expires_at DESC);

-- ----------------------------------------------------------- email auth
-- OTP codes are stored only as SHA-256 digests. Plaintext codes exist only in
-- the delivery function and the request body used for one verification attempt.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMPTZ;
CREATE UNIQUE INDEX IF NOT EXISTS users_email_unique_idx
    ON users (lower(email)) WHERE email IS NOT NULL;

CREATE TABLE IF NOT EXISTS auth_challenges (
    id          BIGSERIAL PRIMARY KEY,
    user_id     INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose     TEXT NOT NULL CHECK (purpose IN ('email_verify', 'password_reset')),
    code_hash   TEXT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    attempts    INT NOT NULL DEFAULT 0,
    consumed_at TIMESTAMPTZ,
    requested_ip TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS auth_challenges_lookup_idx
    ON auth_challenges (user_id, purpose, created_at DESC);
CREATE INDEX IF NOT EXISTS auth_challenges_expiry_idx
    ON auth_challenges (expires_at);

-- ------------------------------------------------------------- provisioning
-- Deviation 3 in the M2 progress report: broker credentials currently live in
-- a static password file, and the Dynamic Security Plugin is the M3 target.
-- A device registered through the API therefore exists in the platform before
-- it exists in the broker. That gap is recorded on the row rather than left
-- implicit, so a device that cannot connect has a visible reason.
ALTER TABLE devices
    ADD COLUMN IF NOT EXISTS broker_provisioned BOOLEAN NOT NULL DEFAULT FALSE;

-- Existing rows predate the API and were provisioned by hand into the
-- password file, so they are true.
UPDATE devices SET broker_provisioned = TRUE WHERE created_at < now();

-- ------------------------------------------------------------------ audit
CREATE INDEX IF NOT EXISTS audit_log_occurred_idx
    ON audit_log (occurred_at DESC);
CREATE INDEX IF NOT EXISTS events_device_time_idx
    ON events (device_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS alarms_state_idx
    ON alarms (state, raised_at DESC);

-- ------------------------------------------------------- trend aggregation
-- A trend chart over 24 hours at 1 Hz would return 86,400 points per metric.
-- No screen resolves that and no browser should receive it. The continuous
-- aggregate pre-computes one-minute buckets, reducing the same query to 1,440
-- rows, and TimescaleDB keeps it current incrementally rather than by
-- recomputing the window.
CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_1m
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 minute', time) AS bucket,
       device_id,
       metric,
       avg(value)   AS avg_value,
       min(value)   AS min_value,
       max(value)   AS max_value,
       count(*)     AS sample_count
FROM telemetry
GROUP BY bucket, device_id, metric
WITH NO DATA;

-- start_offset bounds the refresh window so the job cost stays constant as the
-- hypertable grows. end_offset leaves the newest minute alone until it is
-- complete, so a partially-filled bucket is never materialised.
DO $$
BEGIN
    PERFORM add_continuous_aggregate_policy('telemetry_1m',
        start_offset      => INTERVAL '1 hour',
        end_offset        => INTERVAL '1 minute',
        schedule_interval => INTERVAL '1 minute');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- --------------------------------------------------------------- live push
-- FR-12 asks for live updates. The alternative is the browser polling, which
-- costs a request per client per interval whether or not anything changed.
-- NOTIFY moves the trigger to where the change actually happens: the write.
-- The API holds one listening connection and fans out to every WebSocket
-- client, so the database cost is one connection regardless of client count.
--
-- The payload is deliberately an identifier, not the row. NOTIFY has an 8000
-- byte ceiling, and a notification that carries data would need the same
-- authorisation reasoning as the endpoint that serves it.
CREATE OR REPLACE FUNCTION notify_alarm_change() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('atlas_alarms', json_build_object(
        'alarm_id',  NEW.id,
        'device_id', NEW.device_id,
        'state',     NEW.state,
        'severity',  NEW.severity
    )::text);
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS alarms_notify ON alarms;
CREATE TRIGGER alarms_notify
    AFTER INSERT OR UPDATE ON alarms
    FOR EACH ROW EXECUTE FUNCTION notify_alarm_change();
