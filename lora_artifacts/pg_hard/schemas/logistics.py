"""logistics: warehouse tree, carrier rate history, legs, customs attributes."""

SLUG = "logistics"
DOMAIN = "freight logistics: warehouse tree, carrier rate history, shipment legs, customs data"

DDL = [
    """
    CREATE TABLE warehouses (
        warehouse_id integer PRIMARY KEY,
        name text NOT NULL,
        parent_id integer REFERENCES warehouses(warehouse_id),
        region text NOT NULL
    )
    """,
    """
    CREATE TABLE carrier_rates (
        carrier text NOT NULL,
        valid_from timestamptz NOT NULL,
        rate_per_kg numeric(8,2) NOT NULL,
        PRIMARY KEY (carrier, valid_from)
    )
    """,
    """
    CREATE TABLE shipments (
        shipment_id integer PRIMARY KEY,
        carrier text NOT NULL,
        origin_warehouse_id integer NOT NULL REFERENCES warehouses(warehouse_id),
        shipped_at timestamptz NOT NULL,
        weight_kg numeric(10,2) NOT NULL,
        status text NOT NULL,
        customs jsonb NOT NULL,
        tags text[] NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE shipment_legs (
        leg_id integer PRIMARY KEY,
        shipment_id integer NOT NULL REFERENCES shipments(shipment_id),
        leg_no integer NOT NULL,
        distance_km numeric(10,2) NOT NULL,
        duration_hours numeric(8,2) NOT NULL
    )
    """,
    """
    CREATE TABLE exceptions (
        exception_id integer PRIMARY KEY,
        shipment_id integer NOT NULL REFERENCES shipments(shipment_id),
        kind text NOT NULL,
        cost numeric(10,2) NOT NULL
    )
    """,
]

