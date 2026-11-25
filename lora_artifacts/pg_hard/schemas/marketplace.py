"""marketplace: category tree, seller rating history, listings, offers and disputes."""

SLUG = "marketplace"
DOMAIN = "online marketplace: category tree, seller rating history, listings, offers and disputes"

DDL = [
    """
    CREATE TABLE categories (
        category_id integer PRIMARY KEY,
        name text NOT NULL,
        parent_id integer REFERENCES categories(category_id)
    )
    """,
    """
    CREATE TABLE sellers (
        seller_id integer PRIMARY KEY,
        name text NOT NULL
    )
    """,
    """
    CREATE TABLE seller_ratings (
        seller_id integer NOT NULL REFERENCES sellers(seller_id),
        valid_from timestamptz NOT NULL,
        rating numeric(3,2) NOT NULL,
        PRIMARY KEY (seller_id, valid_from)
    )
    """,
    """
    CREATE TABLE listings (
        listing_id integer PRIMARY KEY,
        seller_id integer NOT NULL REFERENCES sellers(seller_id),
        category_id integer NOT NULL REFERENCES categories(category_id),
        created_at timestamptz NOT NULL,
        price numeric(10,2) NOT NULL,
        attributes jsonb NOT NULL,
        tags text[] NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE offers (
        offer_id integer PRIMARY KEY,
        listing_id integer NOT NULL REFERENCES listings(listing_id),
        amount numeric(10,2) NOT NULL,
        status text NOT NULL
    )
    """,
    """
    CREATE TABLE disputes (
        dispute_id integer PRIMARY KEY,
        listing_id integer NOT NULL REFERENCES listings(listing_id),
        reason text NOT NULL
    )
    """,
]

FIXTURES = {
    "categories": [
        {"category_id": 1, "name": "Electronics", "parent_id": None},
        {"category_id": 2, "name": "Phones", "parent_id": 1},
        {"category_id": 3, "name": "Audio", "parent_id": 1},
        {"category_id": 4, "name": "Smartphones", "parent_id": 2},
        {"category_id": 5, "name": "Headphones", "parent_id": 3},
        {"category_id": 6, "name": "Furniture", "parent_id": None},
    ],
    "sellers": [
        {"seller_id": 1, "name": "Volt"},
        {"seller_id": 2, "name": "Nimbus"},
        {"seller_id": 3, "name": "Orbit"},
    ],
    "seller_ratings": [
        {"seller_id": 1, "valid_from": "2024-01-01 00:00:00+00", "rating": "4.60"},
        {"seller_id": 1, "valid_from": "2024-03-01 00:00:00+00", "rating": "4.20"},
        {"seller_id": 2, "valid_from": "2024-01-01 00:00:00+00", "rating": "4.90"},
        {"seller_id": 3, "valid_from": "2024-02-15 00:00:00+00", "rating": "4.10"},
    ],
    "listings": [
        {"listing_id": 1, "seller_id": 1, "category_id": 4, "created_at": "2024-03-04 22:30:00+00", "price": "699.00",
         "attributes": {"condition": "new", "color": "black"}, "tags": ["electronics", "phone"]},
        {"listing_id": 2, "seller_id": 1, "category_id": 5, "created_at": "2024-03-05 10:00:00+00", "price": "199.00",
         "attributes": {"condition": "used", "color": "white"}, "tags": ["electronics", "audio"]},
        {"listing_id": 3, "seller_id": 2, "category_id": 4, "created_at": "2024-03-06 23:30:00+00", "price": "749.00",
         "attributes": {"condition": "new", "color": "blue"}, "tags": ["electronics", "phone"]},
        {"listing_id": 4, "seller_id": 2, "category_id": 5, "created_at": "2024-03-07 09:00:00+00", "price": "159.00",
         "attributes": {"condition": "new", "color": "black"}, "tags": ["electronics"]},
        {"listing_id": 5, "seller_id": 3, "category_id": 6, "created_at": "2024-03-08 18:00:00+00", "price": "349.00",
         "attributes": {"condition": "used", "color": "oak"}, "tags": ["furniture"]},
        {"listing_id": 6, "seller_id": 3, "category_id": 6, "created_at": "2024-03-10 08:00:00+00", "price": "249.00",
         "attributes": {"condition": "new", "color": "pine"}, "tags": ["furniture"]},
        {"listing_id": 7, "seller_id": 1, "category_id": 4, "created_at": "2024-03-11 12:00:00+00", "price": "649.00",
         "attributes": {"condition": "new", "color": "green"}, "tags": ["electronics", "phone"]},
    ],
    "offers": [
        {"offer_id": 1, "listing_id": 1, "amount": "650.00", "status": "accepted"},
        {"offer_id": 2, "listing_id": 1, "amount": "600.00", "status": "rejected"},
        {"offer_id": 3, "listing_id": 2, "amount": "180.00", "status": "accepted"},
        {"offer_id": 4, "listing_id": 3, "amount": "700.00", "status": "accepted"},
        {"offer_id": 5, "listing_id": 3, "amount": "680.00", "status": "rejected"},
        {"offer_id": 6, "listing_id": 4, "amount": "150.00", "status": "accepted"},
        {"offer_id": 7, "listing_id": 5, "amount": "850.00", "status": "accepted"},
        {"offer_id": 8, "listing_id": 7, "amount": "640.00", "status": "accepted"},
        {"offer_id": 9, "listing_id": 7, "amount": "620.00", "status": "rejected"},
    ],
    "disputes": [
        {"dispute_id": 1, "listing_id": 1, "reason": "not_as_described"},
        {"dispute_id": 2, "listing_id": 1, "reason": "late_shipping"},
        {"dispute_id": 3, "listing_id": 5, "reason": "damaged"},
    ],
}

