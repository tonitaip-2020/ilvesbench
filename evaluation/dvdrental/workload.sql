-- Q1: Films and actors.
SELECT f.film_id, f.title, a.actor_id, a.first_name, a.last_name
FROM film AS f
JOIN film_actor AS fa ON fa.film_id = f.film_id
JOIN actor AS a ON a.actor_id = fa.actor_id
WHERE f.film_id = 1
ORDER BY a.last_name, a.first_name;

-- Q2: Rental history for one customer.
SELECT r.rental_id, r.rental_date, f.title, r.return_date
FROM rental AS r
JOIN inventory AS i ON i.inventory_id = r.inventory_id
JOIN film AS f ON f.film_id = i.film_id
WHERE r.customer_id = 1
ORDER BY r.rental_date DESC
LIMIT 50;

-- Q3: Rental volume by category.
SELECT c.name AS category_name, COUNT(*) AS rental_count
FROM rental AS r
JOIN inventory AS i ON i.inventory_id = r.inventory_id
JOIN film_category AS fc ON fc.film_id = i.film_id
JOIN category AS c ON c.category_id = fc.category_id
GROUP BY c.category_id, c.name
ORDER BY rental_count DESC, category_name;

-- Q4: Revenue by store.
SELECT s.store_id, SUM(p.amount) AS total_revenue
FROM payment AS p
JOIN staff AS s ON s.staff_id = p.staff_id
GROUP BY s.store_id
ORDER BY s.store_id;

-- Q5: Inventory currently available for rental.
SELECT i.inventory_id, f.title, i.store_id
FROM inventory AS i
JOIN film AS f ON f.film_id = i.film_id
WHERE NOT EXISTS (
  SELECT 1
  FROM rental AS r
  WHERE r.inventory_id = i.inventory_id
    AND r.return_date IS NULL
)
ORDER BY i.inventory_id
LIMIT 100;

-- Q6: Explicit 1NF case: films with a selected array feature.
SELECT film_id, title
FROM film
WHERE 'Trailers' = ANY(special_features)
ORDER BY film_id
LIMIT 100;

-- Q7: Customers with their geographic hierarchy.
SELECT cu.customer_id, cu.first_name, cu.last_name,
       a.address, ci.city, co.country
FROM customer AS cu
JOIN address AS a ON a.address_id = cu.address_id
JOIN city AS ci ON ci.city_id = a.city_id
JOIN country AS co ON co.country_id = ci.country_id
WHERE cu.activebool = TRUE
ORDER BY cu.customer_id
LIMIT 100;

-- Q8: Rentals retained beyond the film's configured duration.
SELECT r.rental_id, f.title,
       EXTRACT(DAY FROM (r.return_date - r.rental_date)) AS days_held
FROM rental AS r
JOIN inventory AS i ON i.inventory_id = r.inventory_id
JOIN film AS f ON f.film_id = i.film_id
WHERE r.return_date IS NOT NULL
  AND r.return_date > r.rental_date + (f.rental_duration * INTERVAL '1 day')
ORDER BY days_held DESC, r.rental_id
LIMIT 100;

-- Q9: Actors appearing in many films.
SELECT a.actor_id, a.first_name, a.last_name, COUNT(*) AS film_count
FROM actor AS a
JOIN film_actor AS fa ON fa.actor_id = a.actor_id
GROUP BY a.actor_id, a.first_name, a.last_name
HAVING COUNT(*) >= 20
ORDER BY film_count DESC, a.actor_id;

-- Q10: Monthly revenue.
SELECT date_trunc('month', payment_date) AS payment_month,
       COUNT(*) AS payment_count,
       SUM(amount) AS total_amount
FROM payment
GROUP BY date_trunc('month', payment_date)
ORDER BY payment_month;
