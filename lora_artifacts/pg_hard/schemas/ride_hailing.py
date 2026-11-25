"""ride_hailing: as-of driver rates, split payments against refunds, zone hierarchy."""

SLUG = "ride_hailing"
DOMAIN = "ride-hailing marketplace: trips, split payments, refunds, as-of driver rates, city zone tree"

DDL = [
    """
    CREATE TABLE zones (
        zone_id integer PRIMARY KEY,
        name text NOT NULL,
        parent_id integer REFERENCES zones(zone_id),
        city text NOT NULL
    )
    """,
    """
    CREATE TABLE drivers (
        driver_id integer PRIMARY KEY,
        name text NOT NULL,
        status text NOT NULL,
        zone_id integer NOT NULL REFERENCES zones(zone_id)
    )
    """,
    """
    CREATE TABLE driver_rates (
        driver_id integer NOT NULL REFERENCES drivers(driver_id),
        valid_from timestamptz NOT NULL,
        rate_per_km numeric(6,2) NOT NULL,
        PRIMARY KEY (driver_id, valid_from)
    )
    """,
    """
    CREATE TABLE trips (
        trip_id integer PRIMARY KEY,
        driver_id integer NOT NULL REFERENCES drivers(driver_id),
        started_at timestamptz NOT NULL,
        distance_km numeric(6,2) NOT NULL,
        status text NOT NULL,
        attrs jsonb NOT NULL,
        tags text[] NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE payments (
        payment_id integer PRIMARY KEY,
        trip_id integer NOT NULL REFERENCES trips(trip_id),
        amount numeric(10,2) NOT NULL,
        kind text NOT NULL
    )
    """,
    """
    CREATE TABLE refunds (
        refund_id integer PRIMARY KEY,
        trip_id integer NOT NULL REFERENCES trips(trip_id),
        amount numeric(10,2) NOT NULL
    )
    """,
]

