SELECT s.student_id, s.student_name
FROM student s
JOIN enrollment e ON s.student_id = e.student_id
WHERE s.student_id = 1;

SELECT c.course_name
FROM course c
WHERE c.course_code = 'CS101';

SELECT s.student_name, c.course_name
FROM student s
JOIN enrollment e ON s.student_id = e.student_id
JOIN course c ON e.course_id = c.course_id
WHERE s.student_id = 1;
