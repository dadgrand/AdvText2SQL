"""energy: grid tree with subtree aggregates, tariff history, meter readings, adjustments."""

SLUG = "energy"
DOMAIN = "energy grid: site tree, tariff history per site, meter readings with adjustments"

DDL = [
    """
    CREATE TABLE sites (
        site_id integer PRIMARY KEY,
        name text NOT NULL,
        parent_id integer REFERENCES sites(site_id),
        kind text NOT NULL
    )
    """,
    """
    CREATE TABLE tariff_history (
        site_id integer NOT NULL REFERENCES sites(site_id),
        valid_from timestamptz NOT NULL,
        tariff text NOT NULL,
        price_per_kwh numeric(8,4) NOT NULL,
        PRIMARY KEY (site_id, valid_from)
    )
    """,
    """
    CREATE TABLE readings (
        reading_id integer PRIMARY KEY,
        site_id integer NOT NULL REFERENCES sites(site_id),
        read_at timestamptz NOT NULL,
        kwh numeric(10,2) NOT NULL,
        device jsonb NOT NULL,
        tags text[] NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE adjustments (
        adjustment_id integer PRIMARY KEY,
        reading_id integer NOT NULL REFERENCES readings(reading_id),
        delta_kwh numeric(10,2) NOT NULL
    )
    """,
]

FIXTURES = {
    "sites": [
        {"site_id": 1, "name": "North Grid", "parent_id": None, "kind": "grid"},
        {"site_id": 2, "name": "Substation A", "parent_id": 1, "kind": "substation"},
        {"site_id": 3, "name": "Substation B", "parent_id": 1, "kind": "substation"},
        {"site_id": 4, "name": "Meter 1", "parent_id": 2, "kind": "meter"},
        {"site_id": 5, "name": "Meter 2", "parent_id": 2, "kind": "meter"},
        {"site_id": 6, "name": "Meter 3", "parent_id": 3, "kind": "meter"},
        {"site_id": 7, "name": "South Grid", "parent_id": None, "kind": "grid"},
        {"site_id": 8, "name": "Meter 4", "parent_id": 7, "kind": "meter"},
    ],
    "tariff_history": [
        {"site_id": 4, "valid_from": "2024-01-01 00:00:00+00", "tariff": "standard", "price_per_kwh": "0.2100"},
        {"site_id": 4, "valid_from": "2024-03-01 00:00:00+00", "tariff": "standard", "price_per_kwh": "0.2400"},
        {"site_id": 5, "valid_from": "2024-01-01 00:00:00+00", "tariff": "standard", "price_per_kwh": "0.2200"},
        {"site_id": 6, "valid_from": "2024-02-15 00:00:00+00", "tariff": "industrial", "price_per_kwh": "0.1800"},
        {"site_id": 8, "valid_from": "2024-01-01 00:00:00+00", "tariff": "standard", "price_per_kwh": "0.2500"},
    ],
    "readings": [
        {"reading_id": 1, "site_id": 4, "read_at": "2024-02-20 09:00:00+00", "kwh": "120.00",
         "device": {"type": "smart", "firmware": "3.1"}, "tags": ["solar"]},
        {"reading_id": 2, "site_id": 4, "read_at": "2024-03-04 22:30:00+00", "kwh": "95.00",
         "device": {"type": "smart", "firmware": "3.2"}, "tags": ["solar"]},
        {"reading_id": 3, "site_id": 4, "read_at": "2024-03-05 10:00:00+00", "kwh": "150.00",
         "device": {"type": "smart", "firmware": "3.2"}, "tags": []},
        {"reading_id": 4, "site_id": 5, "read_at": "2024-03-06 08:00:00+00", "kwh": "210.00",
         "device": {"type": "legacy", "firmware": "1.0"}, "tags": []},
        {"reading_id": 5, "site_id": 5, "read_at": "2024-03-07 08:00:00+00", "kwh": "80.00",
         "device": {"type": "smart", "firmware": "3.2"}, "tags": ["solar"]},
        {"reading_id": 6, "site_id": 6, "read_at": "2024-03-08 06:00:00+00", "kwh": "430.00",
         "device": {"type": "smart", "firmware": "2.9"}, "tags": ["solar", "industrial"]},
        {"reading_id": 7, "site_id": 6, "read_at": "2024-03-09 06:00:00+00", "kwh": "390.00",
         "device": {"type": "smart", "firmware": "2.9"}, "tags": []},
        {"reading_id": 8, "site_id": 8, "read_at": "2024-03-10 06:00:00+00", "kwh": "205.00",
         "device": {"type": "legacy", "firmware": "1.0"}, "tags": ["solar"]},
        {"reading_id": 9, "site_id": 8, "read_at": "2024-03-11 06:00:00+00", "kwh": "160.00",
         "device": {"type": "smart", "firmware": "3.0"}, "tags": []},
        {"reading_id": 10, "site_id": 2, "read_at": "2024-03-10 18:00:00+00", "kwh": "60.00",
         "device": {"type": "substation", "firmware": "5.4"}, "tags": ["grid"]},
        {"reading_id": 11, "site_id": 1, "read_at": "2024-03-12 18:00:00+00", "kwh": "40.00",
         "device": {"type": "substation", "firmware": "5.4"}, "tags": ["grid"]},
    ],
    "adjustments": [
        {"adjustment_id": 1, "reading_id": 3, "delta_kwh": "-12.00"},
        {"adjustment_id": 2, "reading_id": 3, "delta_kwh": "5.50"},
        {"adjustment_id": 3, "reading_id": 6, "delta_kwh": "-30.00"},
        {"adjustment_id": 4, "reading_id": 9, "delta_kwh": "8.00"},
    ],
}

