"""airline: region tree, route fare history, bookings, disruptions."""

SLUG = "airline"
DOMAIN = "airline network: region tree, route fare history, flights, bookings and disruptions"

DDL = [
    """
    CREATE TABLE regions (
        region_id integer PRIMARY KEY,
        name text NOT NULL,
        parent_id integer REFERENCES regions(region_id)
    )
    """,
    """
    CREATE TABLE airports (
        airport_id integer PRIMARY KEY,
        code text NOT NULL,
        name text NOT NULL,
        region_id integer NOT NULL REFERENCES regions(region_id)
    )
    """,
    """
    CREATE TABLE fare_history (
        route text NOT NULL,
        valid_from timestamptz NOT NULL,
        base_fare numeric(10,2) NOT NULL,
        PRIMARY KEY (route, valid_from)
    )
    """,
    """
    CREATE TABLE flights (
        flight_id integer PRIMARY KEY,
        route text NOT NULL,
        origin_airport_id integer NOT NULL REFERENCES airports(airport_id),
        dest_airport_id integer NOT NULL REFERENCES airports(airport_id),
        departed_at timestamptz NOT NULL,
        status text NOT NULL,
        attributes jsonb NOT NULL,
        tags text[] NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE bookings (
        booking_id integer PRIMARY KEY,
        flight_id integer NOT NULL REFERENCES flights(flight_id),
        passenger_id integer NOT NULL,
        fare numeric(10,2) NOT NULL,
        cabin text NOT NULL
    )
    """,
    """
    CREATE TABLE disruptions (
        disruption_id integer PRIMARY KEY,
        flight_id integer NOT NULL REFERENCES flights(flight_id),
        kind text NOT NULL,
        cost numeric(10,2) NOT NULL
    )
    """,
]