QUESTIONS = [
    {
        "skill": "grain_fanout_antijoin",
        "difficulty": "challenging",
        "question": (
            "For every listing that has at least one dispute, return the listing id, the number of offers and the "
            "highest offer amount, counting each offer exactly once even when the listing has several disputes."
        ),
        "sql": """
            SELECT l.listing_id,
                   COUNT(o.offer_id) AS offers,
                   MAX(o.amount) AS highest_offer
            FROM listings AS l
            JOIN offers AS o ON o.listing_id = l.listing_id
            WHERE EXISTS (SELECT 1 FROM disputes AS d WHERE d.listing_id = l.listing_id)
            GROUP BY l.listing_id
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; one row per listing in scope.",
    },
    {
        "skill": "topn_ties",
        "difficulty": "challenging",
        "question": (
            "Which two sellers received the most money in accepted offers? If sellers tie, the smaller seller_id "
            "comes first. Return seller_id and accepted_amount, highest first."
        ),
        "sql": """
            SELECT l.seller_id,
                   SUM(o.amount) AS accepted_amount
            FROM offers AS o
            JOIN listings AS l ON l.listing_id = o.listing_id
            WHERE o.status = 'accepted'
            GROUP BY l.seller_id
            ORDER BY accepted_amount DESC, l.seller_id
            LIMIT 2
        """,
        "comparison": "ordered",
        "tie_policy": "Accepted amount descending; ties broken by the smaller seller_id (ORDER BY accepted_amount DESC, seller_id).",
    },
    {
        "skill": "asof_state",
        "difficulty": "challenging",
        "question": (
            "For every listing, what was the seller's rating at the moment the listing was created? Return "
            "listing_id, seller_id and rating."
        ),
        "sql": """
            SELECT l.listing_id,
                   l.seller_id,
                   r.rating
            FROM listings AS l
            JOIN LATERAL (
                SELECT sr.rating
                FROM seller_ratings AS sr
                WHERE sr.seller_id = l.seller_id
                  AND sr.valid_from <= l.created_at
                ORDER BY sr.valid_from DESC
                LIMIT 1
            ) AS r ON true
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the rating in force is unique because (seller_id, valid_from) is unique.",
    },
    {
        "skill": "periods_timezones",
        "difficulty": "moderate",
        "question": (
            "Count new listings per calendar day in London local time (Europe/London, GMT in early March 2024) for "
            "4 March 2024 through 10 March 2024 inclusive. Return the local day and the count, only for days with at "
            "least one listing."
        ),
        "sql": """
            SELECT (l.created_at AT TIME ZONE 'Europe/London')::date AS local_day,
                   COUNT(*) AS listings
            FROM listings AS l
            WHERE (l.created_at AT TIME ZONE 'Europe/London')::date BETWEEN DATE '2024-03-04' AND DATE '2024-03-10'
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
            "For every listing, return listing_id and how many listings were created in the same category over the "
            "window of that listing's creation time and the seven days before it, both ends inclusive."
        ),
        "sql": """
            SELECT l.listing_id,
                   COUNT(*) OVER (
                       PARTITION BY l.category_id
                       ORDER BY l.created_at
                       RANGE BETWEEN INTERVAL '7 days' PRECEDING AND CURRENT ROW
                   ) AS listings_7d
            FROM listings AS l
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the frame is RANGE INTERVAL '7 days' PRECEDING to CURRENT ROW.",
    },
    {
        "skill": "jsonb_arrays",
        "difficulty": "moderate",
        "question": (
            "Which listings are in new condition and tagged electronics? Return listing_id, the color attribute and "
            "how many tags the listing has."
        ),
        "sql": """
            SELECT l.listing_id,
                   l.attributes ->> 'color' AS color,
                   cardinality(l.tags) AS tag_count
            FROM listings AS l
            WHERE l.attributes ->> 'condition' = 'new'
              AND l.tags @> ARRAY['electronics']
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison, row order is not part of the answer.",
    },
    {
        "skill": "compositional",
        "difficulty": "challenging",
        "question": (
            "Among listings that have at least one offer, what share has at least one dispute? Return one row with the "
            "share between 0 and 1, rounded to four decimals."
        ),
        "sql": """
            SELECT ROUND(
                       COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM disputes AS d WHERE d.listing_id = l.listing_id))::numeric
                       / NULLIF(COUNT(*), 0),
                       4
                   ) AS disputed_share
            FROM listings AS l
            WHERE EXISTS (SELECT 1 FROM offers AS o WHERE o.listing_id = l.listing_id)
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: the result is a single row.",
    },
    {
        "skill": "hierarchies",
        "difficulty": "challenging",
        "question": (
            "Starting at the category 'Electronics', list that category and every category below it with the depth "
            "relative to Electronics (Electronics = 0) and the number of listings directly in each category. Order by "
            "depth, then category_id."
        ),
        "sql": """
            WITH RECURSIVE subtree AS (
                SELECT c.category_id, c.name, 0 AS depth
                FROM categories AS c
                WHERE c.name = 'Electronics'
                UNION ALL
                SELECT child.category_id, child.name, parent.depth + 1
                FROM categories AS child
                JOIN subtree AS parent ON child.parent_id = parent.category_id
            )
            SELECT s.category_id,
                   s.name,
                   s.depth,
                   COUNT(l.listing_id) AS listings
            FROM subtree AS s
            LEFT JOIN listings AS l ON l.category_id = s.category_id
            GROUP BY s.category_id, s.name, s.depth
            ORDER BY s.depth, s.category_id
        """,
        "comparison": "ordered",
        "tie_policy": "Depth ascending, then category_id ascending; both keys together are unique in the result.",
    },
]

DROPPED = []

NOTES = [
    "Listing 1 has two disputes and two offers, so a plain join multiplies the offer count.",
    "Seller 1's rating drops on 2024-03-01, so the as-of answer differs for listings created before and after it.",
    "The 22:30 and 23:30 UTC listings land on different London days than in UTC.",
    "Sellers 2 and 3 both earn 850.00, so the top-2 tie-break (smaller seller_id) is exercised at the cutoff.",
]
