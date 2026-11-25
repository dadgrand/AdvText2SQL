"""retail_orders: category tree, price history, fan-out between items and returns."""

SLUG = "retail_orders"
DOMAIN = "retail orders: product category tree, list-price history, order lines, partial returns"

DDL = [
    """
    CREATE TABLE categories (
        category_id integer PRIMARY KEY,
        name text NOT NULL,
        parent_id integer REFERENCES categories(category_id)
    )
    """,
    """
    CREATE TABLE products (
        product_id integer PRIMARY KEY,
        name text NOT NULL,
        category_id integer NOT NULL REFERENCES categories(category_id),
        tags text[] NOT NULL DEFAULT '{}',
        attributes jsonb NOT NULL
    )
    """,
    """
    CREATE TABLE product_prices (
        product_id integer NOT NULL REFERENCES products(product_id),
        valid_from timestamptz NOT NULL,
        list_price numeric(10,2) NOT NULL,
        PRIMARY KEY (product_id, valid_from)
    )
    """,
    """
    CREATE TABLE orders (
        order_id integer PRIMARY KEY,
        customer_id integer NOT NULL,
        placed_at timestamptz NOT NULL,
        status text NOT NULL,
        shipping jsonb NOT NULL,
        promo_codes text[] NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE order_items (
        order_id integer NOT NULL REFERENCES orders(order_id),
        product_id integer NOT NULL REFERENCES products(product_id),
        quantity integer NOT NULL,
        unit_price numeric(10,2) NOT NULL,
        PRIMARY KEY (order_id, product_id)
    )
    """,
    """
    CREATE TABLE returns (
        return_id integer PRIMARY KEY,
        order_id integer NOT NULL REFERENCES orders(order_id),
        product_id integer NOT NULL REFERENCES products(product_id),
        quantity integer NOT NULL,
        refunded numeric(10,2) NOT NULL
    )
    """,
]