FIXTURES = {
    "regions": [
        {"region_id": 1, "name": "EMEA", "parent_id": None},
        {"region_id": 2, "name": "Europe", "parent_id": 1},
        {"region_id": 3, "name": "Middle East", "parent_id": 1},
        {"region_id": 4, "name": "APAC", "parent_id": None},
        {"region_id": 5, "name": "Oceania", "parent_id": 4},
    ],
    "airports": [
        {"airport_id": 1, "code": "FRA", "name": "Frankfurt", "region_id": 2},
        {"airport_id": 2, "code": "BER", "name": "Berlin", "region_id": 2},
        {"airport_id": 3, "code": "DXB", "name": "Dubai", "region_id": 3},
        {"airport_id": 4, "code": "SYD", "name": "Sydney", "region_id": 5},
        {"airport_id": 5, "code": "AKL", "name": "Auckland", "region_id": 5},
    ],
    "fare_history": [
        {"route": "FRA-DXB", "valid_from": "2024-01-01 00:00:00+00", "base_fare": "420.00"},
        {"route": "FRA-DXB", "valid_from": "2024-03-01 00:00:00+00", "base_fare": "465.00"},
        {"route": "BER-DXB", "valid_from": "2024-01-01 00:00:00+00", "base_fare": "410.00"},
        {"route": "DXB-SYD", "valid_from": "2024-02-01 00:00:00+00", "base_fare": "980.00"},
        {"route": "SYD-AKL", "valid_from": "2024-01-01 00:00:00+00", "base_fare": "260.00"},
    ],
    "flights": [
        {"flight_id": 1, "route": "FRA-DXB", "origin_airport_id": 1, "dest_airport_id": 3, "departed_at": "2024-03-04 22:30:00+00",
         "status": "arrived", "attributes": {"aircraft": "A350", "seats": 310}, "tags": ["longhaul"]},
        {"flight_id": 2, "route": "FRA-DXB", "origin_airport_id": 1, "dest_airport_id": 3, "departed_at": "2024-03-05 06:00:00+00",
         "status": "arrived", "attributes": {"aircraft": "A320", "seats": 180}, "tags": ["shorthaul"]},
        {"flight_id": 3, "route": "BER-DXB", "origin_airport_id": 2, "dest_airport_id": 3, "departed_at": "2024-03-06 08:00:00+00",
         "status": "arrived", "attributes": {"aircraft": "A321", "seats": 200}, "tags": ["shorthaul"]},
        {"flight_id": 4, "route": "DXB-SYD", "origin_airport_id": 3, "dest_airport_id": 4, "departed_at": "2024-03-07 03:00:00+00",
         "status": "arrived", "attributes": {"aircraft": "A350", "seats": 310}, "tags": ["longhaul", "overnight"]},
        {"flight_id": 5, "route": "SYD-AKL", "origin_airport_id": 4, "dest_airport_id": 5, "departed_at": "2024-03-08 21:00:00+00",
         "status": "cancelled", "attributes": {"aircraft": "A320", "seats": 180}, "tags": ["shorthaul"]},
        {"flight_id": 6, "route": "FRA-DXB", "origin_airport_id": 1, "dest_airport_id": 3, "departed_at": "2024-03-09 22:30:00+00",
         "status": "arrived", "attributes": {"aircraft": "A350", "seats": 310}, "tags": ["longhaul"]},
        {"flight_id": 7, "route": "BER-DXB", "origin_airport_id": 2, "dest_airport_id": 3, "departed_at": "2024-03-10 23:30:00+00",
         "status": "arrived", "attributes": {"aircraft": "A321", "seats": 200}, "tags": ["shorthaul", "overnight"]},
        {"flight_id": 8, "route": "DXB-SYD", "origin_airport_id": 3, "dest_airport_id": 4, "departed_at": "2024-03-11 03:00:00+00",
         "status": "arrived", "attributes": {"aircraft": "B777", "seats": 350}, "tags": ["longhaul"]},
    ],
    "bookings": [
        {"booking_id": 1, "flight_id": 1, "passenger_id": 900, "fare": "520.00", "cabin": "economy"},
        {"booking_id": 2, "flight_id": 1, "passenger_id": 901, "fare": "1450.00", "cabin": "business"},
        {"booking_id": 3, "flight_id": 2, "passenger_id": 902, "fare": "430.00", "cabin": "economy"},
        {"booking_id": 4, "flight_id": 3, "passenger_id": 903, "fare": "455.00", "cabin": "economy"},
        {"booking_id": 5, "flight_id": 3, "passenger_id": 904, "fare": "1190.00", "cabin": "business"},
        {"booking_id": 6, "flight_id": 4, "passenger_id": 905, "fare": "1080.00", "cabin": "economy"},
        {"booking_id": 7, "flight_id": 4, "passenger_id": 906, "fare": "2980.00", "cabin": "business"},
        {"booking_id": 8, "flight_id": 6, "passenger_id": 907, "fare": "540.00", "cabin": "economy"},
        {"booking_id": 9, "flight_id": 7, "passenger_id": 908, "fare": "1295.00", "cabin": "economy"},
        {"booking_id": 10, "flight_id": 8, "passenger_id": 909, "fare": "1020.00", "cabin": "economy"},
        {"booking_id": 11, "flight_id": 8, "passenger_id": 910, "fare": "1250.00", "cabin": "economy"},
        {"booking_id": 12, "flight_id": 5, "passenger_id": 911, "fare": "270.00", "cabin": "economy"},
    ],
    "disruptions": [
        {"disruption_id": 1, "flight_id": 2, "kind": "delay", "cost": "300.00"},
        {"disruption_id": 2, "flight_id": 4, "kind": "weather", "cost": "1200.00"},
        {"disruption_id": 3, "flight_id": 5, "kind": "cancellation", "cost": "800.00"},
        {"disruption_id": 4, "flight_id": 6, "kind": "delay", "cost": "250.00"},
    ],
}

