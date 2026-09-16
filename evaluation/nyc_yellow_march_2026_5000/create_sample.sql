-- Run this script while connected to: nyc_yellow_march_2026_5000
-- It reads from nyc_tlc_2026.public.yellow_trips through postgres_fdw.
-- The destination database should be new and should not already contain
-- public.yellow_taxi_trips or the _nyc_full_source schema/server.

-- pgAdmin is commonly registered with the postgres login. PostgreSQL will
-- execute the sample-building objects as llm after this role switch.
SET ROLE llm;

CREATE EXTENSION IF NOT EXISTS postgres_fdw;
GRANT USAGE ON FOREIGN DATA WRAPPER postgres_fdw TO llm;
GRANT ALL ON SCHEMA public TO llm;

CREATE SCHEMA _nyc_full_source AUTHORIZATION llm;

CREATE SERVER nyc_full_source_server
FOREIGN DATA WRAPPER postgres_fdw
OPTIONS (
    host '127.0.0.1',
    port '5432',
    dbname 'nyc_tlc_2026'
);

CREATE USER MAPPING FOR llm
SERVER nyc_full_source_server
OPTIONS (user 'llm', password 'llm');

IMPORT FOREIGN SCHEMA public
LIMIT TO (yellow_trips)
FROM SERVER nyc_full_source_server
INTO _nyc_full_source;

-- Build an empty local table with the same 20 TLC columns, plus a temporary
-- hash used only to prevent the same source row from entering two strata.
CREATE TABLE public.yellow_taxi_trips AS
SELECT
    y.*,
    md5(to_jsonb(y)::text) AS _sample_hash
FROM _nyc_full_source.yellow_trips AS y
WITH NO DATA;

CREATE UNIQUE INDEX yellow_taxi_trips_sample_hash_uq
ON public.yellow_taxi_trips (_sample_hash);

-- Stratum A: 200 Midtown Center evening-rush trips for Q1.
INSERT INTO public.yellow_taxi_trips
SELECT y.*, md5(to_jsonb(y)::text)
FROM _nyc_full_source.yellow_trips AS y
WHERE y."PULocationID" = 161
  AND y.tpep_pickup_datetime >= TIMESTAMP '2026-03-18 17:00:00'
  AND y.tpep_pickup_datetime < TIMESTAMP '2026-03-18 19:00:00'
ORDER BY y.tpep_pickup_datetime, y."VendorID", y."DOLocationID"
LIMIT 200
ON CONFLICT (_sample_hash) DO NOTHING;

-- Stratum B: up to 100 Times Square (230) to JFK (132) trips for Q2.
INSERT INTO public.yellow_taxi_trips
SELECT y.*, md5(to_jsonb(y)::text)
FROM _nyc_full_source.yellow_trips AS y
WHERE y."PULocationID" = 230
  AND y."DOLocationID" = 132
  AND y.tpep_pickup_datetime >= TIMESTAMP '2026-03-18 00:00:00'
  AND y.tpep_pickup_datetime < TIMESTAMP '2026-03-19 00:00:00'
ORDER BY y.tpep_pickup_datetime, y."VendorID"
LIMIT 100
ON CONFLICT (_sample_hash) DO NOTHING;

-- Stratum C: 100 stored-and-forwarded trips for Q5.
INSERT INTO public.yellow_taxi_trips
SELECT y.*, md5(to_jsonb(y)::text)
FROM _nyc_full_source.yellow_trips AS y
WHERE y.store_and_fwd_flag = 'Y'
  AND y.tpep_pickup_datetime >= TIMESTAMP '2026-03-18 00:00:00'
  AND y.tpep_pickup_datetime < TIMESTAMP '2026-03-19 00:00:00'
  AND NOT EXISTS (
      SELECT 1
      FROM public.yellow_taxi_trips AS selected
      WHERE selected._sample_hash = md5(to_jsonb(y)::text)
  )
ORDER BY y.tpep_pickup_datetime, y."VendorID", y."PULocationID", y."DOLocationID"
LIMIT 100
ON CONFLICT (_sample_hash) DO NOTHING;

-- Stratum D: 200 unusually long March trips for Q6.
INSERT INTO public.yellow_taxi_trips
SELECT y.*, md5(to_jsonb(y)::text)
FROM _nyc_full_source.yellow_trips AS y
WHERE y.trip_distance >= 50.0
  AND y.tpep_pickup_datetime >= TIMESTAMP '2026-03-01 00:00:00'
  AND y.tpep_pickup_datetime < TIMESTAMP '2026-04-01 00:00:00'
  AND NOT EXISTS (
      SELECT 1
      FROM public.yellow_taxi_trips AS selected
      WHERE selected._sample_hash = md5(to_jsonb(y)::text)
  )
ORDER BY y.trip_distance DESC, y.tpep_pickup_datetime, y."VendorID"
LIMIT 200
ON CONFLICT (_sample_hash) DO NOTHING;

