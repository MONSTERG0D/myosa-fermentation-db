-- =====================================================================
--  MYOSA Smart Fermentation Monitor — TimescaleDB schema
--  Stores BMP180 pressure vs time (main signal), GY906 temperature,
--  MPU6050 motion/lid events, and logistic-fit results per run.
--
--  Run once:  psql -h localhost -U myosa -d fermentation -f schema.sql
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ---------------------------------------------------------------------
-- 1) RUNS — one row per fermentation batch (Kilju test, etc.)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS runs (
    run_id              SERIAL PRIMARY KEY,
    name                TEXT        NOT NULL,                 -- e.g. 'Kilju test 1'
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at            TIMESTAMPTZ,                          -- NULL = still running
    sugar_mass_g        REAL        DEFAULT 70,               -- m_s
    water_volume_ml     REAL        DEFAULT 250,              -- V_l
    headspace_volume_ml REAL        DEFAULT 100,              -- V_g
    leak_setting        TEXT,                                 -- micro-leak calibration note
    baseline_hpa        REAL,                                 -- atmospheric P at start (auto-filled)
    notes               TEXT
);

-- ---------------------------------------------------------------------
-- 2) PRESSURE READINGS — BMP180 (pressure vs time, the core data)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pressure_readings (
    time          TIMESTAMPTZ NOT NULL,
    run_id        INT         NOT NULL REFERENCES runs(run_id),
    pressure_hpa  DOUBLE PRECISION NOT NULL
                  CHECK (pressure_hpa BETWEEN 300 AND 1100),  -- BMP180 range
    bmp_temp_c    REAL,                                       -- BMP180 internal temp
    PRIMARY KEY (run_id, time)
);
SELECT create_hypertable('pressure_readings', 'time',
                         chunk_time_interval => INTERVAL '1 day',
                         if_not_exists => TRUE);

-- ---------------------------------------------------------------------
-- 3) TEMPERATURE READINGS — GY906 (MLX90614) IR sensor
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS temperature_readings (
    time        TIMESTAMPTZ NOT NULL,
    run_id      INT         NOT NULL REFERENCES runs(run_id),
    object_c    REAL CHECK (object_c  BETWEEN -70 AND 380),   -- solution temp
    ambient_c   REAL CHECK (ambient_c BETWEEN -40 AND 125),
    PRIMARY KEY (run_id, time)
);
SELECT create_hypertable('temperature_readings', 'time',
                         chunk_time_interval => INTERVAL '1 day',
                         if_not_exists => TRUE);

-- ---------------------------------------------------------------------
-- 4) MOTION READINGS — MPU6050 on the lid (vibration / lid-open alert)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS motion_readings (
    time        TIMESTAMPTZ NOT NULL,
    run_id      INT         NOT NULL REFERENCES runs(run_id),
    ax REAL, ay REAL, az REAL,          -- acceleration, g
    gx REAL, gy REAL, gz REAL,          -- angular rate, deg/s
    accel_mag   REAL,                   -- sqrt(ax²+ay²+az²)
    event       TEXT CHECK (event IN ('none','vibration','lid_open')) DEFAULT 'none',
    PRIMARY KEY (run_id, time)
);
SELECT create_hypertable('motion_readings', 'time',
                         chunk_time_interval => INTERVAL '1 day',
                         if_not_exists => TRUE);

-- ---------------------------------------------------------------------
-- 5) FIT RESULTS — logistic model output, written by fit_logistic.py
--    ΔP(t) = A / (1 + e^{-k (t - t0)})   (gauge pressure above baseline)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fit_results (
    time            TIMESTAMPTZ NOT NULL DEFAULT now(),       -- when the fit was computed
    run_id          INT         NOT NULL REFERENCES runs(run_id),
    n_points        INT,
    baseline_hpa    DOUBLE PRECISION,                         -- P0
    a_hpa           DOUBLE PRECISION,                         -- saturation ΔP (A)
    k_per_hour      DOUBLE PRECISION,                         -- growth rate
    t0_hours        DOUBLE PRECISION,                         -- inflection point (h after start)
    r_squared       DOUBLE PRECISION,
    current_dp_hpa  DOUBLE PRECISION,
    completion_pct  DOUBLE PRECISION,
    phase           TEXT CHECK (phase IN ('lag','rapid','saturation','unknown')),
    eta             TIMESTAMPTZ,                              -- predicted 99 % completion time
    PRIMARY KEY (run_id, time)
);
SELECT create_hypertable('fit_results', 'time', if_not_exists => TRUE);

-- ---------------------------------------------------------------------
-- 6) EVENT / ALERT LOG (lid opened, sensor out of range, etc.)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
    time      TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id    INT REFERENCES runs(run_id),
    source    TEXT NOT NULL,          -- 'bmp180' | 'gy906' | 'mpu6050' | 'fit'
    level     TEXT NOT NULL CHECK (level IN ('info','warning','critical')),
    message   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alerts_run_time_idx ON alerts (run_id, time DESC);

-- ---------------------------------------------------------------------
-- 7) 1-MINUTE AVERAGES (continuous aggregate) — smooth curve for plots/fit
-- ---------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS pressure_1min
WITH (timescaledb.continuous) AS
SELECT run_id,
       time_bucket(INTERVAL '1 minute', time) AS bucket,
       avg(pressure_hpa) AS avg_hpa,
       min(pressure_hpa) AS min_hpa,
       max(pressure_hpa) AS max_hpa,
       count(*)          AS samples
FROM pressure_readings
GROUP BY run_id, bucket
WITH NO DATA;

SELECT add_continuous_aggregate_policy('pressure_1min',
       start_offset      => INTERVAL '1 day',
       end_offset        => INTERVAL '1 minute',
       schedule_interval => INTERVAL '1 minute',
       if_not_exists     => TRUE);

-- ---------------------------------------------------------------------
-- 8) Compression for old chunks (keeps the DB small on a laptop/Pi)
-- ---------------------------------------------------------------------
ALTER TABLE pressure_readings    SET (timescaledb.compress, timescaledb.compress_segmentby = 'run_id');
ALTER TABLE temperature_readings SET (timescaledb.compress, timescaledb.compress_segmentby = 'run_id');
ALTER TABLE motion_readings      SET (timescaledb.compress, timescaledb.compress_segmentby = 'run_id');
SELECT add_compression_policy('pressure_readings',    INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_compression_policy('temperature_readings', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_compression_policy('motion_readings',      INTERVAL '7 days', if_not_exists => TRUE);

-- ---------------------------------------------------------------------
-- 9) LIVE STATUS VIEW — what the OLED / dashboard shows
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW run_status AS
SELECT r.run_id,
       r.name,
       r.started_at,
       p.time          AS last_reading_at,
       p.pressure_hpa  AS last_pressure_hpa,
       p.pressure_hpa - r.baseline_hpa AS gauge_dp_hpa,
       t.object_c      AS solution_temp_c,
       f.completion_pct,
       f.phase,
       f.eta
FROM runs r
LEFT JOIN LATERAL (SELECT time, pressure_hpa FROM pressure_readings
                   WHERE run_id = r.run_id ORDER BY time DESC LIMIT 1) p ON TRUE
LEFT JOIN LATERAL (SELECT object_c FROM temperature_readings
                   WHERE run_id = r.run_id ORDER BY time DESC LIMIT 1) t ON TRUE
LEFT JOIN LATERAL (SELECT completion_pct, phase, eta FROM fit_results
                   WHERE run_id = r.run_id ORDER BY time DESC LIMIT 1) f ON TRUE;
