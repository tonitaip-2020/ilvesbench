-- Q1: Bounded yellow-taxi lookup.
SELECT "VendorID", "PULocationID", "DOLocationID",
       tpep_pickup_datetime, tpep_dropoff_datetime,
       trip_distance, total_amount
FROM yellow_trips
WHERE "VendorID" = 1
  AND "PULocationID" = 132
ORDER BY tpep_pickup_datetime
LIMIT 100;

-- Q2: Yellow-taxi payment distribution.
SELECT payment_type, COUNT(*) AS trip_count,
       AVG(total_amount) AS average_total
FROM yellow_trips
GROUP BY payment_type
ORDER BY payment_type;

-- Q3: Yellow-taxi daily route summary.
SELECT date_trunc('day', tpep_pickup_datetime) AS pickup_day,
       "PULocationID", "DOLocationID",
       COUNT(*) AS trip_count,
       AVG(trip_distance) AS average_distance
FROM yellow_trips
WHERE tpep_pickup_datetime >= TIMESTAMP '2026-01-01 00:00:00'
  AND tpep_pickup_datetime < TIMESTAMP '2026-01-08 00:00:00'
GROUP BY date_trunc('day', tpep_pickup_datetime), "PULocationID", "DOLocationID"
ORDER BY pickup_day, trip_count DESC
LIMIT 250;

-- Q4: Green-taxi totals by vendor and rate code.
SELECT "VendorID", "RatecodeID", COUNT(*) AS trip_count,
       SUM(total_amount) AS total_revenue
FROM green_trips
GROUP BY "VendorID", "RatecodeID"
ORDER BY "VendorID", "RatecodeID";

-- Q5: FHV trip counts by dispatching base.
SELECT dispatching_base_num, COUNT(*) AS trip_count
FROM fhv_trips
WHERE pickup_datetime >= TIMESTAMP '2026-01-01 00:00:00'
  AND pickup_datetime < TIMESTAMP '2026-02-01 00:00:00'
GROUP BY dispatching_base_num
ORDER BY trip_count DESC
LIMIT 100;

-- Q6: High-volume FHV service summary.
SELECT hvfhs_license_num, shared_request_flag, shared_match_flag,
       COUNT(*) AS trip_count,
       AVG(trip_miles) AS average_miles,
       AVG(driver_pay) AS average_driver_pay
FROM fhvhv_trips
WHERE pickup_datetime >= TIMESTAMP '2026-01-01 00:00:00'
  AND pickup_datetime < TIMESTAMP '2026-01-08 00:00:00'
GROUP BY hvfhs_license_num, shared_request_flag, shared_match_flag
ORDER BY trip_count DESC;

-- Q7: Top high-volume FHV routes.
WITH route_counts AS (
  SELECT "PULocationID" AS pickup_location,
         "DOLocationID" AS dropoff_location,
         COUNT(*) AS trip_count
  FROM fhvhv_trips
  WHERE pickup_datetime >= TIMESTAMP '2026-01-01 00:00:00'
    AND pickup_datetime < TIMESTAMP '2026-01-08 00:00:00'
  GROUP BY "PULocationID", "DOLocationID"
)
SELECT pickup_location, dropoff_location, trip_count
FROM route_counts
ORDER BY trip_count DESC, pickup_location, dropoff_location
LIMIT 100;

-- Q8: Airport-fee yellow trips.
SELECT "PULocationID", "DOLocationID", COUNT(*) AS trip_count,
       SUM("Airport_fee") AS airport_fees,
       SUM(total_amount) AS total_amount
FROM yellow_trips
WHERE "Airport_fee" > 0
GROUP BY "PULocationID", "DOLocationID"
ORDER BY trip_count DESC
LIMIT 100;

-- Q9: Fare-component reconciliation sample.
SELECT tpep_pickup_datetime, fare_amount, extra, mta_tax,
       tip_amount, tolls_amount, improvement_surcharge,
       congestion_surcharge, "Airport_fee", total_amount
FROM yellow_trips
WHERE ABS(total_amount - (
    COALESCE(fare_amount, 0) + COALESCE(extra, 0) +
    COALESCE(mta_tax, 0) + COALESCE(tip_amount, 0) +
    COALESCE(tolls_amount, 0) + COALESCE(improvement_surcharge, 0) +
    COALESCE(congestion_surcharge, 0) + COALESCE("Airport_fee", 0)
  )) > 0.01
ORDER BY tpep_pickup_datetime
LIMIT 100;

-- Q10: Cross-service trip totals.
SELECT service_type, SUM(trip_count) AS trip_count
FROM (
  SELECT 'yellow'::text AS service_type, COUNT(*) AS trip_count FROM yellow_trips
  UNION ALL
  SELECT 'green'::text AS service_type, COUNT(*) AS trip_count FROM green_trips
  UNION ALL
  SELECT 'fhv'::text AS service_type, COUNT(*) AS trip_count FROM fhv_trips
  UNION ALL
  SELECT 'fhvhv'::text AS service_type, COUNT(*) AS trip_count FROM fhvhv_trips
) AS service_counts
GROUP BY service_type
ORDER BY service_type;
