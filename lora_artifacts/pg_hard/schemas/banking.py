"""banking: accounting tree, double-entry postings, chargebacks, tier history, half-hour timezone."""

SLUG = "banking"
DOMAIN = "banking ledger: account tree, double-entry postings, chargebacks, fee-tier history"

DDL = [
    """
    CREATE TABLE accounts (
        account_id integer PRIMARY KEY,
        name text NOT NULL,
        parent_account_id integer REFERENCES accounts(account_id),
        currency text NOT NULL
    )
    """,
    """
    CREATE TABLE account_tiers (
        account_id integer NOT NULL REFERENCES accounts(account_id),
        valid_from timestamptz NOT NULL,
        tier text NOT NULL,
        monthly_fee numeric(8,2) NOT NULL,
        PRIMARY KEY (account_id, valid_from)
    )
    """,
    """
    CREATE TABLE transactions (
        txn_id integer PRIMARY KEY,
        account_id integer NOT NULL REFERENCES accounts(account_id),
        posted_at timestamptz NOT NULL,
        amount numeric(12,2) NOT NULL,
        status text NOT NULL,
        channel jsonb NOT NULL,
        tags text[] NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE postings (
        posting_id integer PRIMARY KEY,
        txn_id integer NOT NULL REFERENCES transactions(txn_id),
        amount numeric(12,2) NOT NULL,
        direction text NOT NULL
    )
    """,
    """
    CREATE TABLE chargebacks (
        chargeback_id integer PRIMARY KEY,
        txn_id integer NOT NULL REFERENCES transactions(txn_id),
        amount numeric(12,2) NOT NULL
    )
    """,
]

FIXTURES = {
    "accounts": [
        {"account_id": 1, "name": "Holding", "parent_account_id": None, "currency": "EUR"},
        {"account_id": 2, "name": "Operations", "parent_account_id": 1, "currency": "EUR"},
        {"account_id": 3, "name": "Payroll", "parent_account_id": 2, "currency": "EUR"},
        {"account_id": 4, "name": "Procurement", "parent_account_id": 2, "currency": "EUR"},
        {"account_id": 5, "name": "Treasury", "parent_account_id": 1, "currency": "EUR"},
        {"account_id": 6, "name": "Customer Escrow", "parent_account_id": None, "currency": "INR"},
    ],
    "account_tiers": [
        {"account_id": 2, "valid_from": "2024-01-01 00:00:00+00", "tier": "standard", "monthly_fee": "0.00"},
        {"account_id": 2, "valid_from": "2024-03-01 00:00:00+00", "tier": "business", "monthly_fee": "19.00"},
        {"account_id": 3, "valid_from": "2024-01-01 00:00:00+00", "tier": "standard", "monthly_fee": "0.00"},
        {"account_id": 4, "valid_from": "2024-02-01 00:00:00+00", "tier": "business", "monthly_fee": "19.00"},
        {"account_id": 5, "valid_from": "2024-01-15 00:00:00+00", "tier": "premium", "monthly_fee": "49.00"},
        {"account_id": 6, "valid_from": "2024-01-01 00:00:00+00", "tier": "escrow", "monthly_fee": "99.00"},
    ],
    "transactions": [
        {"txn_id": 1, "account_id": 2, "posted_at": "2024-03-04 22:45:00+00", "amount": "1200.00", "status": "settled",
         "channel": {"type": "wire", "reference": "W-1"}, "tags": ["review"]},
        {"txn_id": 2, "account_id": 2, "posted_at": "2024-03-05 03:15:00+00", "amount": "850.00", "status": "settled",
         "channel": {"type": "card", "reference": "C-2"}, "tags": []},
        {"txn_id": 3, "account_id": 2, "posted_at": "2024-03-06 18:30:00+00", "amount": "2400.00", "status": "settled",
         "channel": {"type": "wire", "reference": "W-3"}, "tags": ["review", "manual"]},
        {"txn_id": 4, "account_id": 3, "posted_at": "2024-03-07 09:00:00+00", "amount": "15000.00", "status": "settled",
         "channel": {"type": "sepa", "reference": "S-4"}, "tags": ["payroll"]},
        {"txn_id": 5, "account_id": 4, "posted_at": "2024-03-08 11:00:00+00", "amount": "3200.00", "status": "settled",
         "channel": {"type": "card", "reference": "C-5"}, "tags": []},
        {"txn_id": 6, "account_id": 4, "posted_at": "2024-03-09 20:00:00+00", "amount": "450.00", "status": "reversed",
         "channel": {"type": "card", "reference": "C-6"}, "tags": ["review"]},
        {"txn_id": 7, "account_id": 5, "posted_at": "2024-03-10 06:00:00+00", "amount": "50000.00", "status": "settled",
         "channel": {"type": "wire", "reference": "W-7"}, "tags": ["manual"]},
        {"txn_id": 8, "account_id": 6, "posted_at": "2024-03-10 18:20:00+00", "amount": "1500.00", "status": "settled",
         "channel": {"type": "upi", "reference": "U-8"}, "tags": ["review"]},
        {"txn_id": 9, "account_id": 6, "posted_at": "2024-03-10 18:29:00+00", "amount": "28500.00", "status": "settled",
         "channel": {"type": "upi", "reference": "U-9"}, "tags": []},
        {"txn_id": 10, "account_id": 2, "posted_at": "2024-03-11 12:00:00+00", "amount": "980.00", "status": "settled",
         "channel": {"type": "card", "reference": "C-10"}, "tags": []},
        {"txn_id": 11, "account_id": 3, "posted_at": "2024-02-28 08:00:00+00", "amount": "15000.00", "status": "settled",
         "channel": {"type": "sepa", "reference": "S-11"}, "tags": ["payroll"]},
        {"txn_id": 12, "account_id": 4, "posted_at": "2024-03-12 15:00:00+00", "amount": "6100.00", "status": "settled",
         "channel": {"type": "wire", "reference": "W-12"}, "tags": ["review"]},
    ],
    "postings": [
        {"posting_id": 1, "txn_id": 1, "amount": "1200.00", "direction": "credit"},
        {"posting_id": 2, "txn_id": 2, "amount": "850.00", "direction": "credit"},
        {"posting_id": 3, "txn_id": 3, "amount": "2400.00", "direction": "credit"},
        {"posting_id": 4, "txn_id": 3, "amount": "-1400.00", "direction": "debit"},
        {"posting_id": 5, "txn_id": 4, "amount": "15000.00", "direction": "credit"},
        {"posting_id": 6, "txn_id": 5, "amount": "3200.00", "direction": "credit"},
        {"posting_id": 7, "txn_id": 6, "amount": "450.00", "direction": "credit"},
        {"posting_id": 8, "txn_id": 6, "amount": "-450.00", "direction": "debit"},
        {"posting_id": 9, "txn_id": 7, "amount": "50000.00", "direction": "credit"},
        {"posting_id": 10, "txn_id": 8, "amount": "1500.00", "direction": "credit"},
        {"posting_id": 11, "txn_id": 9, "amount": "28500.00", "direction": "credit"},
        {"posting_id": 12, "txn_id": 10, "amount": "980.00", "direction": "credit"},
        {"posting_id": 13, "txn_id": 11, "amount": "15000.00", "direction": "credit"},
        {"posting_id": 14, "txn_id": 12, "amount": "6100.00", "direction": "credit"},
        {"posting_id": 15, "txn_id": 12, "amount": "-4900.00", "direction": "debit"},
    ],
    "chargebacks": [
        {"chargeback_id": 1, "txn_id": 3, "amount": "300.00"},
        {"chargeback_id": 2, "txn_id": 3, "amount": "200.00"},
        {"chargeback_id": 3, "txn_id": 6, "amount": "450.00"},
        {"chargeback_id": 4, "txn_id": 12, "amount": "1100.00"},
    ],
}