FIXTURES = {
    "warehouses": [
        {"warehouse_id": 1, "name": "EU Hub", "parent_id": None, "region": "EU"},
        {"warehouse_id": 2, "name": "Rotterdam", "parent_id": 1, "region": "EU"},
        {"warehouse_id": 3, "name": "Hamburg", "parent_id": 1, "region": "EU"},
        {"warehouse_id": 4, "name": "Rotterdam Dock", "parent_id": 2, "region": "EU"},
        {"warehouse_id": 5, "name": "Hamburg Cold", "parent_id": 3, "region": "EU"},
        {"warehouse_id": 6, "name": "APAC Hub", "parent_id": None, "region": "APAC"},
    ],
    "carrier_rates": [
        {"carrier": "Nordic", "valid_from": "2024-01-01 00:00:00+00", "rate_per_kg": "0.42"},
        {"carrier": "Nordic", "valid_from": "2024-03-01 00:00:00+00", "rate_per_kg": "0.55"},
        {"carrier": "Alpine", "valid_from": "2024-01-01 00:00:00+00", "rate_per_kg": "0.38"},
        {"carrier": "Alpine", "valid_from": "2024-02-15 00:00:00+00", "rate_per_kg": "0.47"},
        {"carrier": "Pacific", "valid_from": "2024-02-01 00:00:00+00", "rate_per_kg": "0.61"},
    ],
    "shipments": [
        {"shipment_id": 1, "carrier": "Nordic", "origin_warehouse_id": 4, "shipped_at": "2024-02-20 09:00:00+00",
         "weight_kg": "1200.00", "status": "delivered", "customs": {"status": "cleared", "code": "EU-1"}, "tags": ["priority"]},
        {"shipment_id": 2, "carrier": "Nordic", "origin_warehouse_id": 4, "shipped_at": "2024-03-04 22:30:00+00",
         "weight_kg": "2400.00", "status": "delivered", "customs": {"status": "cleared", "code": "EU-2"}, "tags": ["priority", "cold"]},
        {"shipment_id": 3, "carrier": "Alpine", "origin_warehouse_id": 5, "shipped_at": "2024-03-05 10:00:00+00",
         "weight_kg": "800.00", "status": "delivered", "customs": {"status": "pending", "code": "EU-3"}, "tags": []},
        {"shipment_id": 4, "carrier": "Alpine", "origin_warehouse_id": 5, "shipped_at": "2024-03-06 23:30:00+00",
         "weight_kg": "1600.00", "status": "delivered", "customs": {"status": "cleared", "code": "EU-4"}, "tags": ["priority"]},
        {"shipment_id": 5, "carrier": "Pacific", "origin_warehouse_id": 6, "shipped_at": "2024-03-07 03:00:00+00",
         "weight_kg": "3200.00", "status": "delivered", "customs": {"status": "cleared", "code": "AP-5"}, "tags": ["priority"]},
        {"shipment_id": 6, "carrier": "Pacific", "origin_warehouse_id": 6, "shipped_at": "2024-03-08 12:00:00+00",
         "weight_kg": "950.00", "status": "cancelled", "customs": {"status": "rejected", "code": "AP-6"}, "tags": []},
        {"shipment_id": 7, "carrier": "Nordic", "origin_warehouse_id": 4, "shipped_at": "2024-03-09 18:00:00+00",
         "weight_kg": "450.00", "status": "delivered", "customs": {"status": "cleared", "code": "EU-7"}, "tags": ["cold"]},
        {"shipment_id": 8, "carrier": "Alpine", "origin_warehouse_id": 3, "shipped_at": "2024-03-10 21:00:00+00",
         "weight_kg": "2000.00", "status": "delivered", "customs": {"status": "cleared", "code": "EU-8"}, "tags": ["priority", "oversize"]},
        {"shipment_id": 9, "carrier": "Nordic", "origin_warehouse_id": 2, "shipped_at": "2024-03-11 07:00:00+00",
         "weight_kg": "1100.00", "status": "delivered", "customs": {"status": "cleared", "code": "EU-9"}, "tags": []},
        {"shipment_id": 10, "carrier": "Pacific", "origin_warehouse_id": 6, "shipped_at": "2024-03-12 15:00:00+00",
         "weight_kg": "1200.00", "status": "delivered", "customs": {"status": "cleared", "code": "AP-10"}, "tags": ["priority"]},
    ],
    "shipment_legs": [
        {"leg_id": 1, "shipment_id": 1, "leg_no": 1, "distance_km": "120.00", "duration_hours": "2.50"},
        {"leg_id": 2, "shipment_id": 1, "leg_no": 2, "distance_km": "480.00", "duration_hours": "7.00"},
        {"leg_id": 3, "shipment_id": 2, "leg_no": 1, "distance_km": "300.00", "duration_hours": "4.00"},
        {"leg_id": 4, "shipment_id": 2, "leg_no": 2, "distance_km": "700.00", "duration_hours": "9.50"},
        {"leg_id": 5, "shipment_id": 3, "leg_no": 1, "distance_km": "250.00", "duration_hours": "3.50"},
        {"leg_id": 6, "shipment_id": 4, "leg_no": 1, "distance_km": "640.00", "duration_hours": "8.00"},
        {"leg_id": 7, "shipment_id": 4, "leg_no": 2, "distance_km": "160.00", "duration_hours": "2.00"},
        {"leg_id": 8, "shipment_id": 5, "leg_no": 1, "distance_km": "1500.00", "duration_hours": "21.00"},
        {"leg_id": 9, "shipment_id": 6, "leg_no": 1, "distance_km": "1500.00", "duration_hours": "20.00"},
        {"leg_id": 10, "shipment_id": 7, "leg_no": 1, "distance_km": "90.00", "duration_hours": "1.50"},
        {"leg_id": 11, "shipment_id": 8, "leg_no": 1, "distance_km": "410.00", "duration_hours": "6.00"},
        {"leg_id": 12, "shipment_id": 8, "leg_no": 2, "distance_km": "210.00", "duration_hours": "3.00"},
        {"leg_id": 13, "shipment_id": 9, "leg_no": 1, "distance_km": "330.00", "duration_hours": "5.00"},
        {"leg_id": 14, "shipment_id": 10, "leg_no": 1, "distance_km": "900.00", "duration_hours": "12.00"},
    ],
    "exceptions": [
        {"exception_id": 1, "shipment_id": 2, "kind": "customs_hold", "cost": "150.00"},
        {"exception_id": 2, "shipment_id": 2, "kind": "damage", "cost": "80.00"},
        {"exception_id": 3, "shipment_id": 5, "kind": "weather", "cost": "300.00"},
        {"exception_id": 4, "shipment_id": 6, "kind": "cancelled", "cost": "500.00"},
        {"exception_id": 5, "shipment_id": 8, "kind": "damage", "cost": "120.00"},
    ],
}

