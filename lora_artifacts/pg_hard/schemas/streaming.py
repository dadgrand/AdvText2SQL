"""streaming: genre tree, plan-price history, plays across a DST boundary."""

SLUG = "streaming"
DOMAIN = "streaming service: genre tree, plan prices over time, subscriptions and play sessions"

DDL = [
    """
    CREATE TABLE genres (
        genre_id integer PRIMARY KEY,
        name text NOT NULL,
        parent_id integer REFERENCES genres(genre_id)
    )
    """,
    """
    CREATE TABLE titles (
        title_id integer PRIMARY KEY,
        name text NOT NULL,
        genre_id integer NOT NULL REFERENCES genres(genre_id),
        release_year integer NOT NULL,
        duration_seconds integer NOT NULL,
        tags text[] NOT NULL DEFAULT '{}',
        attributes jsonb NOT NULL
    )
    """,
    """
    CREATE TABLE plan_prices (
        plan text NOT NULL,
        valid_from timestamptz NOT NULL,
        monthly_price numeric(8,2) NOT NULL,
        PRIMARY KEY (plan, valid_from)
    )
    """,
    """
    CREATE TABLE subscriptions (
        subscription_id integer PRIMARY KEY,
        user_id integer NOT NULL,
        plan text NOT NULL,
        started_at timestamptz NOT NULL,
        cancelled_at timestamptz
    )
    """,
    """
    CREATE TABLE plays (
        play_id integer PRIMARY KEY,
        subscription_id integer NOT NULL REFERENCES subscriptions(subscription_id),
        title_id integer NOT NULL REFERENCES titles(title_id),
        played_at timestamptz NOT NULL,
        seconds_watched integer NOT NULL,
        device jsonb NOT NULL,
        tags text[] NOT NULL DEFAULT '{}'
    )
    """,
]

