-- Q1: Find trips picked up in Midtown Center (TLC Taxi Zone 161)
-- during the evening rush hour on March 18, 2026.
SELECT
    "VendorID",
    tpep_pickup_datetime,
    tpep_dropoff_datetime,
    passenger_count,
    trip_distance,
    "DOLocationID",
    fare_amount,
    tip_amount,
    total_amount,
    payment_type
FROM yellow_taxi_trips
WHERE "PULocationID" = 161
  AND tpep_pickup_datetime >= TIMESTAMP '2026-03-18 17:00:00'
  AND tpep_pickup_datetime < TIMESTAMP '2026-03-18 19:00:00'
ORDER BY tpep_pickup_datetime DESC, "VendorID", "DOLocationID"
LIMIT 100;

-- Q2: Find trips from Times Sq/Theatre District (zone 230)
-- to JFK Airport (zone 132) on March 18, 2026.
SELECT
    tpep_pickup_datetime,
    tpep_dropoff_datetime,
    passenger_count,
    trip_distance,
    "RatecodeID",
    fare_amount,
    tolls_amount,
    "Airport_fee",
    total_amount
FROM yellow_taxi_trips
WHERE "PULocationID" = 230
  AND "DOLocationID" = 132
  AND tpep_pickup_datetime >= TIMESTAMP '2026-03-18 00:00:00'
  AND tpep_pickup_datetime < TIMESTAMP '2026-03-19 00:00:00'
ORDER BY tpep_pickup_datetime, "VendorID";

-- Q3: Show the most expensive credit-card trips completed on March 18, 2026.
SELECT
    tpep_pickup_datetime,
    tpep_dropoff_datetime,
    "PULocationID",
    "DOLocationID",
    trip_distance,
    fare_amount,
    tip_amount,
    tolls_amount,
    congestion_surcharge,
    "Airport_fee",
    cbd_congestion_fee,
    total_amount
FROM yellow_taxi_trips
WHERE payment_type = 1
  AND tpep_dropoff_datetime >= TIMESTAMP '2026-03-18 00:00:00'
  AND tpep_dropoff_datetime < TIMESTAMP '2026-03-19 00:00:00'
ORDER BY total_amount DESC, tpep_pickup_datetime, "PULocationID", "DOLocationID"
LIMIT 50;

-- Q4: Calculate trip count and collected amount by payment type.
SELECT
    payment_type,
    COUNT(*) AS trip_count,
    SUM(total_amount) AS total_collected,
    AVG(total_amount) AS average_trip_total
FROM yellow_taxi_trips
WHERE tpep_pickup_datetime >= TIMESTAMP '2026-03-18 00:00:00'
  AND tpep_pickup_datetime < TIMESTAMP '2026-03-19 00:00:00'
GROUP BY payment_type
ORDER BY payment_type;

-- Q5: Find trips stored in vehicle memory before being forwarded.
SELECT
    "VendorID",
    tpep_pickup_datetime,
    tpep_dropoff_datetime,
    "PULocationID",
    "DOLocationID",
    trip_distance,
    total_amount
FROM yellow_taxi_trips
WHERE store_and_fwd_flag = 'Y'
  AND tpep_pickup_datetime >= TIMESTAMP '2026-03-18 00:00:00'
  AND tpep_pickup_datetime < TIMESTAMP '2026-03-19 00:00:00'
ORDER BY tpep_pickup_datetime DESC, "VendorID", "PULocationID", "DOLocationID";

-- Q6: Find unusually long trips during March 2026.
SELECT
    "VendorID",
    tpep_pickup_datetime,
    tpep_dropoff_datetime,
    passenger_count,
    "PULocationID",
    "DOLocationID",
    trip_distance,
    fare_amount,
    total_amount
FROM yellow_taxi_trips
WHERE trip_distance >= 50.0
  AND tpep_pickup_datetime >= TIMESTAMP '2026-03-01 00:00:00'
  AND tpep_pickup_datetime < TIMESTAMP '2026-04-01 00:00:00'
ORDER BY trip_distance DESC, tpep_pickup_datetime, "VendorID"
LIMIT 100;

-- Q7: Insert a controlled test trip. Validate inside a transaction and roll back.
INSERT INTO yellow_taxi_trips (
    "VendorID", tpep_pickup_datetime, tpep_dropoff_datetime,
    passenger_count, trip_distance, "RatecodeID", store_and_fwd_flag,
    "PULocationID", "DOLocationID", payment_type, fare_amount, extra,
    mta_tax, tip_amount, tolls_amount, improvement_surcharge,
    total_amount, congestion_surcharge, "Airport_fee", cbd_congestion_fee
)
VALUES (
    2, TIMESTAMP '2026-03-18 14:05:12', TIMESTAMP '2026-03-18 14:27:49',
    1, 6.40, 1, 'N', 161, 236, 1, 28.90, 0.00, 0.50, 6.68,
    0.00, 1.00, 40.08, 2.50, 0.00, 0.50
);

-- Q8: Correct a real, unique March 2026 trip. Validate inside a transaction and roll back.
UPDATE yellow_taxi_trips
SET payment_type = 1,
    tip_amount = 5.25,
    total_amount = 53.89
WHERE "VendorID" = 2
  AND tpep_pickup_datetime = TIMESTAMP '2026-03-18 09:00:00'
  AND tpep_dropoff_datetime = TIMESTAMP '2026-03-18 09:27:00'
  AND "PULocationID" = 166
  AND "DOLocationID" = 230;

-- Q9: Mark a real delayed trip as forwarded. Validate inside a transaction and roll back.
UPDATE yellow_taxi_trips
SET store_and_fwd_flag = 'N'
WHERE "VendorID" = 1
  AND tpep_pickup_datetime = TIMESTAMP '2026-03-18 07:37:10'
  AND tpep_dropoff_datetime = TIMESTAMP '2026-03-18 07:58:31'
  AND "PULocationID" = 238
  AND "DOLocationID" = 170
  AND store_and_fwd_flag = 'Y';

-- Q10: Remove a real, unique March 2026 trip. Validate inside a transaction and roll back.
DELETE FROM yellow_taxi_trips
WHERE "VendorID" = 2
  AND tpep_pickup_datetime = TIMESTAMP '2026-03-18 22:00:00'
  AND tpep_dropoff_datetime = TIMESTAMP '2026-03-18 22:11:31'
  AND "PULocationID" = 137
  AND "DOLocationID" = 75
  AND trip_distance = 4.11
  AND total_amount = 42.72;

