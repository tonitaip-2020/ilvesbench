SELECT t.tconst, t.primarytitle, t.startyear
FROM title_basics AS t
WHERE t.titletype = 'movie'
ORDER BY t.tconst
LIMIT 100;

SELECT t.tconst, t.primarytitle, r.averagerating, r.numvotes
FROM title_basics AS t
JOIN title_ratings AS r ON r.tconst = t.tconst
WHERE r.numvotes >= 10
ORDER BY r.averagerating DESC, r.numvotes DESC, t.tconst
LIMIT 100;

SELECT p.tconst, t.primarytitle, p.nconst, n.primaryname, p.category
FROM title_principals AS p
JOIN title_basics AS t ON t.tconst = p.tconst
JOIN name_basics AS n ON n.nconst = p.nconst
WHERE p.category IN ('actor', 'actress')
ORDER BY p.tconst, p.ordering, p.nconst
LIMIT 100;

SELECT t.tconst, t.primarytitle
FROM title_basics AS t
WHERE 'Comedy' = ANY(string_to_array(t.genres, ','))
ORDER BY t.tconst
LIMIT 100;

SELECT t.tconst, t.primarytitle, n.nconst, n.primaryname
FROM title_crew AS c
JOIN title_basics AS t ON t.tconst = c.tconst
JOIN name_basics AS n ON n.nconst = ANY(string_to_array(c.directors, ','))
ORDER BY t.tconst, n.nconst
LIMIT 100;

SELECT n.nconst, n.primaryname
FROM name_basics AS n
WHERE 'actor' = ANY(string_to_array(n.primaryprofession, ','))
ORDER BY n.nconst
LIMIT 100;

SELECT n.nconst, n.primaryname, t.tconst, t.primarytitle
FROM name_basics AS n
JOIN title_basics AS t ON t.tconst = ANY(string_to_array(n.knownfortitles, ','))
ORDER BY n.nconst, t.tconst
LIMIT 100;

SELECT t.tconst, t.primarytitle, n.nconst, n.primaryname
FROM title_crew AS c
JOIN title_basics AS t ON t.tconst = c.tconst
JOIN name_basics AS n ON n.nconst = ANY(string_to_array(c.writers, ','))
ORDER BY t.tconst, n.nconst
LIMIT 100;

SELECT t.titletype, COUNT(*) AS title_count,
       ROUND(AVG(r.averagerating), 2) AS average_rating
FROM title_basics AS t
JOIN title_ratings AS r ON r.tconst = t.tconst
GROUP BY t.titletype
ORDER BY title_count DESC, t.titletype;

SELECT a.titleid, a.ordering, a.title, a.region, a.language,
       a.isoriginaltitle
FROM title_akas AS a
WHERE a.region IS NOT NULL
ORDER BY a.titleid, a.ordering
LIMIT 100;