FIXTURES = {
    "genres": [
        {"genre_id": 1, "name": "Drama", "parent_id": None},
        {"genre_id": 2, "name": "Thriller", "parent_id": None},
        {"genre_id": 3, "name": "Crime", "parent_id": 2},
        {"genre_id": 4, "name": "Nordic Noir", "parent_id": 3},
        {"genre_id": 5, "name": "Courtroom", "parent_id": 1},
        {"genre_id": 8, "name": "Period Drama", "parent_id": 1},
        {"genre_id": 6, "name": "SciFi", "parent_id": None},
        {"genre_id": 7, "name": "Space Opera", "parent_id": 6},
    ],
    "titles": [
        {"title_id": 1, "name": "Fjord Shadows", "genre_id": 4, "release_year": 2021, "duration_seconds": 3300,
         "tags": ["premium", "subs"], "attributes": {"language": "no", "rating": 8.1}},
        {"title_id": 2, "name": "The Quiet Docket", "genre_id": 5, "release_year": 2019, "duration_seconds": 2700,
         "tags": ["subs"], "attributes": {"language": "en", "rating": 7.4}},
        {"title_id": 3, "name": "Harbour Lights", "genre_id": 1, "release_year": 2022, "duration_seconds": 3000,
         "tags": ["premium"], "attributes": {"language": "en", "rating": 8.6}},
        {"title_id": 4, "name": "Orbital Drift", "genre_id": 7, "release_year": 2023, "duration_seconds": 4200,
         "tags": ["premium", "4k"], "attributes": {"language": "en", "rating": 8.9}},
        {"title_id": 5, "name": "Cold Case North", "genre_id": 3, "release_year": 2020, "duration_seconds": 2400,
         "tags": ["subs"], "attributes": {"language": "sv", "rating": 7.9}},
        {"title_id": 6, "name": "Silent Appeal", "genre_id": 2, "release_year": 2018, "duration_seconds": 3600,
         "tags": [], "attributes": {"language": "en", "rating": 6.8}},
        {"title_id": 7, "name": "Void Signal", "genre_id": 6, "release_year": 2024, "duration_seconds": 3900,
         "tags": ["4k"], "attributes": {"language": "en", "rating": 7.1}},
        {"title_id": 8, "name": "Crown and Ledger", "genre_id": 8, "release_year": 2022, "duration_seconds": 3000,
         "tags": ["subs"], "attributes": {"language": "en", "rating": 8.0}},
    ],
    "plan_prices": [
        {"plan": "basic", "valid_from": "2024-01-01 00:00:00+00", "monthly_price": "7.99"},
        {"plan": "basic", "valid_from": "2024-03-01 00:00:00+00", "monthly_price": "8.99"},
        {"plan": "premium", "valid_from": "2024-01-01 00:00:00+00", "monthly_price": "12.99"},
        {"plan": "premium", "valid_from": "2024-02-15 00:00:00+00", "monthly_price": "14.99"},
    ],
    "subscriptions": [
        {"subscription_id": 1, "user_id": 100, "plan": "basic", "started_at": "2024-01-10 10:00:00+00", "cancelled_at": None},
        {"subscription_id": 2, "user_id": 100, "plan": "premium", "started_at": "2024-03-02 09:00:00+00", "cancelled_at": "2024-03-20 09:00:00+00"},
        {"subscription_id": 3, "user_id": 101, "plan": "premium", "started_at": "2024-02-14 23:30:00+00", "cancelled_at": None},
        {"subscription_id": 4, "user_id": 102, "plan": "basic", "started_at": "2024-02-20 08:00:00+00", "cancelled_at": "2024-04-01 08:00:00+00"},
        {"subscription_id": 5, "user_id": 103, "plan": "premium", "started_at": "2024-03-09 12:00:00+00", "cancelled_at": None},
        {"subscription_id": 6, "user_id": 102, "plan": "premium", "started_at": "2024-04-05 12:00:00+00", "cancelled_at": None},
    ],
    "plays": [
        {"play_id": 1, "subscription_id": 1, "title_id": 1, "played_at": "2024-03-04 23:30:00+00", "seconds_watched": 3300,
         "device": {"type": "tv", "os": "tizen"}, "tags": ["premium"]},
        {"play_id": 2, "subscription_id": 1, "title_id": 2, "played_at": "2024-03-05 01:30:00+00", "seconds_watched": 1200,
         "device": {"type": "mobile", "os": "android"}, "tags": ["premium"]},
        {"play_id": 3, "subscription_id": 3, "title_id": 3, "played_at": "2024-03-06 18:00:00+00", "seconds_watched": 3000,
         "device": {"type": "tv", "os": "webos"}, "tags": ["premium"]},
        {"play_id": 4, "subscription_id": 3, "title_id": 4, "played_at": "2024-03-07 20:00:00+00", "seconds_watched": 4200,
         "device": {"type": "tv", "os": "webos"}, "tags": ["premium", "hdr"]},
        {"play_id": 5, "subscription_id": 4, "title_id": 5, "played_at": "2024-03-08 09:00:00+00", "seconds_watched": 600,
         "device": {"type": "mobile", "os": "ios"}, "tags": ["premium"]},
        {"play_id": 6, "subscription_id": 4, "title_id": 1, "played_at": "2024-03-08 22:00:00+00", "seconds_watched": 3300,
         "device": {"type": "tablet", "os": "ios"}, "tags": ["premium"]},
        {"play_id": 7, "subscription_id": 5, "title_id": 4, "played_at": "2024-03-10 06:30:00+00", "seconds_watched": 4100,
         "device": {"type": "tv", "os": "tizen"}, "tags": ["premium"]},
        {"play_id": 8, "subscription_id": 5, "title_id": 3, "played_at": "2024-03-10 07:30:00+00", "seconds_watched": 2800,
         "device": {"type": "mobile", "os": "android"}, "tags": ["premium"]},
        {"play_id": 9, "subscription_id": 2, "title_id": 5, "played_at": "2024-03-11 10:00:00+00", "seconds_watched": 2400,
         "device": {"type": "tv", "os": "tizen"}, "tags": ["premium"]},
        {"play_id": 10, "subscription_id": 2, "title_id": 2, "played_at": "2024-03-12 11:00:00+00", "seconds_watched": 900,
         "device": {"type": "mobile", "os": "android"}, "tags": ["premium"]},
        {"play_id": 11, "subscription_id": 1, "title_id": 6, "played_at": "2024-02-01 12:00:00+00", "seconds_watched": 3600,
         "device": {"type": "tv", "os": "tizen"}, "tags": []},
        {"play_id": 12, "subscription_id": 3, "title_id": 3, "played_at": "2024-03-15 21:00:00+00", "seconds_watched": 1500,
         "device": {"type": "mobile", "os": "ios"}, "tags": ["premium"]},
        {"play_id": 13, "subscription_id": 1, "title_id": 4, "played_at": "2024-03-16 19:00:00+00", "seconds_watched": 4200,
         "device": {"type": "tv", "os": "tizen"}, "tags": ["premium"]},
        {"play_id": 14, "subscription_id": 4, "title_id": 6, "played_at": "2024-03-17 20:00:00+00", "seconds_watched": 1800,
         "device": {"type": "tablet", "os": "android"}, "tags": []},
        {"play_id": 15, "subscription_id": 2, "title_id": 6, "played_at": "2024-03-18 19:00:00+00", "seconds_watched": 1200,
         "device": {"type": "tv", "os": "tizen"}, "tags": []},
    ],
}

