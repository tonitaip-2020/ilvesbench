CREATE TABLE StudentCourses (
    student_id INT,
    student_name TEXT,
    course_code VARCHAR(10),
    course_name TEXT,
    department_name TEXT,
    PRIMARY KEY (student_id, course_code)
);

INSERT INTO StudentCourses (student_id, student_name, course_code, course_name, department_name) VALUES
(1, 'Alice Johnson', 'CS101', 'Introduction to Computer Science', 'Computer Science'),
(2, 'Bob Smith', 'MATH201', 'Calculus II', 'Mathematics'),
(3, 'Charlie Brown', 'PHYS301', 'Modern Physics', 'Physics'),
(4, 'Diana Prince', 'HIST102', 'World History', 'History'),
(5, 'Evan Green', 'CHEM401', 'Organic Chemistry', 'Chemistry'),
(6, 'Fiona Blue', 'BIO501', 'Cell Biology', 'Biology'),
(7, 'George White', 'ENG202', 'English Literature', 'English'),
(8, 'Hannah Black', 'ECON301', 'Microeconomics', 'Economics'),
(9, 'Ivy Red', 'PSYCH101', 'Introduction to Psychology', 'Psychology'),
(10, 'Jack Yellow', 'ART201', 'Modern Art', 'Art');