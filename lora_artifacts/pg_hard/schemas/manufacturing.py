"""manufacturing: machine tree, spec tolerance history, work orders, operations, defects."""

SLUG = "manufacturing"
DOMAIN = "factory production: machine tree, spec tolerance history, work orders, operations, defects"

DDL = [
    """
    CREATE TABLE machines (
        machine_id integer PRIMARY KEY,
        name text NOT NULL,
        parent_id integer REFERENCES machines(machine_id),
        plant text NOT NULL
    )
    """,
    """
    CREATE TABLE spec_history (
        spec text NOT NULL,
        valid_from timestamptz NOT NULL,
        tolerance_mm numeric(6,3) NOT NULL,
        PRIMARY KEY (spec, valid_from)
    )
    """,
    """
    CREATE TABLE work_orders (
        order_id integer PRIMARY KEY,
        machine_id integer NOT NULL REFERENCES machines(machine_id),
        spec text NOT NULL,
        started_at timestamptz NOT NULL,
        finished_at timestamptz NOT NULL,
        status text NOT NULL,
        attributes jsonb NOT NULL,
        tags text[] NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE operations (
        operation_id integer PRIMARY KEY,
        order_id integer NOT NULL REFERENCES work_orders(order_id),
        step integer NOT NULL,
        duration_min numeric(8,2) NOT NULL
    )
    """,
    """
    CREATE TABLE defects (
        defect_id integer PRIMARY KEY,
        order_id integer NOT NULL REFERENCES work_orders(order_id),
        code text NOT NULL,
        quantity integer NOT NULL
    )
    """,
]

FIXTURES = {
    "machines": [
        {"machine_id": 1, "name": "Plant A", "parent_id": None, "plant": "A"},
        {"machine_id": 2, "name": "Line A1", "parent_id": 1, "plant": "A"},
        {"machine_id": 3, "name": "Line A2", "parent_id": 1, "plant": "A"},
        {"machine_id": 4, "name": "Press 1", "parent_id": 2, "plant": "A"},
        {"machine_id": 5, "name": "Press 2", "parent_id": 2, "plant": "A"},
        {"machine_id": 6, "name": "Mill 1", "parent_id": 3, "plant": "A"},
        {"machine_id": 7, "name": "Plant B", "parent_id": None, "plant": "B"},
    ],
    "spec_history": [
        {"spec": "bracket", "valid_from": "2024-01-01 00:00:00+00", "tolerance_mm": "0.050"},
        {"spec": "bracket", "valid_from": "2024-03-01 00:00:00+00", "tolerance_mm": "0.030"},
        {"spec": "housing", "valid_from": "2024-02-01 00:00:00+00", "tolerance_mm": "0.100"},
        {"spec": "shaft", "valid_from": "2024-01-01 00:00:00+00", "tolerance_mm": "0.020"},
    ],
    "work_orders": [
        {"order_id": 1, "machine_id": 4, "spec": "bracket", "started_at": "2024-02-20 09:00:00+00",
         "finished_at": "2024-02-20 12:00:00+00", "status": "closed",
         "attributes": {"line": "A", "shift": "early"}, "tags": []},
        {"order_id": 2, "machine_id": 4, "spec": "bracket", "started_at": "2024-03-04 22:30:00+00",
         "finished_at": "2024-03-05 02:30:00+00", "status": "closed",
         "attributes": {"line": "A", "shift": "late"}, "tags": ["rework"]},
        {"order_id": 3, "machine_id": 5, "spec": "bracket", "started_at": "2024-03-05 10:00:00+00",
         "finished_at": "2024-03-05 13:00:00+00", "status": "closed",
         "attributes": {"line": "A", "shift": "early"}, "tags": []},
        {"order_id": 4, "machine_id": 5, "spec": "bracket", "started_at": "2024-03-06 23:30:00+00",
         "finished_at": "2024-03-07 04:00:00+00", "status": "closed",
         "attributes": {"line": "B", "shift": "late"}, "tags": ["rework"]},
        {"order_id": 5, "machine_id": 6, "spec": "housing", "started_at": "2024-03-08 08:00:00+00",
         "finished_at": "2024-03-08 15:00:00+00", "status": "closed",
         "attributes": {"line": "A", "shift": "early"}, "tags": []},
        {"order_id": 6, "machine_id": 4, "spec": "bracket", "started_at": "2024-03-10 06:00:00+00",
         "finished_at": "2024-03-10 09:00:00+00", "status": "closed",
         "attributes": {"line": "B", "shift": "early"}, "tags": []},
        {"order_id": 7, "machine_id": 6, "spec": "housing", "started_at": "2024-03-11 12:00:00+00",
         "finished_at": "2024-03-11 18:00:00+00", "status": "scrapped",
         "attributes": {"line": "A", "shift": "early"}, "tags": ["rework"]},
    ],
    "operations": [
        {"operation_id": 1, "order_id": 1, "step": 1, "duration_min": "45.00"},
        {"operation_id": 2, "order_id": 1, "step": 2, "duration_min": "30.00"},
        {"operation_id": 3, "order_id": 2, "step": 1, "duration_min": "60.00"},
        {"operation_id": 4, "order_id": 2, "step": 2, "duration_min": "50.00"},
        {"operation_id": 5, "order_id": 2, "step": 3, "duration_min": "35.00"},
        {"operation_id": 6, "order_id": 3, "step": 1, "duration_min": "40.00"},
        {"operation_id": 7, "order_id": 3, "step": 2, "duration_min": "25.00"},
        {"operation_id": 8, "order_id": 4, "step": 1, "duration_min": "70.00"},
        {"operation_id": 9, "order_id": 4, "step": 2, "duration_min": "40.00"},
        {"operation_id": 10, "order_id": 5, "step": 1, "duration_min": "90.00"},
        {"operation_id": 11, "order_id": 5, "step": 2, "duration_min": "60.00"},
        {"operation_id": 12, "order_id": 6, "step": 1, "duration_min": "55.00"},
        {"operation_id": 13, "order_id": 7, "step": 1, "duration_min": "80.00"},
        {"operation_id": 14, "order_id": 7, "step": 2, "duration_min": "45.00"},
    ],
    "defects": [
        {"defect_id": 1, "order_id": 2, "code": "burr", "quantity": 3},
        {"defect_id": 2, "order_id": 2, "code": "crack", "quantity": 2},
        {"defect_id": 3, "order_id": 4, "code": "burr", "quantity": 6},
        {"defect_id": 4, "order_id": 6, "code": "crack", "quantity": 1},
        {"defect_id": 5, "order_id": 7, "code": "crack", "quantity": 6},
    ],
}

