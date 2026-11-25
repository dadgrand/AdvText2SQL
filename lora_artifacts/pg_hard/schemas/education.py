"""education: program tree, prerequisite DAG, tuition-rate history, enrollment JSONB."""

SLUG = "education"
DOMAIN = "university records: program tree, course prerequisite DAG, tuition history, enrollments"

DDL = [
    """
    CREATE TABLE programs (
        program_id integer PRIMARY KEY,
        name text NOT NULL,
        parent_id integer REFERENCES programs(program_id),
        level text NOT NULL
    )
    """,
    """
    CREATE TABLE courses (
        course_id integer PRIMARY KEY,
        title text NOT NULL,
        program_id integer NOT NULL REFERENCES programs(program_id),
        credits integer NOT NULL,
        topics text[] NOT NULL DEFAULT '{}',
        attributes jsonb NOT NULL
    )
    """,
    """
    CREATE TABLE tuition_rates (
        valid_from timestamptz NOT NULL,
        rate_per_credit numeric(8,2) NOT NULL,
        PRIMARY KEY (valid_from)
    )
    """,
    """
    CREATE TABLE enrollments (
        enrollment_id integer PRIMARY KEY,
        course_id integer NOT NULL REFERENCES courses(course_id),
        student_id integer NOT NULL,
        enrolled_at timestamptz NOT NULL,
        status text NOT NULL,
        grade numeric(5,2),
        submission jsonb NOT NULL
    )
    """,
    """
    CREATE TABLE prerequisites (
        course_id integer NOT NULL REFERENCES courses(course_id),
        prerequisite_course_id integer NOT NULL REFERENCES courses(course_id),
        PRIMARY KEY (course_id, prerequisite_course_id)
    )
    """,
]

FIXTURES = {
    "programs": [
        {"program_id": 1, "name": "Sciences", "parent_id": None, "level": "faculty"},
        {"program_id": 2, "name": "Computer Science", "parent_id": 1, "level": "department"},
        {"program_id": 3, "name": "Data Science", "parent_id": 1, "level": "department"},
        {"program_id": 4, "name": "CS Bachelor", "parent_id": 2, "level": "program"},
        {"program_id": 5, "name": "DS Master", "parent_id": 3, "level": "program"},
    ],
    "courses": [
        {"course_id": 1, "title": "Programming Basics", "program_id": 4, "credits": 6,
         "topics": ["variables", "loops"], "attributes": {"language": "python", "capacity": 120}},
        {"course_id": 2, "title": "Data Structures", "program_id": 4, "credits": 6,
         "topics": ["indexing", "trees"], "attributes": {"language": "java", "capacity": 90}},
        {"course_id": 3, "title": "Algorithms", "program_id": 4, "credits": 8,
         "topics": ["recursion", "graphs"], "attributes": {"language": "python", "capacity": 80}},
        {"course_id": 4, "title": "Databases", "program_id": 4, "credits": 6,
         "topics": ["sql", "indexing", "transactions"], "attributes": {"language": "sql", "capacity": 70}},
        {"course_id": 5, "title": "Machine Learning", "program_id": 5, "credits": 8,
         "topics": ["recursion", "optimization"], "attributes": {"language": "python", "capacity": 60}},
        {"course_id": 6, "title": "Statistics", "program_id": 5, "credits": 6,
         "topics": ["probability"], "attributes": {"language": "r", "capacity": 75}},
    ],
    "tuition_rates": [
        {"valid_from": "2024-01-01 00:00:00+00", "rate_per_credit": "210.00"},
        {"valid_from": "2024-03-01 00:00:00+00", "rate_per_credit": "235.00"},
    ],
    "enrollments": [
        {"enrollment_id": 1, "course_id": 1, "student_id": 500, "enrolled_at": "2024-02-20 09:00:00+00", "status": "completed", "grade": "91.00",
         "submission": {"late": False, "attempts": 2}},
        {"enrollment_id": 2, "course_id": 1, "student_id": 501, "enrolled_at": "2024-02-20 10:00:00+00", "status": "completed", "grade": "78.00",
         "submission": {"late": True, "attempts": 1}},
        {"enrollment_id": 3, "course_id": 2, "student_id": 500, "enrolled_at": "2024-03-04 22:30:00+00", "status": "completed", "grade": "88.00",
         "submission": {"late": False, "attempts": 3}},
        {"enrollment_id": 4, "course_id": 2, "student_id": 502, "enrolled_at": "2024-03-05 10:00:00+00", "status": "completed", "grade": "88.00",
         "submission": {"late": False, "attempts": 1}},
        {"enrollment_id": 5, "course_id": 3, "student_id": 500, "enrolled_at": "2024-03-05 18:00:00+00", "status": "completed", "grade": "95.00",
         "submission": {"late": True, "attempts": 2}},
        {"enrollment_id": 6, "course_id": 3, "student_id": 501, "enrolled_at": "2024-03-06 23:30:00+00", "status": "completed", "grade": "84.00",
         "submission": {"late": False, "attempts": 1}},
        {"enrollment_id": 7, "course_id": 4, "student_id": 500, "enrolled_at": "2024-03-07 09:00:00+00", "status": "completed", "grade": "92.00",
         "submission": {"late": False, "attempts": 2}},
        {"enrollment_id": 8, "course_id": 4, "student_id": 502, "enrolled_at": "2024-03-08 12:00:00+00", "status": "withdrawn", "grade": None,
         "submission": {"late": True, "attempts": 0}},
        {"enrollment_id": 9, "course_id": 5, "student_id": 501, "enrolled_at": "2024-03-09 18:00:00+00", "status": "completed", "grade": "89.00",
         "submission": {"late": False, "attempts": 1}},
        {"enrollment_id": 10, "course_id": 5, "student_id": 503, "enrolled_at": "2024-03-10 20:00:00+00", "status": "completed", "grade": "93.00",
         "submission": {"late": False, "attempts": 2}},
        {"enrollment_id": 11, "course_id": 6, "student_id": 502, "enrolled_at": "2024-03-11 04:00:00+00", "status": "completed", "grade": "85.00",
         "submission": {"late": False, "attempts": 1}},
        {"enrollment_id": 12, "course_id": 6, "student_id": 503, "enrolled_at": "2024-03-12 10:00:00+00", "status": "completed", "grade": "92.00",
         "submission": {"late": True, "attempts": 2}},
        {"enrollment_id": 13, "course_id": 3, "student_id": 502, "enrolled_at": "2024-03-13 09:00:00+00", "status": "completed", "grade": "95.00",
         "submission": {"late": False, "attempts": 1}},
        {"enrollment_id": 14, "course_id": 2, "student_id": 503, "enrolled_at": "2024-03-13 11:00:00+00", "status": "completed", "grade": "88.00",
         "submission": {"late": False, "attempts": 2}},
        {"enrollment_id": 15, "course_id": 6, "student_id": 504, "enrolled_at": "2024-03-14 15:00:00+00", "status": "completed", "grade": "87.00",
         "submission": {"late": True, "attempts": 1}},
    ],
    "prerequisites": [
        {"course_id": 2, "prerequisite_course_id": 1},
        {"course_id": 3, "prerequisite_course_id": 2},
        {"course_id": 4, "prerequisite_course_id": 2},
        {"course_id": 5, "prerequisite_course_id": 3},
        {"course_id": 5, "prerequisite_course_id": 4},
    ],
}

