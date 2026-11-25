"""healthcare: department tree, protocol cost history, length of stay, vitals JSONB."""

SLUG = "healthcare"
DOMAIN = "hospital operations: department tree, protocol cost history, encounters, procedures"

DDL = [
    """
    CREATE TABLE departments (
        department_id integer PRIMARY KEY,
        name text NOT NULL,
        parent_id integer REFERENCES departments(department_id),
        building text NOT NULL
    )
    """,
    """
    CREATE TABLE protocol_versions (
        protocol text NOT NULL,
        valid_from timestamptz NOT NULL,
        base_cost numeric(10,2) NOT NULL,
        PRIMARY KEY (protocol, valid_from)
    )
    """,
    """
    CREATE TABLE encounters (
        encounter_id integer PRIMARY KEY,
        department_id integer NOT NULL REFERENCES departments(department_id),
        protocol text NOT NULL,
        admitted_at timestamptz NOT NULL,
        discharged_at timestamptz NOT NULL,
        status text NOT NULL,
        vitals jsonb NOT NULL,
        tags text[] NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE procedures (
        procedure_id integer PRIMARY KEY,
        encounter_id integer NOT NULL REFERENCES encounters(encounter_id),
        code text NOT NULL,
        cost numeric(10,2) NOT NULL
    )
    """,
    """
    CREATE TABLE readmissions (
        readmission_id integer PRIMARY KEY,
        encounter_id integer NOT NULL REFERENCES encounters(encounter_id),
        days_to_readmit integer NOT NULL
    )
    """,
]

FIXTURES = {
    "departments": [
        {"department_id": 1, "name": "Medicine", "parent_id": None, "building": "A"},
        {"department_id": 2, "name": "Cardiology", "parent_id": 1, "building": "A"},
        {"department_id": 3, "name": "Neurology", "parent_id": 1, "building": "B"},
        {"department_id": 4, "name": "Interventional Cardiology", "parent_id": 2, "building": "A"},
        {"department_id": 5, "name": "Epilepsy", "parent_id": 3, "building": "B"},
        {"department_id": 6, "name": "Surgery", "parent_id": None, "building": "C"},
    ],
    "protocol_versions": [
        {"protocol": "chest_pain", "valid_from": "2024-01-01 00:00:00+00", "base_cost": "1200.00"},
        {"protocol": "chest_pain", "valid_from": "2024-03-01 00:00:00+00", "base_cost": "1450.00"},
        {"protocol": "stroke", "valid_from": "2024-01-01 00:00:00+00", "base_cost": "2400.00"},
        {"protocol": "stroke", "valid_from": "2024-02-15 00:00:00+00", "base_cost": "2600.00"},
        {"protocol": "seizure", "valid_from": "2024-02-01 00:00:00+00", "base_cost": "900.00"},
    ],
    "encounters": [
        {"encounter_id": 1, "department_id": 2, "protocol": "chest_pain", "admitted_at": "2024-03-04 22:30:00+00",
         "discharged_at": "2024-03-05 02:30:00+00", "status": "closed",
         "vitals": {"spo2": 95, "heart_rate": 88}, "tags": []},
        {"encounter_id": 2, "department_id": 4, "protocol": "chest_pain", "admitted_at": "2024-03-05 06:00:00+00",
         "discharged_at": "2024-03-08 12:00:00+00", "status": "closed",
         "vitals": {"spo2": 87, "heart_rate": 104}, "tags": ["urgent"]},
        {"encounter_id": 3, "department_id": 3, "protocol": "stroke", "admitted_at": "2024-03-05 23:30:00+00",
         "discharged_at": "2024-03-07 23:30:00+00", "status": "closed",
         "vitals": {"spo2": 92, "heart_rate": 96}, "tags": ["urgent"]},
        {"encounter_id": 4, "department_id": 5, "protocol": "seizure", "admitted_at": "2024-03-07 09:00:00+00",
         "discharged_at": "2024-03-08 09:00:00+00", "status": "closed",
         "vitals": {"spo2": 97, "heart_rate": 72}, "tags": []},
        {"encounter_id": 5, "department_id": 2, "protocol": "chest_pain", "admitted_at": "2024-03-09 18:00:00+00",
         "discharged_at": "2024-03-14 18:00:00+00", "status": "closed",
         "vitals": {"spo2": 89, "heart_rate": 110}, "tags": ["urgent", "isolation"]},
        {"encounter_id": 6, "department_id": 4, "protocol": "chest_pain", "admitted_at": "2024-03-10 20:00:00+00",
         "discharged_at": "2024-03-15 08:00:00+00", "status": "closed",
         "vitals": {"spo2": 94, "heart_rate": 84}, "tags": []},
        {"encounter_id": 7, "department_id": 3, "protocol": "stroke", "admitted_at": "2024-03-11 04:00:00+00",
         "discharged_at": "2024-03-12 04:00:00+00", "status": "closed",
         "vitals": {"spo2": 96, "heart_rate": 78}, "tags": []},
        {"encounter_id": 8, "department_id": 2, "protocol": "chest_pain", "admitted_at": "2024-02-20 10:00:00+00",
         "discharged_at": "2024-02-21 10:00:00+00", "status": "closed",
         "vitals": {"spo2": 98, "heart_rate": 70}, "tags": []},
    ],
    "procedures": [
        {"procedure_id": 1, "encounter_id": 1, "code": "ECG", "cost": "120.00"},
        {"procedure_id": 2, "encounter_id": 2, "code": "ANGIO", "cost": "2400.00"},
        {"procedure_id": 3, "encounter_id": 2, "code": "ECG", "cost": "120.00"},
        {"procedure_id": 4, "encounter_id": 3, "code": "CT", "cost": "900.00"},
        {"procedure_id": 5, "encounter_id": 5, "code": "ECG", "cost": "120.00"},
        {"procedure_id": 6, "encounter_id": 5, "code": "MRI", "cost": "1800.00"},
        {"procedure_id": 7, "encounter_id": 6, "code": "ANGIO", "cost": "2400.00"},
        {"procedure_id": 8, "encounter_id": 7, "code": "CT", "cost": "1260.00"},
        {"procedure_id": 9, "encounter_id": 8, "code": "ECG", "cost": "120.00"},
    ],
    "readmissions": [
        {"readmission_id": 1, "encounter_id": 2, "days_to_readmit": 12},
        {"readmission_id": 2, "encounter_id": 2, "days_to_readmit": 40},
        {"readmission_id": 3, "encounter_id": 5, "days_to_readmit": 21},
    ],
}