QUESTIONS = [
    {
        "skill": "grain_fanout_antijoin",
        "difficulty": "challenging",
        "question": (
            "For every shipment that has at least one exception, return the shipment id and the total leg distance, "
            "counting each leg exactly once even when the shipment has several exceptions."
        ),
        "sql": """
            SELECT s.shipment_id,
                   SUM(l.distance_km) AS total_distance
            FROM shipments AS s
            JOIN shipment_legs AS l ON l.shipment_id = s.shipment_id
            WHERE EXISTS (SELECT 1 FROM exceptions AS e WHERE e.shipment_id = s.shipment_id)
            GROUP BY s.shipment_id
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; one row per shipment in scope.",
    },
    {
        "skill": "topn_ties",
        "difficulty": "challenging",
        "question": (
            "Which two carriers moved the most weight across delivered shipments? If carriers tie, the carrier name "
            "that sorts first alphabetically comes first. Return carrier and total weight, heaviest first."
        ),
        "sql": """
            SELECT s.carrier, SUM(s.weight_kg) AS total_weight
            FROM shipments AS s
            WHERE s.status = 'delivered'
            GROUP BY s.carrier
            ORDER BY total_weight DESC, s.carrier
            LIMIT 2
        """,
        "comparison": "ordered",
        "tie_policy": "Total weight descending; ties broken by carrier name ascending (ORDER BY total_weight DESC, carrier).",
    },
    {
        "skill": "asof_state",
        "difficulty": "challenging",
        "question": (
            "For every delivered shipment, what is its freight cost at the carrier rate that was in force when it "
            "was shipped (weight in kg times that rate per kg, rounded to two decimals)? Return shipment_id and "
            "cost."
        ),
        "sql": """
            SELECT s.shipment_id,
                   ROUND(s.weight_kg * r.rate_per_kg, 2) AS cost
            FROM shipments AS s
            JOIN LATERAL (
                SELECT cr.rate_per_kg
                FROM carrier_rates AS cr
                WHERE cr.carrier = s.carrier
                  AND cr.valid_from <= s.shipped_at
                ORDER BY cr.valid_from DESC
                LIMIT 1
            ) AS r ON true
            WHERE s.status = 'delivered'
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the rate in force is unique because (carrier, valid_from) is unique.",
    },
    {
        "skill": "periods_timezones",
        "difficulty": "moderate",
        "question": (
            "Count delivered shipments per calendar day in Sydney local time (Australia/Sydney, UTC+11 in early "
            "March 2024) for 4 March 2024 through 10 March 2024 inclusive. Return the local day and the count, only "
            "for days with at least one shipment."
        ),
        "sql": """
            SELECT (s.shipped_at AT TIME ZONE 'Australia/Sydney')::date AS local_day,
                   COUNT(*) AS shipments
            FROM shipments AS s
            WHERE s.status = 'delivered'
              AND (s.shipped_at AT TIME ZONE 'Australia/Sydney')::date BETWEEN DATE '2024-03-04' AND DATE '2024-03-10'
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
            "For every delivered shipment, return shipment_id and the carrier's total weight over the window of that "
            "shipment's timestamp and the thirty days before it, both ends inclusive."
        ),
        "sql": """
            SELECT s.shipment_id,
                   SUM(s.weight_kg) OVER (
                       PARTITION BY s.carrier
                       ORDER BY s.shipped_at
                       RANGE BETWEEN INTERVAL '30 days' PRECEDING AND CURRENT ROW
                   ) AS weight_30d
            FROM shipments AS s
            WHERE s.status = 'delivered'
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the frame is RANGE INTERVAL '30 days' PRECEDING to CURRENT ROW.",
    },
    {
        "skill": "jsonb_arrays",
        "difficulty": "moderate",
        "question": (
            "Which shipments have customs status cleared and carry the tag priority? Return shipment_id, the customs "
            "code and how many tags the shipment has."
        ),
        "sql": """
            SELECT s.shipment_id,
                   s.customs ->> 'code' AS customs_code,
                   cardinality(s.tags) AS tag_count
            FROM shipments AS s
            WHERE s.customs ->> 'status' = 'cleared'
              AND s.tags @> ARRAY['priority']
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison, row order is not part of the answer.",
    },
    {
        "skill": "compositional",
        "difficulty": "challenging",
        "question": (
            "Among delivered shipments heavier than 1000 kg, what share has at least one exception? Return one row "
            "with the share between 0 and 1, rounded to four decimals."
        ),
        "sql": """
            SELECT ROUND(
                       COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM exceptions AS e WHERE e.shipment_id = s.shipment_id))::numeric
                       / NULLIF(COUNT(*), 0),
                       4
                   ) AS exception_share
            FROM shipments AS s
            WHERE s.status = 'delivered'
              AND s.weight_kg > 1000
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: the result is a single row.",
    },
    {
        "skill": "hierarchies",
        "difficulty": "challenging",
        "question": (
            "Starting at the warehouse 'EU Hub', list that warehouse and every warehouse below it with the depth "
            "relative to EU Hub (EU Hub = 0) and the number of shipments that originated there. Order by depth, then "
            "warehouse_id."
        ),
        "sql": """
            WITH RECURSIVE subtree AS (
                SELECT w.warehouse_id, w.name, 0 AS depth
                FROM warehouses AS w
                WHERE w.name = 'EU Hub'
                UNION ALL
                SELECT child.warehouse_id, child.name, parent.depth + 1
                FROM warehouses AS child
                JOIN subtree AS parent ON child.parent_id = parent.warehouse_id
            )
            SELECT s.warehouse_id,
                   s.name,
                   s.depth,
                   COUNT(sh.shipment_id) AS shipments
            FROM subtree AS s
            LEFT JOIN shipments AS sh ON sh.origin_warehouse_id = s.warehouse_id
            GROUP BY s.warehouse_id, s.name, s.depth
            ORDER BY s.depth, s.warehouse_id
        """,
        "comparison": "ordered",
        "tie_policy": "Depth ascending, then warehouse_id ascending; both keys together are unique in the result.",
    },
]

DROPPED = []

NOTES = [
    "Shipment 2 has two exceptions and two legs, so a plain join doubles the distance.",
    "Carrier rate changes on 2024-02-15 and 2024-03-01 make the as-of cost differ from the current rate.",
    "The 22:30 and 23:30 UTC departures land on different Sydney days than in UTC.",
    "Alpine and Pacific both move 4400.00 kg, so the top-2 tie-break (carrier name) is exercised at the cutoff.",
]