QUESTIONS = [
    {
        "skill": "grain_fanout_antijoin",
        "difficulty": "challenging",
        "question": (
            "For every flight that has at least one disruption, return the flight id and the total booking fare on it, "
            "counting each booking exactly once even when the flight has several disruptions."
        ),
        "sql": """
            SELECT f.flight_id,
                   SUM(b.fare) AS booking_fare
            FROM flights AS f
            JOIN bookings AS b ON b.flight_id = f.flight_id
            WHERE EXISTS (SELECT 1 FROM disruptions AS d WHERE d.flight_id = f.flight_id)
            GROUP BY f.flight_id
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; one row per flight in scope.",
    },
    {
        "skill": "topn_ties",
        "difficulty": "challenging",
        "question": (
            "Which two routes earned the most booking fare across all flights? If routes tie, the route that sorts "
            "first alphabetically comes first. Return route and total fare, highest first."
        ),
        "sql": """
            SELECT f.route, SUM(b.fare) AS total_fare
            FROM flights AS f
            JOIN bookings AS b ON b.flight_id = f.flight_id
            GROUP BY f.route
            ORDER BY total_fare DESC, f.route
            LIMIT 2
        """,
        "comparison": "ordered",
        "tie_policy": "Total fare descending; ties broken by route ascending (ORDER BY total_fare DESC, route).",
    },
    {
        "skill": "asof_state",
        "difficulty": "challenging",
        "question": (
            "For every flight, which base fare was in force on its route at the moment of departure? Return "
            "flight_id, route and base_fare."
        ),
        "sql": """
            SELECT f.flight_id,
                   f.route,
                   fh.base_fare
            FROM flights AS f
            JOIN LATERAL (
                SELECT fare.base_fare
                FROM fare_history AS fare
                WHERE fare.route = f.route
                  AND fare.valid_from <= f.departed_at
                ORDER BY fare.valid_from DESC
                LIMIT 1
            ) AS fh ON true
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the fare in force is unique because (route, valid_from) is unique.",
    },
    {
        "skill": "periods_timezones",
        "difficulty": "moderate",
        "question": (
            "Count arrived flights per calendar day in Dubai local time (Asia/Dubai, UTC+4) for 4 March 2024 through "
            "10 March 2024 inclusive. Return the local day and the count, only for days with at least one flight."
        ),
        "sql": """
            SELECT (f.departed_at AT TIME ZONE 'Asia/Dubai')::date AS local_day,
                   COUNT(*) AS flights
            FROM flights AS f
            WHERE f.status = 'arrived'
              AND (f.departed_at AT TIME ZONE 'Asia/Dubai')::date BETWEEN DATE '2024-03-04' AND DATE '2024-03-10'
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
            "For every flight, return flight_id and how many flights departed on the same route over the window of "
            "that flight's departure and the seven days before it, both ends inclusive."
        ),
        "sql": """
            SELECT f.flight_id,
                   COUNT(*) OVER (
                       PARTITION BY f.route
                       ORDER BY f.departed_at
                       RANGE BETWEEN INTERVAL '7 days' PRECEDING AND CURRENT ROW
                   ) AS flights_7d
            FROM flights AS f
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the frame is RANGE INTERVAL '7 days' PRECEDING to CURRENT ROW.",
    },
    {
        "skill": "jsonb_arrays",
        "difficulty": "moderate",
        "question": (
            "Which flights use an A350 aircraft and carry the longhaul tag? Return flight_id, the seat count from the "
            "attributes and how many tags the flight has."
        ),
        "sql": """
            SELECT f.flight_id,
                   (f.attributes ->> 'seats')::integer AS seats,
                   cardinality(f.tags) AS tag_count
            FROM flights AS f
            WHERE f.attributes ->> 'aircraft' = 'A350'
              AND f.tags @> ARRAY['longhaul']
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison, row order is not part of the answer.",
    },
    {
        "skill": "compositional",
        "difficulty": "challenging",
        "question": (
            "Among flights that have at least one booking, what share has at least one disruption? Return one row with "
            "the share between 0 and 1, rounded to four decimals."
        ),
        "sql": """
            SELECT ROUND(
                       COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM disruptions AS d WHERE d.flight_id = f.flight_id))::numeric
                       / NULLIF(COUNT(*), 0),
                       4
                   ) AS disrupted_share
            FROM flights AS f
            WHERE EXISTS (SELECT 1 FROM bookings AS b WHERE b.flight_id = f.flight_id)
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: the result is a single row.",
    },
    {
        "skill": "hierarchies",
        "difficulty": "challenging",
        "question": (
            "Starting at the region 'EMEA', list that region and every region below it with the depth relative to "
            "EMEA (EMEA = 0) and the number of airports assigned directly to each region. Order by depth, then "
            "region_id."
        ),
        "sql": """
            WITH RECURSIVE subtree AS (
                SELECT r.region_id, r.name, 0 AS depth
                FROM regions AS r
                WHERE r.name = 'EMEA'
                UNION ALL
                SELECT child.region_id, child.name, parent.depth + 1
                FROM regions AS child
                JOIN subtree AS parent ON child.parent_id = parent.region_id
            )
            SELECT s.region_id,
                   s.name,
                   s.depth,
                   COUNT(a.airport_id) AS airports
            FROM subtree AS s
            LEFT JOIN airports AS a ON a.region_id = s.region_id
            GROUP BY s.region_id, s.name, s.depth
            ORDER BY s.depth, s.region_id
        """,
        "comparison": "ordered",
        "tie_policy": "Depth ascending, then region_id ascending; both keys together are unique in the result.",
    },
]

DROPPED = []

NOTES = [
    "Flight 4 has one disruption and two bookings; the fan-out question keeps the fare summed once.",
    "The FRA-DXB base fare changes on 2024-03-01, separating an as-of answer from the current fare.",
    "The 22:30 and 23:30 UTC departures fall on different Dubai days than in UTC.",
    "BER-DXB and FRA-DXB both earn 2940.00, so the top-2 tie-break (route name) is exercised at the cutoff.",
]
