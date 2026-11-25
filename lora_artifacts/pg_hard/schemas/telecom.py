"""telecom: flat network inventory (no hierarchy), plan history, usage and complaints.

The network inventory carries a region label but no parent relationship, so the
hierarchy skill cannot be supported honestly here: the question is dropped and a
second window question is used instead (see DROPPED).
"""

SLUG = "telecom"
DOMAIN = "mobile network: flat network inventory, plan fee history, usage events, complaints"

DDL = [
    """
    CREATE TABLE network_elements (
        element_id integer PRIMARY KEY,
        name text NOT NULL,
        region text NOT NULL
    )
    """,
    """
    CREATE TABLE plan_history (
        plan text NOT NULL,
        valid_from timestamptz NOT NULL,
        monthly_fee numeric(8,2) NOT NULL,
        PRIMARY KEY (plan, valid_from)
    )
    """,
    """
    CREATE TABLE subscribers (
        subscriber_id integer PRIMARY KEY,
        plan text NOT NULL,
        network_element_id integer NOT NULL REFERENCES network_elements(element_id),
        activated_at timestamptz NOT NULL,
        cancelled_at timestamptz
    )
    """,
    """
    CREATE TABLE usage_events (
        event_id integer PRIMARY KEY,
        subscriber_id integer NOT NULL REFERENCES subscribers(subscriber_id),
        occurred_at timestamptz NOT NULL,
        mb_used numeric(10,2) NOT NULL,
        qos jsonb NOT NULL,
        services text[] NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE complaints (
        complaint_id integer PRIMARY KEY,
        subscriber_id integer NOT NULL REFERENCES subscribers(subscriber_id),
        opened_at timestamptz NOT NULL
    )
    """,
]

FIXTURES = {
    "network_elements": [
        {"element_id": 1, "name": "Cell North 1", "region": "north"},
        {"element_id": 2, "name": "Cell North 2", "region": "north"},
        {"element_id": 3, "name": "Cell South 1", "region": "south"},
    ],
    "plan_history": [
        {"plan": "basic", "valid_from": "2024-01-01 00:00:00+00", "monthly_fee": "9.99"},
        {"plan": "basic", "valid_from": "2024-03-01 00:00:00+00", "monthly_fee": "11.99"},
        {"plan": "gold", "valid_from": "2024-01-01 00:00:00+00", "monthly_fee": "29.99"},
        {"plan": "gold", "valid_from": "2024-02-15 00:00:00+00", "monthly_fee": "34.99"},
    ],
    "subscribers": [
        {"subscriber_id": 1, "plan": "gold", "network_element_id": 1, "activated_at": "2024-02-14 23:30:00+00", "cancelled_at": None},
        {"subscriber_id": 2, "plan": "basic", "network_element_id": 1, "activated_at": "2024-02-20 09:00:00+00", "cancelled_at": None},
        {"subscriber_id": 3, "plan": "gold", "network_element_id": 2, "activated_at": "2024-03-04 22:30:00+00", "cancelled_at": None},
        {"subscriber_id": 4, "plan": "basic", "network_element_id": 3, "activated_at": "2024-03-09 18:00:00+00", "cancelled_at": "2024-04-01 09:00:00+00"},
        {"subscriber_id": 5, "plan": "gold", "network_element_id": 3, "activated_at": "2024-04-02 10:00:00+00", "cancelled_at": None},
    ],
    "usage_events": [
        {"event_id": 1, "subscriber_id": 1, "occurred_at": "2024-03-04 22:30:00+00", "mb_used": "1200.00",
         "qos": {"profile": "gold", "latency_ms": 18}, "services": ["voip", "streaming"]},
        {"event_id": 2, "subscriber_id": 1, "occurred_at": "2024-03-05 10:00:00+00", "mb_used": "2400.00",
         "qos": {"profile": "gold", "latency_ms": 22}, "services": ["streaming"]},
        {"event_id": 3, "subscriber_id": 2, "occurred_at": "2024-03-05 18:00:00+00", "mb_used": "800.00",
         "qos": {"profile": "silver", "latency_ms": 40}, "services": ["voip"]},
        {"event_id": 4, "subscriber_id": 3, "occurred_at": "2024-03-06 23:30:00+00", "mb_used": "5100.00",
         "qos": {"profile": "gold", "latency_ms": 15}, "services": ["streaming", "voip"]},
        {"event_id": 5, "subscriber_id": 3, "occurred_at": "2024-03-07 09:00:00+00", "mb_used": "3200.00",
         "qos": {"profile": "gold", "latency_ms": 19}, "services": ["streaming"]},
        {"event_id": 6, "subscriber_id": 4, "occurred_at": "2024-03-08 12:00:00+00", "mb_used": "1500.00",
         "qos": {"profile": "silver", "latency_ms": 55}, "services": ["voip"]},
        {"event_id": 7, "subscriber_id": 4, "occurred_at": "2024-03-10 20:00:00+00", "mb_used": "900.00",
         "qos": {"profile": "silver", "latency_ms": 48}, "services": []},
        {"event_id": 8, "subscriber_id": 2, "occurred_at": "2024-03-11 04:00:00+00", "mb_used": "6100.00",
         "qos": {"profile": "silver", "latency_ms": 35}, "services": ["streaming"]},
        {"event_id": 9, "subscriber_id": 1, "occurred_at": "2024-03-12 10:00:00+00", "mb_used": "3300.00",
         "qos": {"profile": "gold", "latency_ms": 20}, "services": []},
    ],
    "complaints": [
        {"complaint_id": 1, "subscriber_id": 1, "opened_at": "2024-03-06 09:00:00+00"},
        {"complaint_id": 2, "subscriber_id": 3, "opened_at": "2024-03-08 11:00:00+00"},
        {"complaint_id": 3, "subscriber_id": 1, "opened_at": "2024-03-12 15:00:00+00"},
    ],
}