QUESTIONS = [
    {
        "skill": "grain_fanout_antijoin",
        "difficulty": "challenging",
        "question": (
            "For every reading that has at least one adjustment, return the reading id and the adjusted energy, "
            "which is the reading's kWh plus every adjustment on it, counting the reading exactly once even when it "
            "has several adjustments."
        ),
        "sql": """
            SELECT r.reading_id,
                   r.kwh + SUM(a.delta_kwh) AS adjusted_kwh
            FROM readings AS r
            JOIN adjustments AS a ON a.reading_id = r.reading_id
            GROUP BY r.reading_id, r.kwh
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; one row per reading in scope.",
    },
    {
        "skill": "topn_ties",
        "difficulty": "challenging",
        "question": (
            "Which two sites consumed the most energy across all meter readings? If sites tie, the smaller site_id "
            "comes first. Return site_id and total kWh, highest first."
        ),
        "sql": """
            SELECT r.site_id, SUM(r.kwh) AS total_kwh
            FROM readings AS r
            GROUP BY r.site_id
            ORDER BY total_kwh DESC, r.site_id
            LIMIT 2
        """,
        "comparison": "ordered",
        "tie_policy": "Total kWh descending; ties broken by the smaller site_id (ORDER BY total_kwh DESC, site_id).",
    },
    {
        "skill": "asof_state",
        "difficulty": "challenging",
        "question": (
            "For every reading on a site that has a tariff history, what is its cost at the price per kWh in force at "
            "the reading time (kWh times price, rounded to two decimals)? Return reading_id and cost."
        ),
        "sql": """
            SELECT r.reading_id,
                   ROUND(r.kwh * t.price_per_kwh, 2) AS cost
            FROM readings AS r
            JOIN LATERAL (
                SELECT th.price_per_kwh
                FROM tariff_history AS th
                WHERE th.site_id = r.site_id
                  AND th.valid_from <= r.read_at
                ORDER BY th.valid_from DESC
                LIMIT 1
            ) AS t ON true
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the price in force is unique because (site_id, valid_from) is unique.",
    },
    {
        "skill": "periods_timezones",
        "difficulty": "moderate",
        "question": (
            "Count readings per calendar day in Auckland local time (Pacific/Auckland, UTC+13 in early March 2024) "
            "for 4 March 2024 through 10 March 2024 inclusive. Return the local day and the count, only for days "
            "with at least one reading."
        ),
        "sql": """
            SELECT (r.read_at AT TIME ZONE 'Pacific/Auckland')::date AS local_day,
                   COUNT(*) AS readings
            FROM readings AS r
            WHERE (r.read_at AT TIME ZONE 'Pacific/Auckland')::date BETWEEN DATE '2024-03-04' AND DATE '2024-03-10'
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
            "For every reading, return reading_id and the total kWh of the same site over the window of that "
            "reading's timestamp and the seven days before it, both ends inclusive."
        ),
        "sql": """
            SELECT r.reading_id,
                   SUM(r.kwh) OVER (
                       PARTITION BY r.site_id
                       ORDER BY r.read_at
                       RANGE BETWEEN INTERVAL '7 days' PRECEDING AND CURRENT ROW
                   ) AS kwh_7d
            FROM readings AS r
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the frame is RANGE INTERVAL '7 days' PRECEDING to CURRENT ROW.",
    },
    {
        "skill": "jsonb_arrays",
        "difficulty": "moderate",
        "question": (
            "Which readings come from a smart meter and carry the solar tag? Return reading_id, the firmware version "
            "and how many tags the reading has."
        ),
        "sql": """
            SELECT r.reading_id,
                   r.device ->> 'firmware' AS firmware,
                   cardinality(r.tags) AS tag_count
            FROM readings AS r
            WHERE r.device ->> 'type' = 'smart'
              AND r.tags @> ARRAY['solar']
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison, row order is not part of the answer.",
    },
    {
        "skill": "compositional",
        "difficulty": "challenging",
        "question": (
            "Among readings above 100 kWh, what share has at least one adjustment? Return one row with the share "
            "between 0 and 1, rounded to four decimals."
        ),
        "sql": """
            SELECT ROUND(
                       COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM adjustments AS a WHERE a.reading_id = r.reading_id))::numeric
                       / NULLIF(COUNT(*), 0),
                       4
                   ) AS adjusted_share
            FROM readings AS r
            WHERE r.kwh > 100
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: the result is a single row.",
    },
    {
        "skill": "hierarchies",
        "difficulty": "challenging",
        "question": (
            "For the site 'North Grid' and every site below it, return the site id, the depth relative to North Grid "
            "(North Grid = 0) and the total kWh of all readings at that site and at every site below it. Order by "
            "depth, then site_id."
        ),
        "sql": """
            WITH RECURSIVE nodes AS (
                SELECT s.site_id, 0 AS depth
                FROM sites AS s
                WHERE s.name = 'North Grid'
                UNION ALL
                SELECT child.site_id, parent.depth + 1
                FROM sites AS child
                JOIN nodes AS parent ON child.parent_id = parent.site_id
            ), ancestors AS (
                SELECT n.site_id AS node_id, n.site_id AS ancestor_id
                FROM nodes AS n
                UNION ALL
                SELECT up.node_id, parent.site_id
                FROM ancestors AS up
                JOIN sites AS parent ON parent.site_id = (
                    SELECT s.parent_id FROM sites AS s WHERE s.site_id = up.ancestor_id
                )
            )
            SELECT n.site_id,
                   n.depth,
                   COALESCE(SUM(r.kwh), 0) AS subtree_kwh
            FROM nodes AS n
            LEFT JOIN ancestors AS a ON a.ancestor_id = n.site_id
            LEFT JOIN readings AS r ON r.site_id = a.node_id
            GROUP BY n.site_id, n.depth
            ORDER BY n.depth, n.site_id
        """,
        "comparison": "ordered",
        "tie_policy": "Depth ascending, then site_id ascending; both keys together are unique in the result.",
    },
]

DROPPED = []

NOTES = [
    "Reading 3 has two adjustments, so the adjusted energy needs a grouped sum rather than a join per adjustment.",
    "The tariff changes on 2024-03-01 for site 4, so early and late readings price differently.",
    "The hierarchy question aggregates over the whole subtree, not only the site's own readings.",
    "Sites 4 and 8 both consume 365.00 kWh, so the top-2 tie-break (smaller site_id) is exercised at the cutoff.",
]