FIXTURES = {
    "zones": [
        {"zone_id": 1, "name": "Berlin", "parent_id": None, "city": "Berlin"},
        {"zone_id": 2, "name": "Mitte", "parent_id": 1, "city": "Berlin"},
        {"zone_id": 3, "name": "Friedrichshain", "parent_id": 1, "city": "Berlin"},
        {"zone_id": 4, "name": "Mitte Nord", "parent_id": 2, "city": "Berlin"},
        {"zone_id": 5, "name": "Mitte Sued", "parent_id": 2, "city": "Berlin"},
        {"zone_id": 6, "name": "Boxhagen", "parent_id": 3, "city": "Berlin"},
        {"zone_id": 7, "name": "Munich", "parent_id": None, "city": "Munich"},
        {"zone_id": 8, "name": "Maxvorstadt", "parent_id": 7, "city": "Munich"},
        {"zone_id": 9, "name": "Maxvorstadt West", "parent_id": 8, "city": "Munich"},
    ],
    "drivers": [
        {"driver_id": 1, "name": "Anna", "status": "active", "zone_id": 4},
        {"driver_id": 2, "name": "Ben", "status": "active", "zone_id": 5},
        {"driver_id": 3, "name": "Carla", "status": "active", "zone_id": 6},
        {"driver_id": 4, "name": "Deniz", "status": "inactive", "zone_id": 9},
        {"driver_id": 5, "name": "Emil", "status": "active", "zone_id": 4},
    ],
    "driver_rates": [
        {"driver_id": 1, "valid_from": "2024-01-01 00:00:00+00", "rate_per_km": "1.20"},
        {"driver_id": 1, "valid_from": "2024-03-01 00:00:00+00", "rate_per_km": "1.50"},
        {"driver_id": 2, "valid_from": "2024-01-01 00:00:00+00", "rate_per_km": "1.10"},
        {"driver_id": 2, "valid_from": "2024-03-05 12:00:00+00", "rate_per_km": "1.40"},
        {"driver_id": 3, "valid_from": "2024-01-01 00:00:00+00", "rate_per_km": "0.90"},
        {"driver_id": 4, "valid_from": "2024-01-01 00:00:00+00", "rate_per_km": "2.00"},
        {"driver_id": 5, "valid_from": "2024-02-15 00:00:00+00", "rate_per_km": "1.30"},
        {"driver_id": 5, "valid_from": "2024-03-10 00:00:00+00", "rate_per_km": "1.60"},
    ],
    "trips": [
        {"trip_id": 1, "driver_id": 1, "started_at": "2024-02-20 08:00:00+00", "distance_km": "8.00", "status": "completed",
         "attrs": {"channel": "consumer", "surge": {"multiplier": 1.0}}, "tags": ["morning"]},
        {"trip_id": 2, "driver_id": 1, "started_at": "2024-03-04 22:30:00+00", "distance_km": "5.20", "status": "completed",
         "attrs": {"channel": "corporate", "surge": {"multiplier": 1.4}}, "tags": ["airport", "night"]},
        {"trip_id": 3, "driver_id": 1, "started_at": "2024-03-04 23:30:00+00", "distance_km": "3.10", "status": "completed",
         "attrs": {"channel": "corporate", "surge": {"multiplier": 1.2}}, "tags": ["airport"]},
        {"trip_id": 4, "driver_id": 2, "started_at": "2024-03-05 10:00:00+00", "distance_km": "12.40", "status": "completed",
         "attrs": {"channel": "consumer", "surge": {"multiplier": 1.0}}, "tags": []},
        {"trip_id": 5, "driver_id": 2, "started_at": "2024-03-06 21:15:00+00", "distance_km": "4.50", "status": "completed",
         "attrs": {"channel": "corporate", "surge": {"multiplier": 1.5}}, "tags": ["night", "corporate"]},
        {"trip_id": 6, "driver_id": 3, "started_at": "2024-03-05 18:00:00+00", "distance_km": "6.80", "status": "completed",
         "attrs": {"channel": "consumer", "surge": {"multiplier": 1.0}}, "tags": ["evening"]},
        {"trip_id": 7, "driver_id": 3, "started_at": "2024-03-07 03:00:00+00", "distance_km": "2.20", "status": "completed",
         "attrs": {"channel": "consumer", "surge": {"multiplier": 1.1}}, "tags": ["night"]},
        {"trip_id": 8, "driver_id": 3, "started_at": "2024-03-07 23:59:00+00", "distance_km": "7.60", "status": "cancelled",
         "attrs": {"channel": "consumer", "surge": {"multiplier": 1.0}}, "tags": []},
        {"trip_id": 9, "driver_id": 1, "started_at": "2024-03-09 12:00:00+00", "distance_km": "9.30", "status": "completed",
         "attrs": {"channel": "corporate", "surge": {"multiplier": 1.3}}, "tags": ["airport", "corporate"]},
        {"trip_id": 10, "driver_id": 5, "started_at": "2024-03-10 01:30:00+00", "distance_km": "3.80", "status": "completed",
         "attrs": {"channel": "consumer", "surge": {"multiplier": 1.0}}, "tags": []},
        {"trip_id": 11, "driver_id": 5, "started_at": "2024-03-10 22:00:00+00", "distance_km": "11.00", "status": "completed",
         "attrs": {"channel": "consumer", "surge": {"multiplier": 1.2}}, "tags": ["night"]},
        {"trip_id": 12, "driver_id": 2, "started_at": "2024-03-11 09:00:00+00", "distance_km": "5.00", "status": "completed",
         "attrs": {"channel": "consumer", "surge": {"multiplier": 1.0}}, "tags": []},
        {"trip_id": 13, "driver_id": 4, "started_at": "2024-01-15 07:00:00+00", "distance_km": "6.00", "status": "cancelled",
         "attrs": {"channel": "consumer", "surge": {"multiplier": 1.0}}, "tags": []},
        {"trip_id": 14, "driver_id": 1, "started_at": "2024-03-05 20:00:00+00", "distance_km": "2.90", "status": "completed",
         "attrs": {"channel": "corporate", "surge": {"multiplier": 1.2}}, "tags": ["airport"]},
    ],
    "payments": [
        {"payment_id": 1, "trip_id": 2, "amount": "31.00", "kind": "card"},
        {"payment_id": 2, "trip_id": 2, "amount": "4.00", "kind": "tip"},
        {"payment_id": 3, "trip_id": 3, "amount": "18.60", "kind": "card"},
        {"payment_id": 4, "trip_id": 4, "amount": "40.00", "kind": "card"},
        {"payment_id": 5, "trip_id": 5, "amount": "21.50", "kind": "card"},
        {"payment_id": 6, "trip_id": 5, "amount": "3.50", "kind": "tip"},
        {"payment_id": 7, "trip_id": 6, "amount": "15.00", "kind": "card"},
        {"payment_id": 8, "trip_id": 7, "amount": "9.00", "kind": "cash"},
        {"payment_id": 9, "trip_id": 9, "amount": "35.00", "kind": "card"},
        {"payment_id": 10, "trip_id": 10, "amount": "12.00", "kind": "card"},
        {"payment_id": 11, "trip_id": 11, "amount": "69.00", "kind": "card"},
        {"payment_id": 12, "trip_id": 12, "amount": "16.00", "kind": "card"},
        {"payment_id": 13, "trip_id": 14, "amount": "11.00", "kind": "card"},
        {"payment_id": 14, "trip_id": 1, "amount": "26.00", "kind": "card"},
        {"payment_id": 15, "trip_id": 13, "amount": "0.00", "kind": "voucher"},
    ],
    "refunds": [
        {"refund_id": 1, "trip_id": 2, "amount": "5.00"},
        {"refund_id": 2, "trip_id": 2, "amount": "2.00"},
        {"refund_id": 3, "trip_id": 6, "amount": "15.00"},
        {"refund_id": 4, "trip_id": 8, "amount": "9.50"},
        {"refund_id": 5, "trip_id": 13, "amount": "0.00"},
    ],
}