-- Stratum E: the real source rows targeted by Q8, Q9, and Q10.
INSERT INTO public.yellow_taxi_trips
SELECT y.*, md5(to_jsonb(y)::text)
FROM _nyc_full_source.yellow_trips AS y
WHERE
    (
        y."VendorID" = 2
        AND y.tpep_pickup_datetime = TIMESTAMP '2026-03-18 09:00:00'
        AND y.tpep_dropoff_datetime = TIMESTAMP '2026-03-18 09:27:00'
        AND y."PULocationID" = 166
        AND y."DOLocationID" = 230
    )
    OR
    (
        y."VendorID" = 1
        AND y.tpep_pickup_datetime = TIMESTAMP '2026-03-18 07:37:10'
        AND y.tpep_dropoff_datetime = TIMESTAMP '2026-03-18 07:58:31'
        AND y."PULocationID" = 238
        AND y."DOLocationID" = 170
        AND y.store_and_fwd_flag = 'Y'
    )
    OR
    (
        y."VendorID" = 2
        AND y.tpep_pickup_datetime = TIMESTAMP '2026-03-18 22:00:00'
        AND y.tpep_dropoff_datetime = TIMESTAMP '2026-03-18 22:11:31'
        AND y."PULocationID" = 137
        AND y."DOLocationID" = 75
        AND y.trip_distance = 4.11
        AND y.total_amount = 42.72
    )
ON CONFLICT (_sample_hash) DO NOTHING;

-- Fill the remaining capacity with ordinary trips from March 18.
-- Existing sample hashes are excluded so that this insert reaches 5,000 rows.
INSERT INTO public.yellow_taxi_trips
SELECT y.*, md5(to_jsonb(y)::text)
FROM _nyc_full_source.yellow_trips AS y
WHERE y.tpep_pickup_datetime >= TIMESTAMP '2026-03-18 00:00:00'
  AND y.tpep_pickup_datetime < TIMESTAMP '2026-03-19 00:00:00'
  AND NOT EXISTS (
      SELECT 1
      FROM public.yellow_taxi_trips AS selected
      WHERE selected._sample_hash = md5(to_jsonb(y)::text)
  )
ORDER BY md5(to_jsonb(y)::text)
LIMIT (SELECT 5000 - COUNT(*) FROM public.yellow_taxi_trips)
ON CONFLICT (_sample_hash) DO NOTHING;

-- The helper hash is not part of the TLC schema and must not be shown to IlvesBench.
ALTER TABLE public.yellow_taxi_trips DROP COLUMN _sample_hash;

ANALYZE public.yellow_taxi_trips;

-- Required outcome: exactly 5,000 rows and non-zero coverage for every read case.
SELECT COUNT(*) AS total_sample_rows
FROM public.yellow_taxi_trips;

SELECT
    COUNT(*) FILTER (
        WHERE "PULocationID" = 161
          AND tpep_pickup_datetime >= TIMESTAMP '2026-03-18 17:00:00'
          AND tpep_pickup_datetime < TIMESTAMP '2026-03-18 19:00:00'
    ) AS q1_midtown_evening,
    COUNT(*) FILTER (
        WHERE "PULocationID" = 230
          AND "DOLocationID" = 132
          AND tpep_pickup_datetime >= TIMESTAMP '2026-03-18 00:00:00'
          AND tpep_pickup_datetime < TIMESTAMP '2026-03-19 00:00:00'
    ) AS q2_times_square_to_jfk,
    COUNT(*) FILTER (
        WHERE payment_type = 1
          AND tpep_dropoff_datetime >= TIMESTAMP '2026-03-18 00:00:00'
          AND tpep_dropoff_datetime < TIMESTAMP '2026-03-19 00:00:00'
    ) AS q3_credit_card,
    COUNT(*) FILTER (
        WHERE store_and_fwd_flag = 'Y'
          AND tpep_pickup_datetime >= TIMESTAMP '2026-03-18 00:00:00'
          AND tpep_pickup_datetime < TIMESTAMP '2026-03-19 00:00:00'
    ) AS q5_store_and_forward,
    COUNT(*) FILTER (
        WHERE trip_distance >= 50.0
          AND tpep_pickup_datetime >= TIMESTAMP '2026-03-01 00:00:00'
          AND tpep_pickup_datetime < TIMESTAMP '2026-04-01 00:00:00'
    ) AS q6_long_trips
FROM public.yellow_taxi_trips;

-- Required outcome: one matching row for each DML predicate.
SELECT
    COUNT(*) FILTER (
        WHERE "VendorID" = 2
          AND tpep_pickup_datetime = TIMESTAMP '2026-03-18 09:00:00'
          AND tpep_dropoff_datetime = TIMESTAMP '2026-03-18 09:27:00'
          AND "PULocationID" = 166
          AND "DOLocationID" = 230
    ) AS q8_update_target,
    COUNT(*) FILTER (
        WHERE "VendorID" = 1
          AND tpep_pickup_datetime = TIMESTAMP '2026-03-18 07:37:10'
          AND tpep_dropoff_datetime = TIMESTAMP '2026-03-18 07:58:31'
          AND "PULocationID" = 238
          AND "DOLocationID" = 170
          AND store_and_fwd_flag = 'Y'
    ) AS q9_update_target,
    COUNT(*) FILTER (
        WHERE "VendorID" = 2
          AND tpep_pickup_datetime = TIMESTAMP '2026-03-18 22:00:00'
          AND tpep_dropoff_datetime = TIMESTAMP '2026-03-18 22:11:31'
          AND "PULocationID" = 137
          AND "DOLocationID" = 75
          AND trip_distance = 4.11
          AND total_amount = 42.72
    ) AS q10_delete_target
FROM public.yellow_taxi_trips;

-- Remove the temporary link after the local sample has been verified.
DROP SERVER nyc_full_source_server CASCADE;
DROP SCHEMA _nyc_full_source;

RESET ROLE;