FIXTURES = {
    "categories": [
        {"category_id": 1, "name": "Electronics", "parent_id": None},
        {"category_id": 2, "name": "Audio", "parent_id": 1},
        {"category_id": 3, "name": "Computers", "parent_id": 1},
        {"category_id": 4, "name": "Headphones", "parent_id": 2},
        {"category_id": 5, "name": "Laptops", "parent_id": 3},
        {"category_id": 6, "name": "Home", "parent_id": None},
        {"category_id": 7, "name": "Kitchen", "parent_id": 6},
    ],
    "products": [
        {"product_id": 1, "name": "Aria Headphones", "category_id": 4, "tags": ["wireless", "premium"],
         "attributes": {"color": "black", "warranty_months": 24}},
        {"product_id": 2, "name": "Basalt Headphones", "category_id": 4, "tags": ["wired"],
         "attributes": {"color": "grey", "warranty_months": 12}},
        {"product_id": 3, "name": "Cobalt Laptop", "category_id": 5, "tags": ["premium", "business"],
         "attributes": {"ram_gb": 32, "warranty_months": 36}},
        {"product_id": 4, "name": "Dune Laptop", "category_id": 5, "tags": ["business"],
         "attributes": {"ram_gb": 16, "warranty_months": 24}},
        {"product_id": 5, "name": "Ember Kettle", "category_id": 7, "tags": ["kitchen"],
         "attributes": {"capacity_l": 1.7, "warranty_months": 12}},
        {"product_id": 6, "name": "Fjord Speaker", "category_id": 2, "tags": ["wireless"],
         "attributes": {"color": "white", "warranty_months": 12}},
    ],
    "product_prices": [
        {"product_id": 1, "valid_from": "2024-01-01 00:00:00+00", "list_price": "199.00"},
        {"product_id": 1, "valid_from": "2024-03-01 00:00:00+00", "list_price": "179.00"},
        {"product_id": 2, "valid_from": "2024-01-01 00:00:00+00", "list_price": "89.00"},
        {"product_id": 3, "valid_from": "2024-01-01 00:00:00+00", "list_price": "1499.00"},
        {"product_id": 3, "valid_from": "2024-03-10 00:00:00+00", "list_price": "1399.00"},
        {"product_id": 4, "valid_from": "2024-02-01 00:00:00+00", "list_price": "999.00"},
        {"product_id": 5, "valid_from": "2024-01-15 00:00:00+00", "list_price": "59.00"},
        {"product_id": 6, "valid_from": "2024-01-01 00:00:00+00", "list_price": "129.00"},
    ],
    "orders": [
        {"order_id": 1, "customer_id": 10, "placed_at": "2024-02-28 09:00:00+00", "status": "completed",
         "shipping": {"country": "DE", "carrier": "DHL", "express": False}, "promo_codes": []},
        {"order_id": 2, "customer_id": 10, "placed_at": "2024-03-04 23:30:00+00", "status": "completed",
         "shipping": {"country": "JP", "carrier": "Sagawa", "express": True}, "promo_codes": ["SPRING"]},
        {"order_id": 3, "customer_id": 11, "placed_at": "2024-03-05 14:00:00+00", "status": "completed",
         "shipping": {"country": "DE", "carrier": "DHL", "express": True}, "promo_codes": ["SPRING", "VIP"]},
        {"order_id": 4, "customer_id": 12, "placed_at": "2024-03-10 08:00:00+00", "status": "completed",
         "shipping": {"country": "DE", "carrier": "UPS", "express": False}, "promo_codes": []},
        {"order_id": 5, "customer_id": 11, "placed_at": "2024-03-12 16:00:00+00", "status": "completed",
         "shipping": {"country": "JP", "carrier": "Sagawa", "express": False}, "promo_codes": ["SPRING"]},
        {"order_id": 6, "customer_id": 13, "placed_at": "2024-03-29 22:00:00+00", "status": "completed",
         "shipping": {"country": "DE", "carrier": "DHL", "express": False}, "promo_codes": []},
        {"order_id": 7, "customer_id": 12, "placed_at": "2024-04-02 10:00:00+00", "status": "completed",
         "shipping": {"country": "DE", "carrier": "UPS", "express": True}, "promo_codes": []},
    ],
    "order_items": [
        {"order_id": 1, "product_id": 1, "quantity": 1, "unit_price": "189.00"},
        {"order_id": 1, "product_id": 2, "quantity": 2, "unit_price": "79.00"},
        {"order_id": 2, "product_id": 3, "quantity": 1, "unit_price": "1499.00"},
        {"order_id": 2, "product_id": 5, "quantity": 2, "unit_price": "49.00"},
        {"order_id": 3, "product_id": 1, "quantity": 2, "unit_price": "179.00"},
        {"order_id": 3, "product_id": 3, "quantity": 1, "unit_price": "1449.00"},
        {"order_id": 4, "product_id": 3, "quantity": 2, "unit_price": "1399.00"},
        {"order_id": 4, "product_id": 4, "quantity": 1, "unit_price": "949.00"},
        {"order_id": 5, "product_id": 1, "quantity": 1, "unit_price": "179.00"},
        {"order_id": 5, "product_id": 6, "quantity": 3, "unit_price": "119.00"},
        {"order_id": 6, "product_id": 2, "quantity": 4, "unit_price": "85.00"},
        {"order_id": 7, "product_id": 5, "quantity": 1, "unit_price": "55.00"},
        {"order_id": 7, "product_id": 6, "quantity": 2, "unit_price": "119.00"},
    ],
    "returns": [
        {"return_id": 1, "order_id": 1, "product_id": 2, "quantity": 1, "refunded": "79.00"},
        {"return_id": 2, "order_id": 2, "product_id": 5, "quantity": 2, "refunded": "98.00"},
        {"return_id": 3, "order_id": 3, "product_id": 1, "quantity": 1, "refunded": "179.00"},
        {"return_id": 4, "order_id": 4, "product_id": 3, "quantity": 2, "refunded": "2798.00"},
        {"return_id": 5, "order_id": 4, "product_id": 4, "quantity": 1, "refunded": "581.00"},
        {"return_id": 7, "order_id": 3, "product_id": 1, "quantity": 1, "refunded": "179.00"},
        {"return_id": 6, "order_id": 6, "product_id": 2, "quantity": 4, "refunded": "340.00"},
    ],
}

