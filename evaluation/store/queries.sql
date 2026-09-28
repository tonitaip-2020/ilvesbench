-- Q1: Monthly sales and order volume by marketing channel.
SELECT
    date_trunc('month', o.orderdate) AS order_month,
    ca.channel,
    COUNT(*) AS order_count,
    SUM(o.numunits) AS units_sold,
    SUM(o.totalprice)::numeric(14, 2) AS revenue
FROM orders AS o
JOIN campaigns AS ca ON ca.campaignid = o.campaignid
GROUP BY date_trunc('month', o.orderdate), ca.channel
ORDER BY order_month, revenue DESC;

-- Q2: Best-selling product groups in the most recent 90 days of data.
SELECT
    p.groupname,
    COUNT(DISTINCT ol.orderid) AS orders,
    SUM(ol.numunits) AS units_sold,
    SUM(ol.totalprice)::numeric(14, 2) AS revenue
FROM orderlines AS ol
JOIN products AS p ON p.productid = ol.productid
WHERE ol.billdate >= (SELECT MAX(billdate) - INTERVAL '90 days' FROM orderlines)
GROUP BY p.groupcode, p.groupname
ORDER BY revenue DESC, units_sold DESC
LIMIT 20;

-- Q3: Campaign effectiveness, including average order value.
SELECT
    ca.campaignname, ca.channel, ca.discount, ca.freeshippingflag,
    COUNT(o.orderid) AS order_count,
    COUNT(DISTINCT o.customerid) AS customer_count,
    SUM(o.totalprice)::numeric(14, 2) AS revenue,
    AVG(o.totalprice)::numeric(12, 2) AS average_order_value
FROM campaigns AS ca
LEFT JOIN orders AS o ON o.campaignid = ca.campaignid
GROUP BY ca.campaignid, ca.campaignname, ca.channel,
         ca.discount, ca.freeshippingflag
ORDER BY revenue DESC NULLS LAST, order_count DESC;

-- Q4: Geographic sales performance enriched with ZIP-level census indicators.
SELECT
    o.state, o.zipcode, z.county, z.medianage, z.totpop,
    COUNT(*) AS order_count,
    SUM(o.totalprice)::numeric(14, 2) AS revenue,
    AVG(o.totalprice)::numeric(12, 2) AS average_order_value
FROM orders AS o
LEFT JOIN zipcensus AS z
       ON z.zcta5 = o.zipcode AND lower(z.state) = lower(o.state)
GROUP BY o.state, o.zipcode, z.county, z.medianage, z.totpop
ORDER BY revenue DESC
LIMIT 100;

-- Q5: Customer spend change between the latest two 90-day periods.
WITH bounds AS (
    SELECT MAX(orderdate) AS max_orderdate FROM orders
), customer_period_sales AS (
    SELECT
        o.customerid,
        SUM(o.totalprice) FILTER (
            WHERE o.orderdate > b.max_orderdate - INTERVAL '90 days'
        ) AS current_period_sales,
        SUM(o.totalprice) FILTER (
            WHERE o.orderdate > b.max_orderdate - INTERVAL '180 days'
              AND o.orderdate <= b.max_orderdate - INTERVAL '90 days'
        ) AS previous_period_sales
    FROM orders AS o CROSS JOIN bounds AS b
    GROUP BY o.customerid
)
SELECT
    c.customerid, c.gender,
    cps.current_period_sales::numeric(14, 2),
    cps.previous_period_sales::numeric(14, 2),
    (cps.current_period_sales - cps.previous_period_sales)::numeric(14, 2)
        AS sales_change
FROM customer_period_sales AS cps
JOIN customers AS c ON c.customerid = cps.customerid
WHERE COALESCE(cps.current_period_sales, 0) > 0
ORDER BY sales_change DESC
LIMIT 100;

-- Q6: Frequently co-purchased product pairs (market-basket analysis).
SELECT
    p1.groupname AS product_group_1, p1.name AS product_1,
    p2.groupname AS product_group_2, p2.name AS product_2,
    COUNT(DISTINCT ol1.orderid) AS orders_together,
    SUM(LEAST(ol1.numunits, ol2.numunits)) AS paired_units
FROM orderlines AS ol1
JOIN orderlines AS ol2
  ON ol2.orderid = ol1.orderid AND ol2.productid > ol1.productid
JOIN products AS p1 ON p1.productid = ol1.productid
JOIN products AS p2 ON p2.productid = ol2.productid
GROUP BY p1.groupname, p1.name, p2.groupname, p2.name
HAVING COUNT(DISTINCT ol1.orderid) >= 5
ORDER BY orders_together DESC, paired_units DESC
LIMIT 100;

-- Q7: Rank each customer's three largest orders and inter-order interval.
WITH ordered_customer_orders AS (
    SELECT
        o.customerid, o.orderid, o.orderdate, o.totalprice,
        ROW_NUMBER() OVER (
            PARTITION BY o.customerid ORDER BY o.totalprice DESC, o.orderid
        ) AS spend_rank,
        LAG(o.orderdate) OVER (
            PARTITION BY o.customerid ORDER BY o.orderdate, o.orderid
        ) AS prior_order_date
    FROM orders AS o
)
SELECT
    c.customerid, c.gender, oco.orderid, oco.orderdate,
    oco.totalprice::numeric(12, 2) AS order_value,
    oco.spend_rank, oco.orderdate - oco.prior_order_date AS days_since_prior_order
FROM ordered_customer_orders AS oco
JOIN customers AS c ON c.customerid = oco.customerid
WHERE oco.spend_rank <= 3
ORDER BY c.customerid, oco.spend_rank;

-- Q8: INSERT a controlled campaign record (validate in a transaction, then roll back).
INSERT INTO campaigns (campaignid, campaignname, channel, discount, freeshippingflag)
SELECT MAX(campaignid) + 1, 'Evaluation Test Campaign', 'Email', 15, 'N'
FROM campaigns;

-- Q9: UPDATE products with no sales in the latest 180-day observation period.
UPDATE products AS p
SET isinstock = 'N'
WHERE p.isinstock = 'Y'
  AND NOT EXISTS (
      SELECT 1
      FROM orderlines AS ol
      WHERE ol.productid = p.productid
        AND ol.billdate >= (
            SELECT MAX(billdate) - INTERVAL '180 days' FROM orderlines
        )
  );

-- Q10: DELETE the controlled campaign record (validate in a transaction, then roll back).
DELETE FROM campaigns AS ca
WHERE ca.campaignname = 'Evaluation Test Campaign'
  AND NOT EXISTS (
      SELECT 1 FROM orders AS o WHERE o.campaignid = ca.campaignid
  );