QUESTIONS = [
    {
        "skill": "grain_fanout_antijoin",
        "difficulty": "challenging",
        "question": (
            "For every subscriber with at least one complaint, return the subscriber id and the total megabytes used, "
            "counting each usage event exactly once even when the subscriber has several complaints."
        ),
        "sql": """
            SELECT u.subscriber_id,
                   SUM(u.mb_used) AS total_mb
            FROM usage_events AS u
            WHERE EXISTS (SELECT 1 FROM complaints AS c WHERE c.subscriber_id = u.subscriber_id)
            GROUP BY u.subscriber_id
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; one row per subscriber in scope.",
    },
    {
        "skill": "topn_ties",
        "difficulty": "challenging",
        "question": (
            "Which two subscribers used the most megabytes? If subscribers tie, the smaller subscriber_id comes "
            "first. Return subscriber_id and total megabytes, highest first."
        ),
        "sql": """
            SELECT u.subscriber_id, SUM(u.mb_used) AS total_mb
            FROM usage_events AS u
            GROUP BY u.subscriber_id
            ORDER BY total_mb DESC, u.subscriber_id
            LIMIT 2
        """,
        "comparison": "ordered",
        "tie_policy": "Total megabytes descending; ties broken by the smaller subscriber_id (ORDER BY total_mb DESC, subscriber_id).",
    },
    {
        "skill": "asof_state",
        "difficulty": "challenging",
        "question": (
            "For every subscriber, what was the monthly fee of their plan at the moment they activated? Return "
            "subscriber_id, plan and monthly_fee."
        ),
        "sql": """
            SELECT s.subscriber_id,
                   s.plan,
                   fee.monthly_fee
            FROM subscribers AS s
            JOIN LATERAL (
                SELECT ph.monthly_fee
                FROM plan_history AS ph
                WHERE ph.plan = s.plan
                  AND ph.valid_from <= s.activated_at
                ORDER BY ph.valid_from DESC
                LIMIT 1
            ) AS fee ON true
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the fee in force is unique because (plan, valid_from) is unique.",
    },
    {
        "skill": "periods_timezones",
        "difficulty": "moderate",
        "question": (
            "Count usage events per calendar day in Singapore local time (Asia/Singapore, UTC+8) for 4 March 2024 "
            "through 10 March 2024 inclusive. Return the local day and the count, only for days with at least one "
            "event."
        ),
        "sql": """
            SELECT (u.occurred_at AT TIME ZONE 'Asia/Singapore')::date AS local_day,
                   COUNT(*) AS events
            FROM usage_events AS u
            WHERE (u.occurred_at AT TIME ZONE 'Asia/Singapore')::date BETWEEN DATE '2024-03-04' AND DATE '2024-03-10'
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
            "For every usage event, return event_id and the subscriber's total megabytes over the window of that "
            "event's timestamp and the seven days before it, both ends inclusive."
        ),
        "sql": """
            SELECT u.event_id,
                   SUM(u.mb_used) OVER (
                       PARTITION BY u.subscriber_id
                       ORDER BY u.occurred_at
                       RANGE BETWEEN INTERVAL '7 days' PRECEDING AND CURRENT ROW
                   ) AS mb_7d
            FROM usage_events AS u
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the frame is RANGE INTERVAL '7 days' PRECEDING to CURRENT ROW.",
    },
    {
        "skill": "jsonb_arrays",
        "difficulty": "moderate",
        "question": (
            "Which usage events ran on the gold QoS profile and include the voip service? Return event_id, the "
            "latency in milliseconds and how many services the event lists."
        ),
        "sql": """
            SELECT u.event_id,
                   (u.qos ->> 'latency_ms')::integer AS latency_ms,
                   cardinality(u.services) AS service_count
            FROM usage_events AS u
            WHERE u.qos ->> 'profile' = 'gold'
              AND u.services @> ARRAY['voip']
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison, row order is not part of the answer.",
    },
    {
        "skill": "compositional",
        "difficulty": "challenging",
        "question": (
            "Among subscribers who used more than 5000 megabytes in total, what share has at least one complaint? "
            "Return one row with the share between 0 and 1, rounded to four decimals."
        ),
        "sql": """
            SELECT ROUND(
                       COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM complaints AS c WHERE c.subscriber_id = usage.subscriber_id))::numeric
                       / NULLIF(COUNT(*), 0),
                       4
                   ) AS complain_share
            FROM (
                SELECT u.subscriber_id
                FROM usage_events AS u
                GROUP BY u.subscriber_id
                HAVING SUM(u.mb_used) > 5000
            ) AS usage
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: the result is a single row.",
    },
    {
        "skill": "window_frames_cohorts",
        "difficulty": "challenging",
        "question": (
            "For every complaint, return complaint_id and the subscriber's total megabytes over the thirty days "
            "ending at the complaint time, both ends inclusive, counting only usage events of that subscriber."
        ),
        "sql": """
            WITH pairs AS (
                SELECT c.complaint_id,
                       c.subscriber_id,
                       c.opened_at,
                       u.mb_used
                FROM complaints AS c
                LEFT JOIN usage_events AS u
                  ON u.subscriber_id = c.subscriber_id
                 AND u.occurred_at <= c.opened_at
                 AND u.occurred_at >= c.opened_at - INTERVAL '30 days'
            )
            SELECT complaint_id,
                   COALESCE(SUM(mb_used), 0) AS mb_30d
            FROM pairs
            GROUP BY complaint_id
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; one row per complaint.",
    },
]

DROPPED = [
    {
        "skill": "hierarchies",
        "reason": (
            "the network inventory is flat: elements carry a region label and no parent relationship, so a hierarchy "
            "question would have to invent a tree that the schema does not encode"
        ),
        "replacement": "a second window_frames_cohorts question (subscriber usage over the 30 days before each complaint)",
    },
]

NOTES = [
    "Subscriber 1 has two complaints and three usage events, so the fan-out question must not multiply the totals.",
    "The gold plan fee changes on 2024-02-15 and subscriber 1 activates at 23:30 UTC the day before.",
    "Subscribers 1 and 2 both use 6900.00 MB, so the top-2 tie-break (smaller subscriber_id) is exercised at the cutoff.",
    "The hierarchy skill is dropped here on purpose rather than weakened (see DROPPED).",
]