QUESTIONS = [
    {
        "skill": "grain_fanout_antijoin",
        "difficulty": "challenging",
        "question": (
            "For every course that has at least one prerequisite, return the course id, the number of distinct "
            "enrolled students and the course credits, counting each student once even when the course has several "
            "prerequisites."
        ),
        "sql": """
            SELECT c.course_id,
                   COUNT(DISTINCT e.student_id) AS students,
                   c.credits
            FROM courses AS c
            JOIN enrollments AS e ON e.course_id = c.course_id
            WHERE EXISTS (SELECT 1 FROM prerequisites AS p WHERE p.course_id = c.course_id)
            GROUP BY c.course_id, c.credits
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; one row per course in scope.",
    },
    {
        "skill": "topn_ties",
        "difficulty": "challenging",
        "question": (
            "Which two courses have the highest average grade among completed enrollments, considering only courses "
            "with at least three completed enrollments? If courses tie, the smaller course_id comes first. Return "
            "course_id and average grade rounded to two decimals, highest first."
        ),
        "sql": """
            SELECT e.course_id,
                   ROUND(AVG(e.grade), 2) AS avg_grade
            FROM enrollments AS e
            WHERE e.status = 'completed'
            GROUP BY e.course_id
            HAVING COUNT(*) >= 3
            ORDER BY avg_grade DESC, e.course_id
            LIMIT 2
        """,
        "comparison": "ordered",
        "tie_policy": "Average grade descending; ties broken by the smaller course_id (ORDER BY avg_grade DESC, course_id).",
    },
    {
        "skill": "asof_state",
        "difficulty": "challenging",
        "question": (
            "For every completed enrollment, what is the tuition cost at the per-credit rate in force when the "
            "student enrolled (course credits times that rate, rounded to two decimals)? Return enrollment_id and "
            "tuition_cost."
        ),
        "sql": """
            SELECT e.enrollment_id,
                   ROUND(c.credits * r.rate_per_credit, 2) AS tuition_cost
            FROM enrollments AS e
            JOIN courses AS c ON c.course_id = e.course_id
            JOIN LATERAL (
                SELECT tr.rate_per_credit
                FROM tuition_rates AS tr
                WHERE tr.valid_from <= e.enrolled_at
                ORDER BY tr.valid_from DESC
                LIMIT 1
            ) AS r ON true
            WHERE e.status = 'completed'
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the rate in force is unique because valid_from is unique.",
    },
    {
        "skill": "periods_timezones",
        "difficulty": "moderate",
        "question": (
            "Count enrollments per calendar day in Chicago local time (America/Chicago) for 4 March 2024 through "
            "10 March 2024 inclusive, a week that contains the switch to daylight saving time. Return the local day "
            "and the count, only for days with at least one enrollment."
        ),
        "sql": """
            SELECT (e.enrolled_at AT TIME ZONE 'America/Chicago')::date AS local_day,
                   COUNT(*) AS enrollments
            FROM enrollments AS e
            WHERE (e.enrolled_at AT TIME ZONE 'America/Chicago')::date BETWEEN DATE '2024-03-04' AND DATE '2024-03-10'
            GROUP BY local_day
            ORDER BY local_day
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the grouping key is the local calendar day.",
    },
    {
        "skill": "window_frames_cohorts",
        "difficulty": "challenging",
        "question": (
            "For every enrollment, return enrollment_id and how many enrollments the same course had up to and "
            "including that enrollment, ordered by enrollment time."
        ),
        "sql": """
            SELECT e.enrollment_id,
                   COUNT(*) OVER (
                       PARTITION BY e.course_id
                       ORDER BY e.enrolled_at, e.enrollment_id
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS enrollments_to_date
            FROM enrollments AS e
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the frame is UNBOUNDED PRECEDING to CURRENT ROW with (enrolled_at, enrollment_id) ordering.",
    },
    {
        "skill": "jsonb_arrays",
        "difficulty": "moderate",
        "question": (
            "Which enrollments were submitted late and belong to a course whose topics include indexing or recursion? "
            "Return enrollment_id, the course title and the number of submission attempts."
        ),
        "sql": """
            SELECT e.enrollment_id,
                   c.title,
                   (e.submission ->> 'attempts')::integer AS attempts
            FROM enrollments AS e
            JOIN courses AS c ON c.course_id = e.course_id
            WHERE (e.submission ->> 'late')::boolean
              AND c.topics && ARRAY['indexing', 'recursion']
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison, row order is not part of the answer.",
    },
    {
        "skill": "compositional",
        "difficulty": "challenging",
        "question": (
            "Among completed enrollments, what share has a grade of at least 90? Return one row with the share "
            "between 0 and 1, rounded to four decimals."
        ),
        "sql": """
            SELECT ROUND(
                       COUNT(*) FILTER (WHERE e.grade >= 90)::numeric / NULLIF(COUNT(*), 0),
                       4
                   ) AS high_grade_share
            FROM enrollments AS e
            WHERE e.status = 'completed'
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: the result is a single row.",
    },
    {
        "skill": "hierarchies",
        "difficulty": "challenging",
        "question": (
            "Which courses are direct or indirect prerequisites of 'Databases', and at what smallest depth (a direct "
            "prerequisite has depth 1)? Return course_id, title and depth, ordered by depth, then course_id."
        ),
        "sql": """
            WITH RECURSIVE prereq AS (
                SELECT p.prerequisite_course_id AS course_id, 1 AS depth
                FROM prerequisites AS p
                JOIN courses AS c ON c.course_id = p.course_id
                WHERE c.title = 'Databases'
                UNION
                SELECT p.prerequisite_course_id, child.depth + 1
                FROM prerequisites AS p
                JOIN prereq AS child ON p.course_id = child.course_id
            )
            SELECT c.course_id,
                   c.title,
                   MIN(q.depth) AS depth
            FROM prereq AS q
            JOIN courses AS c ON c.course_id = q.course_id
            GROUP BY c.course_id, c.title
            ORDER BY depth, c.course_id
        """,
        "comparison": "ordered",
        "tie_policy": "Smallest depth ascending, then course_id ascending; the GROUP BY takes the minimum depth over all paths.",
    },
]

DROPPED = []

NOTES = [
    "The prerequisite relation is a DAG: Databases needs Data Structures, which needs Programming Basics.",
    "The per-credit rate changes on 2024-03-01, so enrolments before and after it differ in tuition cost.",
    "Courses 2 and 6 both average 88.00, so the top-2 tie-break (smaller course_id) is exercised at the cutoff.",
]