QUESTIONS = [
    {
        "skill": "grain_fanout_antijoin",
        "difficulty": "moderate",
        "question": "Which titles have never been played, and which genre is each of them in?",
        "sql": """
            SELECT t.title_id, t.name, g.name AS genre
            FROM titles AS t
            JOIN genres AS g ON g.genre_id = t.genre_id
            WHERE NOT EXISTS (SELECT 1 FROM plays AS p WHERE p.title_id = t.title_id)
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison, row order is not part of the answer.",
    },
    {
        "skill": "topn_ties",
        "difficulty": "challenging",
        "question": (
            "Which three titles have the highest total watched time across all plays? If titles tie, the smaller "
            "title_id comes first. Return title_id and total seconds, highest first."
        ),
        "sql": """
            SELECT t.title_id, SUM(p.seconds_watched) AS total_seconds
            FROM titles AS t
            JOIN plays AS p ON p.title_id = t.title_id
            GROUP BY t.title_id
            ORDER BY total_seconds DESC, t.title_id
            LIMIT 3
        """,
        "comparison": "ordered",
        "tie_policy": "Total seconds descending; ties broken by the smaller title_id (ORDER BY total_seconds DESC, title_id).",
    },
    {
        "skill": "asof_state",
        "difficulty": "challenging",
        "question": (
            "For every subscription, what was the monthly price of its plan at the moment the subscription started? "
            "Return subscription_id, plan and monthly_price."
        ),
        "sql": """
            SELECT DISTINCT ON (s.subscription_id)
                   s.subscription_id,
                   s.plan,
                   pp.monthly_price
            FROM subscriptions AS s
            JOIN plan_prices AS pp
              ON pp.plan = s.plan
             AND pp.valid_from <= s.started_at
            ORDER BY s.subscription_id, pp.valid_from DESC
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the price in force is unique because (plan, valid_from) is unique.",
    },
    {
        "skill": "periods_timezones",
        "difficulty": "challenging",
        "question": (
            "Count plays of at least 60 seconds per calendar day in New York local time (America/New_York) for "
            "4 March 2024 through 10 March 2024 inclusive, a week that contains the switch to daylight saving time. "
            "Return the local day and the number of plays, only for days with at least one play."
        ),
        "sql": """
            SELECT (p.played_at AT TIME ZONE 'America/New_York')::date AS local_day,
                   COUNT(*) AS plays
            FROM plays AS p
            WHERE p.seconds_watched >= 60
              AND (p.played_at AT TIME ZONE 'America/New_York')::date BETWEEN DATE '2024-03-04' AND DATE '2024-03-10'
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
            "For every play, return play_id and the total seconds watched on the same title over the window of that "
            "play and the seven days before it, both ends inclusive."
        ),
        "sql": """
            SELECT p.play_id,
                   SUM(p.seconds_watched) OVER (
                       PARTITION BY p.title_id
                       ORDER BY p.played_at
                       RANGE BETWEEN INTERVAL '7 days' PRECEDING AND CURRENT ROW
                   ) AS seconds_7d
            FROM plays AS p
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the frame is RANGE INTERVAL '7 days' PRECEDING to CURRENT ROW on played_at.",
    },
    {
        "skill": "jsonb_arrays",
        "difficulty": "moderate",
        "question": (
            "Which plays happened on a mobile device and carry the tag premium? Return play_id, the device operating "
            "system and how many tags the play has."
        ),
        "sql": """
            SELECT p.play_id,
                   p.device ->> 'os' AS os,
                   cardinality(p.tags) AS tag_count
            FROM plays AS p
            WHERE p.device ->> 'type' = 'mobile'
              AND p.tags @> ARRAY['premium']
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison, row order is not part of the answer.",
    },
    {
        "skill": "compositional",
        "difficulty": "challenging",
        "question": (
            "Among all plays, what share reached at least 90 percent of the title's duration? Return one row with "
            "the share between 0 and 1, rounded to four decimals."
        ),
        "sql": """
            SELECT ROUND(
                       COUNT(*) FILTER (WHERE p.seconds_watched >= 0.9 * t.duration_seconds)::numeric
                       / NULLIF(COUNT(*), 0),
                       4
                   ) AS completed_share
            FROM plays AS p
            JOIN titles AS t ON t.title_id = p.title_id
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: the result is a single row.",
    },
    {
        "skill": "hierarchies",
        "difficulty": "challenging",
        "question": (
            "Starting at the genre 'Drama', list that genre and every genre below it with the depth relative to Drama "
            "(Drama = 0) and the number of titles directly in each genre. Order by depth, then genre_id."
        ),
        "sql": """
            WITH RECURSIVE subtree AS (
                SELECT g.genre_id, g.name, 0 AS depth
                FROM genres AS g
                WHERE g.name = 'Drama'
                UNION ALL
                SELECT child.genre_id, child.name, parent.depth + 1
                FROM genres AS child
                JOIN subtree AS parent ON child.parent_id = parent.genre_id
            )
            SELECT s.genre_id,
                   s.name,
                   s.depth,
                   COUNT(t.title_id) AS titles
            FROM subtree AS s
            LEFT JOIN titles AS t ON t.genre_id = s.genre_id
            GROUP BY s.genre_id, s.name, s.depth
            ORDER BY s.depth, s.genre_id
        """,
        "comparison": "ordered",
        "tie_policy": "Depth ascending, then genre_id ascending; both keys together are unique in the result.",
    },
]

DROPPED = []

NOTES = [
    "The week 4-10 March 2024 contains the US daylight-saving switch; 06:30 and 07:30 UTC both fall on 10 March locally.",
    "Plan prices change on 2024-02-15 and 2024-03-01, and subscription 3 starts one hour before the premium change.",
    "Title 7 has no plays at all, so it anchors the anti-join question.",
    "Titles 1 and 6 both total 6600 seconds, so the top-3 tie-break (smaller title_id) is exercised at the cutoff.",
]