QUESTIONS = [
    {
        "skill": "grain_fanout_antijoin",
        "difficulty": "challenging",
        "question": (
            "For every encounter that has at least one readmission, return the encounter id and the total procedure "
            "cost, counting each procedure exactly once even when the encounter has several readmissions."
        ),
        "sql": """
            SELECT e.encounter_id,
                   SUM(p.cost) AS procedure_cost
            FROM encounters AS e
            JOIN procedures AS p ON p.encounter_id = e.encounter_id
            WHERE EXISTS (SELECT 1 FROM readmissions AS r WHERE r.encounter_id = e.encounter_id)
            GROUP BY e.encounter_id
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; one row per encounter in scope.",
    },
    {
        "skill": "topn_ties",
        "difficulty": "challenging",
        "question": (
            "Which two departments have the highest total procedure cost across their encounters? If departments "
            "tie, the smaller department_id comes first. Return department_id and total cost, highest first."
        ),
        "sql": """
            SELECT e.department_id,
                   SUM(p.cost) AS total_cost
            FROM encounters AS e
            JOIN procedures AS p ON p.encounter_id = e.encounter_id
            GROUP BY e.department_id
            ORDER BY total_cost DESC, e.department_id
            LIMIT 2
        """,
        "comparison": "ordered",
        "tie_policy": "Total cost descending; ties broken by the smaller department_id (ORDER BY total_cost DESC, department_id).",
    },
    {
        "skill": "asof_state",
        "difficulty": "challenging",
        "question": (
            "For every encounter, which protocol base cost was in force at the moment of admission? Return "
            "encounter_id, protocol and base_cost."
        ),
        "sql": """
            SELECT e.encounter_id,
                   e.protocol,
                   v.base_cost
            FROM encounters AS e
            JOIN LATERAL (
                SELECT pv.base_cost
                FROM protocol_versions AS pv
                WHERE pv.protocol = e.protocol
                  AND pv.valid_from <= e.admitted_at
                ORDER BY pv.valid_from DESC
                LIMIT 1
            ) AS v ON true
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the protocol version in force is unique because (protocol, valid_from) is unique.",
    },
    {
        "skill": "periods_timezones",
        "difficulty": "challenging",
        "question": (
            "Group encounters by calendar day of admission in Berlin local time (Europe/Berlin) for 4 March 2024 "
            "through 10 March 2024 inclusive and return the local day, the number of encounters and the average "
            "length of stay in hours rounded to two decimals."
        ),
        "sql": """
            SELECT (e.admitted_at AT TIME ZONE 'Europe/Berlin')::date AS local_day,
                   COUNT(*) AS encounters,
                   ROUND(AVG(EXTRACT(EPOCH FROM (e.discharged_at - e.admitted_at)) / 3600.0), 2) AS avg_stay_hours
            FROM encounters AS e
            WHERE (e.admitted_at AT TIME ZONE 'Europe/Berlin')::date BETWEEN DATE '2024-03-04' AND DATE '2024-03-10'
            GROUP BY local_day
            ORDER BY local_day
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the grouping key is the local admission day.",
    },
    {
        "skill": "window_frames_cohorts",
        "difficulty": "challenging",
        "question": (
            "For every encounter, return encounter_id and the number of encounters in the same department over the "
            "window of that encounter's admission time and the seven days before it, both ends inclusive."
        ),
        "sql": """
            SELECT e.encounter_id,
                   COUNT(*) OVER (
                       PARTITION BY e.department_id
                       ORDER BY e.admitted_at
                       RANGE BETWEEN INTERVAL '7 days' PRECEDING AND CURRENT ROW
                   ) AS encounters_7d
            FROM encounters AS e
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the frame is RANGE INTERVAL '7 days' PRECEDING to CURRENT ROW.",
    },
    {
        "skill": "jsonb_arrays",
        "difficulty": "moderate",
        "question": (
            "Which encounters have a recorded oxygen saturation below 90 and carry the tag urgent? Return "
            "encounter_id, the saturation value as a number and how many tags the encounter has."
        ),
        "sql": """
            SELECT e.encounter_id,
                   (e.vitals ->> 'spo2')::numeric AS spo2,
                   cardinality(e.tags) AS tag_count
            FROM encounters AS e
            WHERE (e.vitals ->> 'spo2')::numeric < 90
              AND e.tags @> ARRAY['urgent']
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison, row order is not part of the answer.",
    },
    {
        "skill": "compositional",
        "difficulty": "challenging",
        "question": (
            "Among encounters that have at least one procedure, what share stayed longer than 72 hours? Return one "
            "row with the share between 0 and 1, rounded to four decimals."
        ),
        "sql": """
            SELECT ROUND(
                       COUNT(*) FILTER (WHERE e.discharged_at - e.admitted_at > INTERVAL '72 hours')::numeric
                       / NULLIF(COUNT(*), 0),
                       4
                   ) AS long_stay_share
            FROM encounters AS e
            WHERE EXISTS (SELECT 1 FROM procedures AS p WHERE p.encounter_id = e.encounter_id)
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: the result is a single row.",
    },
    {
        "skill": "hierarchies",
        "difficulty": "challenging",
        "question": (
            "Starting at the department 'Medicine', list that department and every department below it with the depth "
            "relative to Medicine (Medicine = 0) and the number of encounters admitted directly to each department. "
            "Order by depth, then department_id."
        ),
        "sql": """
            WITH RECURSIVE subtree AS (
                SELECT d.department_id, d.name, 0 AS depth
                FROM departments AS d
                WHERE d.name = 'Medicine'
                UNION ALL
                SELECT child.department_id, child.name, parent.depth + 1
                FROM departments AS child
                JOIN subtree AS parent ON child.parent_id = parent.department_id
            )
            SELECT s.department_id,
                   s.name,
                   s.depth,
                   COUNT(e.encounter_id) AS encounters
            FROM subtree AS s
            LEFT JOIN encounters AS e ON e.department_id = s.department_id
            GROUP BY s.department_id, s.name, s.depth
            ORDER BY s.depth, s.department_id
        """,
        "comparison": "ordered",
        "tie_policy": "Depth ascending, then department_id ascending; both keys together are unique in the result.",
    },
]

DROPPED = []

NOTES = [
    "Encounter 2 has two readmissions and two procedures, so a plain join doubles the cost.",
    "The chest_pain protocol cost changes on 2024-03-01, which separates an as-of answer from the current cost.",
    "Admissions at 22:30 and 23:30 UTC fall on different Berlin days than in UTC.",
    "Departments 2 and 3 both total 2160.00, so the top-2 tie-break (smaller department_id) is exercised at the cutoff.",
]