QUESTIONS = [
    {
        "skill": "grain_fanout_antijoin",
        "difficulty": "challenging",
        "question": (
            "For every account with at least one chargeback on its transactions, return the account id and the sum of "
            "all posting amounts on that account, counting each posting exactly once even when a transaction has "
            "several chargebacks."
        ),
        "sql": """
            SELECT t.account_id,
                   SUM(p.amount) AS posted_amount
            FROM transactions AS t
            JOIN postings AS p ON p.txn_id = t.txn_id
            WHERE EXISTS (SELECT 1 FROM chargebacks AS c WHERE c.txn_id = t.txn_id)
            GROUP BY t.account_id
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; one row per account in scope.",
    },
    {
        "skill": "topn_ties",
        "difficulty": "challenging",
        "question": (
            "Which two accounts have the highest net ledger amount, where net is the sum of posting amounts on the "
            "account's transactions minus all chargebacks on those transactions? If accounts tie, the smaller "
            "account_id comes first. Return account_id and net_amount, highest first."
        ),
        "sql": """
            WITH posted AS (
                SELECT t.account_id, SUM(p.amount) AS amount
                FROM transactions AS t
                JOIN postings AS p ON p.txn_id = t.txn_id
                GROUP BY t.account_id
            ), charged_back AS (
                SELECT t.account_id, SUM(c.amount) AS amount
                FROM transactions AS t
                JOIN chargebacks AS c ON c.txn_id = t.txn_id
                GROUP BY t.account_id
            )
            SELECT a.account_id,
                   COALESCE(posted.amount, 0) - COALESCE(charged_back.amount, 0) AS net_amount
            FROM accounts AS a
            LEFT JOIN posted ON posted.account_id = a.account_id
            LEFT JOIN charged_back ON charged_back.account_id = a.account_id
            ORDER BY net_amount DESC, a.account_id
            LIMIT 2
        """,
        "comparison": "ordered",
        "tie_policy": "Net amount descending; ties broken by the smaller account_id (ORDER BY net_amount DESC, account_id).",
    },
    {
        "skill": "asof_state",
        "difficulty": "challenging",
        "question": (
            "For every transaction, which fee tier was in force on its account at the transaction time, and what was "
            "that tier's monthly fee? Return txn_id, tier and monthly_fee."
        ),
        "sql": """
            SELECT DISTINCT ON (t.txn_id)
                   t.txn_id,
                   tier.tier,
                   tier.monthly_fee
            FROM transactions AS t
            JOIN account_tiers AS tier
              ON tier.account_id = t.account_id
             AND tier.valid_from <= t.posted_at
            ORDER BY t.txn_id, tier.valid_from DESC
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the tier in force is unique because (account_id, valid_from) is unique.",
    },
    {
        "skill": "periods_timezones",
        "difficulty": "challenging",
        "question": (
            "Group transactions by calendar day in India Standard Time (Asia/Kolkata, UTC+05:30) for 10 March 2024 "
            "through 12 March 2024 inclusive and return the local day, the number of settled transactions and their "
            "total amount."
        ),
        "sql": """
            SELECT (t.posted_at AT TIME ZONE 'Asia/Kolkata')::date AS local_day,
                   COUNT(*) AS settled_txns,
                   SUM(t.amount) AS total_amount
            FROM transactions AS t
            WHERE t.status = 'settled'
              AND (t.posted_at AT TIME ZONE 'Asia/Kolkata')::date BETWEEN DATE '2024-03-10' AND DATE '2024-03-12'
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
            "For every transaction, return txn_id and the running total of the account's posting amounts ordered by "
            "transaction time, including all earlier transactions of that account and the current one."
        ),
        "sql": """
            WITH per_txn AS (
                SELECT t.txn_id,
                       t.account_id,
                       t.posted_at,
                       COALESCE((SELECT SUM(p.amount) FROM postings AS p WHERE p.txn_id = t.txn_id), 0) AS amount
                FROM transactions AS t
            )
            SELECT txn_id,
                   SUM(amount) OVER (
                       PARTITION BY account_id
                       ORDER BY posted_at, txn_id
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS running_total
            FROM per_txn
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the frame is UNBOUNDED PRECEDING to CURRENT ROW with (posted_at, txn_id) ordering.",
    },
    {
        "skill": "jsonb_arrays",
        "difficulty": "moderate",
        "question": (
            "Which transactions went through the wire channel and carry the tag review? Return txn_id, the channel "
            "reference and how many tags the transaction has."
        ),
        "sql": """
            SELECT t.txn_id,
                   t.channel ->> 'reference' AS reference,
                   cardinality(t.tags) AS tag_count
            FROM transactions AS t
            WHERE t.channel ->> 'type' = 'wire'
              AND t.tags @> ARRAY['review']
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison, row order is not part of the answer.",
    },
    {
        "skill": "compositional",
        "difficulty": "challenging",
        "question": (
            "Among settled transactions of at least 1000, what share has at least one chargeback? Return one row with "
            "the share between 0 and 1, rounded to four decimals."
        ),
        "sql": """
            SELECT ROUND(
                       COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM chargebacks AS c WHERE c.txn_id = t.txn_id))::numeric
                       / NULLIF(COUNT(*), 0),
                       4
                   ) AS chargeback_share
            FROM transactions AS t
            WHERE t.status = 'settled'
              AND t.amount >= 1000
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: the result is a single row.",
    },
    {
        "skill": "hierarchies",
        "difficulty": "challenging",
        "question": (
            "Starting at the account 'Holding', list that account and every account below it with the depth relative "
            "to Holding (Holding = 0) and the sum of posting amounts booked directly on each account. Order by depth, "
            "then account_id."
        ),
        "sql": """
            WITH RECURSIVE subtree AS (
                SELECT a.account_id, a.name, 0 AS depth
                FROM accounts AS a
                WHERE a.name = 'Holding'
                UNION ALL
                SELECT child.account_id, child.name, parent.depth + 1
                FROM accounts AS child
                JOIN subtree AS parent ON child.parent_account_id = parent.account_id
            )
            SELECT s.account_id,
                   s.name,
                   s.depth,
                   COALESCE(SUM(p.amount), 0) AS posted_amount
            FROM subtree AS s
            LEFT JOIN transactions AS t ON t.account_id = s.account_id
            LEFT JOIN postings AS p ON p.txn_id = t.txn_id
            GROUP BY s.account_id, s.name, s.depth
            ORDER BY s.depth, s.account_id
        """,
        "comparison": "ordered",
        "tie_policy": "Depth ascending, then account_id ascending; both keys together are unique in the result.",
    },
]

DROPPED = []

NOTES = [
    "Transaction 3 has two postings and two chargebacks, so a plain join multiplies both sides.",
    "The 05:30 UTC offset makes the 18:20 and 18:29 UTC transactions land on different India days than in UTC.",
    "Account 4 switches to the business tier on 2024-02-01, so the as-of answer differs from the current tier.",
    "Accounts 3 and 6 tie at 30000.00, so the top-2 tie-break (smaller account_id) is exercised at the cutoff.",
]