QUESTIONS = [
    {
        "skill": "grain_fanout_antijoin",
        "difficulty": "challenging",
        "question": (
            "For every work order that has at least one defect, return the order id and the total operation minutes, "
            "counting each operation exactly once even when the order has several defects."
        ),
        "sql": """
            SELECT o.order_id,
                   SUM(op.duration_min) AS operation_minutes
            FROM work_orders AS o
            JOIN operations AS op ON op.order_id = o.order_id
            WHERE EXISTS (SELECT 1 FROM defects AS d WHERE d.order_id = o.order_id)
            GROUP BY o.order_id
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; one row per order in scope.",
    },
    {
        "skill": "topn_ties",
        "difficulty": "challenging",
        "question": (
            "Which two machines produced the most defect units? If machines tie, the smaller machine_id comes first. "
            "Return machine_id and defect_units, highest first."
        ),
        "sql": """
            SELECT o.machine_id,
                   SUM(d.quantity) AS defect_units
            FROM work_orders AS o
            JOIN defects AS d ON d.order_id = o.order_id
            GROUP BY o.machine_id
            ORDER BY defect_units DESC, o.machine_id
            LIMIT 2
        """,
        "comparison": "ordered",
        "tie_policy": "Defect units descending; ties broken by the smaller machine_id (ORDER BY defect_units DESC, machine_id).",
    },
    {
        "skill": "asof_state",
        "difficulty": "challenging",
        "question": (
            "For every work order, which tolerance in millimetres was in force for its spec at the moment the order "
            "started? Return order_id, spec and tolerance_mm."
        ),
        "sql": """
            SELECT o.order_id,
                   o.spec,
                   s.tolerance_mm
            FROM work_orders AS o
            JOIN LATERAL (
                SELECT sh.tolerance_mm
                FROM spec_history AS sh
                WHERE sh.spec = o.spec
                  AND sh.valid_from <= o.started_at
                ORDER BY sh.valid_from DESC
                LIMIT 1
            ) AS s ON true
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the tolerance in force is unique because (spec, valid_from) is unique.",
    },
    {
        "skill": "periods_timezones",
        "difficulty": "moderate",
        "question": (
            "Count work orders per calendar day in Sao Paulo local time (America/Sao_Paulo, UTC-3) for 4 March 2024 "
            "through 10 March 2024 inclusive. Return the local day and the count, only for days with at least one "
            "order."
        ),
        "sql": """
            SELECT (o.started_at AT TIME ZONE 'America/Sao_Paulo')::date AS local_day,
                   COUNT(*) AS orders
            FROM work_orders AS o
            WHERE (o.started_at AT TIME ZONE 'America/Sao_Paulo')::date BETWEEN DATE '2024-03-04' AND DATE '2024-03-10'
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
            "For every closed work order, return order_id and how many defect units the same machine contributed over "
            "the window of that order's start time and the seven days before it, both ends inclusive."
        ),
        "sql": """
            WITH per_order AS (
                SELECT o.order_id,
                       o.machine_id,
                       o.started_at,
                       COALESCE(SUM(d.quantity), 0) AS defect_units
                FROM work_orders AS o
                LEFT JOIN defects AS d ON d.order_id = o.order_id
                WHERE o.status = 'closed'
                GROUP BY o.order_id, o.machine_id, o.started_at
            )
            SELECT order_id,
                   SUM(defect_units) OVER (
                       PARTITION BY machine_id
                       ORDER BY started_at
                       RANGE BETWEEN INTERVAL '7 days' PRECEDING AND CURRENT ROW
                   ) AS defect_units_7d
            FROM per_order
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the frame is RANGE INTERVAL '7 days' PRECEDING to CURRENT ROW.",
    },
    {
        "skill": "jsonb_arrays",
        "difficulty": "moderate",
        "question": (
            "Which work orders ran on line A and carry the rework tag? Return order_id, the shift attribute and how "
            "many tags the order has."
        ),
        "sql": """
            SELECT o.order_id,
                   o.attributes ->> 'shift' AS shift,
                   cardinality(o.tags) AS tag_count
            FROM work_orders AS o
            WHERE o.attributes ->> 'line' = 'A'
              AND o.tags @> ARRAY['rework']
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison, row order is not part of the answer.",
    },
    {
        "skill": "compositional",
        "difficulty": "challenging",
        "question": (
            "Among work orders whose operations take more than 60 minutes in total, what share has at least one "
            "defect? Return one row with the share between 0 and 1, rounded to four decimals."
        ),
        "sql": """
            WITH durations AS (
                SELECT op.order_id, SUM(op.duration_min) AS minutes
                FROM operations AS op
                GROUP BY op.order_id
            )
            SELECT ROUND(
                       COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM defects AS d WHERE d.order_id = durations.order_id))::numeric
                       / NULLIF(COUNT(*), 0),
                       4
                   ) AS defect_share
            FROM durations
            WHERE durations.minutes > 60
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: the result is a single row.",
    },
    {
        "skill": "hierarchies",
        "difficulty": "challenging",
        "question": (
            "Starting at 'Plant A', list that node and every machine below it with the depth relative to Plant A "
            "(Plant A = 0) and the number of work orders started on each machine. Order by depth, then machine_id."
        ),
        "sql": """
            WITH RECURSIVE subtree AS (
                SELECT m.machine_id, m.name, 0 AS depth
                FROM machines AS m
                WHERE m.name = 'Plant A'
                UNION ALL
                SELECT child.machine_id, child.name, parent.depth + 1
                FROM machines AS child
                JOIN subtree AS parent ON child.parent_id = parent.machine_id
            )
            SELECT s.machine_id,
                   s.name,
                   s.depth,
                   COUNT(o.order_id) AS orders
            FROM subtree AS s
            LEFT JOIN work_orders AS o ON o.machine_id = s.machine_id
            GROUP BY s.machine_id, s.name, s.depth
            ORDER BY s.depth, s.machine_id
        """,
        "comparison": "ordered",
        "tie_policy": "Depth ascending, then machine_id ascending; both keys together are unique in the result.",
    },
]

DROPPED = []

NOTES = [
    "Order 2 has two defects and three operations, so a plain join multiplies the minutes.",
    "The bracket tolerance tightens on 2024-03-01, separating an as-of answer from the current tolerance.",
    "Orders starting at 22:30 and 23:30 UTC fall on different Sao Paulo days than in UTC.",
    "Machines 4, 5 and 6 all produce 6 defect units, so the top-2 tie-break (smaller machine_id) decides who is excluded.",
]