QUESTIONS = [
    {
        "skill": "grain_fanout_antijoin",
        "difficulty": "challenging",
        "question": (
            "For every product that appears on at least one order, return the product id, the number of distinct "
            "orders that contain it and the number of units returned, with each order counted once per product even "
            "when the product has several returns."
        ),
        "sql": """
            SELECT p.product_id,
                   (SELECT COUNT(DISTINCT oi.order_id) FROM order_items AS oi WHERE oi.product_id = p.product_id) AS orders,
                   (SELECT COALESCE(SUM(r.quantity), 0) FROM returns AS r WHERE r.product_id = p.product_id) AS returned_units
            FROM products AS p
            WHERE EXISTS (SELECT 1 FROM order_items AS oi WHERE oi.product_id = p.product_id)
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison, row order is not part of the answer; one row per product.",
    },
    {
        "skill": "topn_ties",
        "difficulty": "challenging",
        "question": (
            "Which three products have the highest net revenue, where net revenue is the charged amount of all order "
            "lines (quantity times unit price) minus every refund on those products? If products tie, the smaller "
            "product_id comes first. Return product_id and net_revenue, highest first."
        ),
        "sql": """
            WITH charged AS (
                SELECT oi.product_id, SUM(oi.quantity * oi.unit_price) AS amount
                FROM order_items AS oi
                GROUP BY oi.product_id
            ), refunded AS (
                SELECT r.product_id, SUM(r.refunded) AS amount
                FROM returns AS r
                GROUP BY r.product_id
            )
            SELECT p.product_id,
                   COALESCE(charged.amount, 0) - COALESCE(refunded.amount, 0) AS net_revenue
            FROM products AS p
            LEFT JOIN charged ON charged.product_id = p.product_id
            LEFT JOIN refunded ON refunded.product_id = p.product_id
            ORDER BY net_revenue DESC, p.product_id
            LIMIT 3
        """,
        "comparison": "ordered",
        "tie_policy": "Net revenue descending; ties broken by the smaller product_id (ORDER BY net_revenue DESC, product_id).",
    },
    {
        "skill": "asof_state",
        "difficulty": "challenging",
        "question": (
            "For every order, what is the total value of its lines at the list price that was in force when the order "
            "was placed (quantity times list price, rounded to two decimals)? Return order_id and list_value."
        ),
        "sql": """
            SELECT o.order_id,
                   ROUND(SUM(oi.quantity * pp.list_price), 2) AS list_value
            FROM orders AS o
            JOIN order_items AS oi ON oi.order_id = o.order_id
            JOIN LATERAL (
                SELECT price.list_price
                FROM product_prices AS price
                WHERE price.product_id = oi.product_id
                  AND price.valid_from <= o.placed_at
                ORDER BY price.valid_from DESC
                LIMIT 1
            ) AS pp ON true
            GROUP BY o.order_id
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the list price in force is unique because (product_id, valid_from) is unique.",
    },
    {
        "skill": "periods_timezones",
        "difficulty": "challenging",
        "question": (
            "For each Monday-based calendar week in Tokyo local time (Asia/Tokyo) that starts in March 2024, return "
            "the week start date, the number of orders and the charged total of those orders (quantity times unit "
            "price)."
        ),
        "sql": """
            SELECT date_trunc('week', o.placed_at AT TIME ZONE 'Asia/Tokyo')::date AS week_start,
                   COUNT(DISTINCT o.order_id) AS orders,
                   SUM(oi.quantity * oi.unit_price) AS charged_total
            FROM orders AS o
            JOIN order_items AS oi ON oi.order_id = o.order_id
            WHERE (o.placed_at AT TIME ZONE 'Asia/Tokyo') >= TIMESTAMPTZ '2024-03-01 00:00:00+09'
              AND (o.placed_at AT TIME ZONE 'Asia/Tokyo') <  TIMESTAMPTZ '2024-04-01 00:00:00+09'
            GROUP BY week_start
            ORDER BY week_start
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; the grouping key is the Tokyo week start.",
    },
    {
        "skill": "window_frames_cohorts",
        "difficulty": "challenging",
        "question": (
            "For every customer who placed at least two orders, return the customer id and the number of days between "
            "their first and their second order (second minus first, as an integer)."
        ),
        "sql": """
            WITH ranked AS (
                SELECT o.customer_id,
                       o.placed_at,
                       ROW_NUMBER() OVER (PARTITION BY o.customer_id ORDER BY o.placed_at) AS position
                FROM orders AS o
            )
            SELECT first_order.customer_id,
                   (second_order.placed_at::date - first_order.placed_at::date) AS days_between
            FROM ranked AS first_order
            JOIN ranked AS second_order
              ON second_order.customer_id = first_order.customer_id
             AND second_order.position = 2
            WHERE first_order.position = 1
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison; ROW_NUMBER over placed_at gives a unique first and second order per customer.",
    },
    {
        "skill": "jsonb_arrays",
        "difficulty": "moderate",
        "question": (
            "Which orders ship to Germany with express delivery and carry the promo code SPRING? Return order_id, "
            "the carrier from the shipping attributes and how many promo codes the order has."
        ),
        "sql": """
            SELECT o.order_id,
                   o.shipping ->> 'carrier' AS carrier,
                   cardinality(o.promo_codes) AS promo_count
            FROM orders AS o
            WHERE o.shipping ->> 'country' = 'DE'
              AND (o.shipping ->> 'express')::boolean
              AND o.promo_codes @> ARRAY['SPRING']
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: bag comparison, row order is not part of the answer.",
    },
    {
        "skill": "compositional",
        "difficulty": "challenging",
        "question": (
            "Among the orders placed in March 2024 in UTC, what share had every ordered unit returned? Return one "
            "row with the share between 0 and 1, rounded to four decimals."
        ),
        "sql": """
            WITH ordered AS (
                SELECT oi.order_id, SUM(oi.quantity) AS units
                FROM order_items AS oi
                JOIN orders AS o ON o.order_id = oi.order_id
                WHERE o.placed_at >= TIMESTAMPTZ '2024-03-01 00:00:00+00'
                  AND o.placed_at <  TIMESTAMPTZ '2024-04-01 00:00:00+00'
                GROUP BY oi.order_id
            ), returned AS (
                SELECT r.order_id, SUM(r.quantity) AS units
                FROM returns AS r
                GROUP BY r.order_id
            )
            SELECT ROUND(
                       COUNT(*) FILTER (WHERE COALESCE(returned.units, 0) >= ordered.units)::numeric
                       / NULLIF(COUNT(*), 0),
                       4
                   ) AS fully_returned_share
            FROM ordered
            LEFT JOIN returned ON returned.order_id = ordered.order_id
        """,
        "comparison": "bag",
        "tie_policy": "Not applicable: the result is a single row.",
    },
    {
        "skill": "hierarchies",
        "difficulty": "challenging",
        "question": (
            "Starting at the category 'Electronics', list that category and every category below it with the depth "
            "relative to Electronics (Electronics = 0) and the number of products assigned directly to each category. "
            "Order by depth, then category_id."
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
                   COUNT(p.product_id) AS products
            FROM subtree AS s
            LEFT JOIN products AS p ON p.category_id = s.category_id
            GROUP BY s.category_id, s.name, s.depth
            ORDER BY s.depth, s.category_id
        """,
        "comparison": "ordered",
        "tie_policy": "Depth ascending, then category_id ascending; both keys together are unique in the result.",
    },
]

DROPPED = []

NOTES = [
    "Order 4 is fully returned (2 laptops plus 1 laptop), order 2 is partly returned, order 1 is partly returned.",
    "Products 1 and 4 both net 368.00, so the top-3 tie-break (smaller product_id) is exercised at the cutoff.",
    "The 2024-03-10 price change for product 3 separates an as-of valuation from a current-price valuation.",
    "Orders placed at 23:30 UTC and around local Tokyo midnight exercise the week boundary.",
]