QUESTIONS = [
    {
        "skill": "grain_fanout_antijoin",
        "difficulty": "moderate",
        "question": (
            "For every trip that has at least one payment and at least one refund, report the trip id and the "
            "total amount paid on that trip, counting each payment exactly once even when the trip has several "
            "payments and several refunds."
        ),
        "sql": """
            SELECT t.trip_id, SUM(p.amount) AS paid_amount
            FROM trips AS t
            JOIN payments AS p ON p.trip_id = t.trip_id
            WHERE EXISTS (SELECT 1 FROM refunds AS r WHERE r.trip_id = t.trip_id)
            GROUP BY t.trip_id
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison, row order is not part of the answer; one row per trip.",
    },
    {
        "skill": "topn_ties",
        "difficulty": "challenging",
        "question": (
            "Give the two drivers with the highest net amount on completed trips, where net amount is all payments on "
            "their completed trips minus all refunds on those trips. If two drivers have the same net amount, the "
            "smaller driver_id comes first. Return driver_id and net_amount, highest first."
        ),
        "sql": """
            WITH paid AS (
                SELECT t.driver_id, SUM(p.amount) AS amount
                FROM trips AS t
                JOIN payments AS p ON p.trip_id = t.trip_id
                WHERE t.status = 'completed'
                GROUP BY t.driver_id
            ), refunded AS (
                SELECT t.driver_id, SUM(r.amount) AS amount
                FROM trips AS t
                JOIN refunds AS r ON r.trip_id = t.trip_id
                WHERE t.status = 'completed'
                GROUP BY t.driver_id
            )
            SELECT d.driver_id,
                   COALESCE(paid.amount, 0) - COALESCE(refunded.amount, 0) AS net_amount
            FROM drivers AS d
            LEFT JOIN paid ON paid.driver_id = d.driver_id
            LEFT JOIN refunded ON refunded.driver_id = d.driver_id
            ORDER BY net_amount DESC, d.driver_id
            LIMIT 2
        """,
        "comparison": "ordered",
        "tie_policy": "Net amount descending; ties broken by the smaller driver_id (ORDER BY net_amount DESC, driver_id).",
    },
    {
        "skill": "asof_state",
        "difficulty": "challenging",
        "question": (
            "For every completed trip, compute the fare using the driver's rate per km that was in force at the "
            "trip's start (distance_km times that rate), rounded to two decimals. Return trip_id and fare."
        ),
        "sql": """
            SELECT t.trip_id, ROUND(t.distance_km * r.rate_per_km, 2) AS fare
            FROM trips AS t
            JOIN LATERAL (
                SELECT dr.rate_per_km
                FROM driver_rates AS dr
                WHERE dr.driver_id = t.driver_id
                  AND dr.valid_from <= t.started_at
                ORDER BY dr.valid_from DESC
                LIMIT 1
            ) AS r ON true
            WHERE t.status = 'completed'
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the rate in force is unique because (driver_id, valid_from) is unique.",
    },
    {
        "skill": "periods_timezones",
        "difficulty": "challenging",
        "question": (
            "Count completed trips per calendar day in Berlin local time (Europe/Berlin, CET = UTC+1 in early March "
            "2024) for 4 March 2024 through 10 March 2024 inclusive. Return the local day and the number of trips, "
            "only for days that have at least one trip."
        ),
        "sql": """
            SELECT (t.started_at AT TIME ZONE 'Europe/Berlin')::date AS local_day,
                   COUNT(*) AS trips
            FROM trips AS t
            WHERE t.status = 'completed'
              AND (t.started_at AT TIME ZONE 'Europe/Berlin')::date BETWEEN DATE '2024-03-04' AND DATE '2024-03-10'
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
            "For every completed trip, return trip_id and the driver's net amount (payments minus refunds on the "
            "trip) over the rolling window of that trip's timestamp and the seven days before it, both ends "
            "inclusive, using only completed trips."
        ),
        "sql": """
            WITH trip_net AS (
                SELECT t.trip_id,
                       t.driver_id,
                       t.started_at,
                       COALESCE((SELECT SUM(p.amount) FROM payments AS p WHERE p.trip_id = t.trip_id), 0)
                       - COALESCE((SELECT SUM(r.amount) FROM refunds AS r WHERE r.trip_id = t.trip_id), 0) AS net
                FROM trips AS t
                WHERE t.status = 'completed'
            )
            SELECT trip_id,
                   SUM(net) OVER (
                       PARTITION BY driver_id
                       ORDER BY started_at
                       RANGE BETWEEN INTERVAL '7 days' PRECEDING AND CURRENT ROW
                   ) AS net_7d
            FROM trip_net
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the frame is RANGE INTERVAL '7 days' PRECEDING to CURRENT ROW on started_at.",
    },
    {
        "skill": "jsonb_arrays",
        "difficulty": "moderate",
        "question": (
            "For trips whose attributes say channel = 'corporate' and whose tags include 'airport', return trip_id, "
            "the surge multiplier from attributes (as a number) and how many tags the trip carries."
        ),
        "sql": """
            SELECT t.trip_id,
                   (t.attrs -> 'surge' ->> 'multiplier')::numeric AS surge_multiplier,
                   cardinality(t.tags) AS tag_count
            FROM trips AS t
            WHERE t.attrs ->> 'channel' = 'corporate'
              AND t.tags @> ARRAY['airport']
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison, row order is not part of the answer.",
    },
    {
        "skill": "compositional",
        "difficulty": "challenging",
        "question": (
            "Among all trips that have at least one refund, what share were cancelled? Return exactly one row with "
            "the share as a number between 0 and 1, rounded to four decimals."
        ),
        "sql": """
            SELECT ROUND(
                       COUNT(*) FILTER (WHERE t.status = 'cancelled')::numeric / NULLIF(COUNT(*), 0),
                       4
                   ) AS cancelled_share
            FROM trips AS t
            WHERE EXISTS (SELECT 1 FROM refunds AS r WHERE r.trip_id = t.trip_id)
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: the result is a single row.",
    },
    {
        "skill": "hierarchies",
        "difficulty": "challenging",
        "question": (
            "List the zone 'Mitte' together with every zone below it in the zone tree, with each zone's depth "
            "relative to Mitte (Mitte = 0) and the number of active drivers whose zone is exactly that zone. "
            "Order by depth, then zone_id."
        ),
        "sql": """
            WITH RECURSIVE subtree AS (
                SELECT z.zone_id, z.name, 0 AS depth
                FROM zones AS z
                WHERE z.name = 'Mitte'
                UNION ALL
                SELECT child.zone_id, child.name, parent.depth + 1
                FROM zones AS child
                JOIN subtree AS parent ON child.parent_id = parent.zone_id
            )
            SELECT s.zone_id,
                   s.name,
                   s.depth,
                   COUNT(d.driver_id) FILTER (WHERE d.status = 'active') AS active_drivers
            FROM subtree AS s
            LEFT JOIN drivers AS d ON d.zone_id = s.zone_id
            GROUP BY s.zone_id, s.name, s.depth
            ORDER BY s.depth, s.zone_id
        """,
        "comparison": "ordered",
        "tie_policy": "Depth ascending, then zone_id ascending; both keys together are unique in the result.",
    },
]

DROPPED = []

NOTES = [
    "Split payments with refunds make the fan-out question distinguish EXISTS from a plain join.",
    "Rates change on 2024-03-01 / 2024-03-05 / 2024-03-10, so an as-of join and a plain max-rate join differ.",
    "Trips at 22:30 and 23:30 UTC cross the Berlin midnight boundary for the local-day question.",
    "Drivers 2 and 5 both net 81.00, so the top-2 tie-break (smaller driver_id) is exercised, not assumed.",
]
