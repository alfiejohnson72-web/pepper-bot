"""
Database for the whole platform (SQLite).

One file, kept on a Railway volume (DB_PATH=/data/shop.db) so it survives restarts.
The existing `leads` table is kept as it was. New tables are added by numbered
migrations, which only ever add things, so existing data is never deleted.

Money is stored in pence (whole numbers) to avoid rounding errors.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from guard import product_problems

BASE = Path(__file__).resolve().parent


def db_path() -> Path:
    # Old setups used leads.db. New setups should set DB_PATH=/data/shop.db on Railway.
    return Path(os.getenv("DB_PATH", str(BASE / "leads.db")))


def now() -> int:
    return int(time.time())


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=15, isolation_level=None)  # we manage transactions ourselves
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 15000")
    return con


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """A database transaction: everything inside succeeds together or not at all."""
    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


@contextmanager
def ro() -> Iterator[sqlite3.Connection]:
    con = connect()
    try:
        yield con
    finally:
        con.close()


# ---------------------------------------------------------------- migrations
MIGRATIONS: list[str] = [
    # 1: the original leads table (unchanged, so old data keeps working)
    """
    CREATE TABLE IF NOT EXISTS leads (
        user_id INTEGER PRIMARY KEY,
        username TEXT, first_name TEXT,
        source TEXT,
        first_seen INTEGER, last_seen INTEGER,
        messages INTEGER DEFAULT 0,
        last_product TEXT,
        checkout_clicks INTEGER DEFAULT 0,
        wanted_human INTEGER DEFAULT 0
    );
    """,
    # 2: the shop
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,               -- Telegram user id
        username TEXT, first_name TEXT,
        language TEXT NOT NULL DEFAULT 'en',
        referral_code TEXT NOT NULL UNIQUE,
        referred_by INTEGER REFERENCES users(id),
        blocked INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        last_seen INTEGER NOT NULL
    );
    CREATE TABLE categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        sort INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
        size TEXT NOT NULL DEFAULT '',
        price_pence INTEGER NOT NULL CHECK (price_pence > 0),
        summary TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        ingredients TEXT NOT NULL DEFAULT '',
        how_to_use TEXT NOT NULL DEFAULT '',
        image_path TEXT,
        image_file_id TEXT,
        stock INTEGER,                        -- NULL = not tracked
        active INTEGER NOT NULL DEFAULT 1,
        deleted INTEGER NOT NULL DEFAULT 0,   -- removed products are hidden, never erased (old orders point at them)
        sort INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE INDEX products_category ON products(category_id, active, deleted);
    CREATE TABLE basket_items (
        user_id INTEGER NOT NULL REFERENCES users(id),
        product_id INTEGER NOT NULL REFERENCES products(id),
        qty INTEGER NOT NULL CHECK (qty > 0),
        added_at INTEGER NOT NULL,
        PRIMARY KEY (user_id, product_id)
    );
    CREATE TABLE orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ref TEXT NOT NULL UNIQUE,
        user_id INTEGER NOT NULL REFERENCES users(id),
        status TEXT NOT NULL DEFAULT 'awaiting_payment',
        payment_status TEXT NOT NULL DEFAULT 'unpaid',
        subtotal_pence INTEGER NOT NULL,
        delivery_pence INTEGER NOT NULL DEFAULT 0,
        total_pence INTEGER NOT NULL,
        currency TEXT NOT NULL DEFAULT 'gbp',
        basket_hash TEXT NOT NULL,
        customer_name TEXT, customer_email TEXT,
        shipping_address TEXT,
        tracking_number TEXT, tracking_url TEXT,
        admin_notes TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        paid_at INTEGER
    );
    CREATE INDEX orders_user ON orders(user_id, created_at);
    CREATE INDEX orders_status ON orders(status, created_at);
    CREATE TABLE order_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL REFERENCES orders(id),
        product_id INTEGER REFERENCES products(id),
        name TEXT NOT NULL,
        unit_pence INTEGER NOT NULL,
        qty INTEGER NOT NULL CHECK (qty > 0)
    );
    CREATE INDEX order_items_order ON order_items(order_id);
    CREATE TABLE payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL REFERENCES orders(id),
        provider TEXT NOT NULL,
        provider_ref TEXT NOT NULL,           -- e.g. Stripe checkout session id
        provider_payment_id TEXT,             -- e.g. Stripe payment intent id
        checkout_url TEXT,
        status TEXT NOT NULL DEFAULT 'pending', -- pending, paid, failed, expired, refunded, partially_refunded
        amount_pence INTEGER NOT NULL,
        refunded_pence INTEGER NOT NULL DEFAULT 0,
        expires_at INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        UNIQUE (provider, provider_ref)
    );
    CREATE INDEX payments_order ON payments(order_id);
    CREATE TABLE payment_events (
        provider TEXT NOT NULL,
        event_id TEXT NOT NULL,
        type TEXT NOT NULL,
        received_at INTEGER NOT NULL,
        PRIMARY KEY (provider, event_id)
    );
    CREATE TABLE order_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL REFERENCES orders(id),
        kind TEXT NOT NULL,
        detail TEXT,
        actor TEXT,
        created_at INTEGER NOT NULL
    );
    CREATE INDEX order_events_order ON order_events(order_id);
    CREATE TABLE referrals (
        referred_id INTEGER PRIMARY KEY REFERENCES users(id),
        referrer_id INTEGER NOT NULL REFERENCES users(id),
        created_at INTEGER NOT NULL,
        first_paid_order_id INTEGER REFERENCES orders(id)
    );
    CREATE INDEX referrals_referrer ON referrals(referrer_id);
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        order_id INTEGER REFERENCES orders(id),
        status TEXT NOT NULL DEFAULT 'open',  -- open, answered, closed
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE INDEX tickets_status ON tickets(status, updated_at);
    CREATE TABLE ticket_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL REFERENCES tickets(id),
        sender TEXT NOT NULL,                 -- customer, admin, bot
        admin_id INTEGER,
        body TEXT NOT NULL,
        created_at INTEGER NOT NULL
    );
    CREATE INDEX ticket_messages_ticket ON ticket_messages(ticket_id);
    CREATE TABLE admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'staff',   -- owner, manager, staff
        active INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL,
        last_login INTEGER
    );
    CREATE TABLE notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dedupe_key TEXT NOT NULL UNIQUE,      -- the same notification is never queued twice
        chat_id INTEGER NOT NULL,
        body TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', -- pending, sent, failed
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at INTEGER NOT NULL,
        sent_at INTEGER
    );
    CREATE INDEX notifications_pending ON notifications(status, id);
    CREATE TABLE announcements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        body TEXT NOT NULL,
        admin_id INTEGER,
        recipients INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    );
    """,
]


def migrate() -> None:
    con = connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = con.execute("SELECT MAX(version) FROM schema_version").fetchone()
        current = row[0] or 0
        for i, sql in enumerate(MIGRATIONS, start=1):
            if i <= current:
                continue
            con.execute("BEGIN IMMEDIATE")
            try:
                for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
                    con.execute(stmt)
                con.execute("INSERT INTO schema_version (version) VALUES (?)", (i,))
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
    finally:
        con.close()


# ---------------------------------------------------------------- settings
DEFAULT_SETTINGS: dict[str, str] = {
    "brand": "AB Labs",
    "bot_name": "Pepper",
    "welcome": "hey welcome to AB Labs 👋\n\neveryday skincare that actually gets used. moisturiser and daily spf, thats it\n\npick an option below 👇",
    "about": "AB Labs is a small UK skincare brand run by A J and Bert. We keep it simple: a good moisturiser and daily SPF.",
    "faq": "",            # filled from faq.md on first run
    "terms": (
        "Terms of sale (summary)\n\n"
        "• UK delivery only.\n"
        "• Prices are in GBP and include VAT where it applies.\n"
        "• Payment is taken on a secure Stripe page. We never ask for card details in Telegram.\n"
        "• Unopened products can be returned within 14 days of delivery for a full refund.\n"
        "• Cosmetics for use on the skin only. Patch test first and stop if irritation happens.\n"
        "• Full terms: ask support and we'll send them."
    ),
    "support_intro": "type ur message below and the team will get back to u here, usually within a few hours",
    "support_hours": "We reply within 24 hours, usually much faster.",
    "delivery_pence": "395",
    "free_delivery_over_pence": "5000",
    "referral_text": "share ur link with mates. when they place their first order u both get thanked by the team 🙏",
    "payments_enabled": "1",
    "notify_admin_new_order": "1",
    "notify_admin_new_ticket": "1",
    "notify_customer_status": "1",
    "ai_chat_enabled": "1",
    "max_qty_per_item": "10",
}


def seed_settings() -> None:
    faq_file = BASE / "faq.md"
    with tx() as con:
        for k, v in DEFAULT_SETTINGS.items():
            if k == "faq" and faq_file.exists():
                v = faq_file.read_text(encoding="utf-8")
            con.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))


def get_setting(key: str, default: str | None = None) -> str:
    with ro() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is not None:
        return row["value"]
    return default if default is not None else DEFAULT_SETTINGS.get(key, "")


def get_int(key: str) -> int:
    try:
        return int(get_setting(key))
    except ValueError:
        return int(DEFAULT_SETTINGS.get(key, "0") or 0)


def get_flag(key: str) -> bool:
    return get_setting(key) == "1"


def set_setting(key: str, value: str) -> None:
    with tx() as con:
        con.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def all_settings() -> dict[str, str]:
    with ro() as con:
        return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM settings")}


# ---------------------------------------------------------------- catalogue
def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:50] or "item"


def seed_catalogue() -> None:
    """First run only: copy catalogue.json into the database. Never overwrites."""
    with ro() as con:
        if con.execute("SELECT COUNT(*) FROM products").fetchone()[0]:
            return
    path = BASE / "catalogue.json"
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    cats: dict[str, int] = {}
    with tx() as con:
        for i, c in enumerate(data.get("categories", [])):
            if product_problems(c):
                continue
            cur = con.execute("INSERT OR IGNORE INTO categories (name, sort) VALUES (?, ?)", (c, i))
            cats[c] = cur.lastrowid or con.execute("SELECT id FROM categories WHERE name=?", (c,)).fetchone()[0]
        for i, p in enumerate(data.get("products", [])):
            ingredients = ", ".join(p.get("key_ingredients", []))
            if product_problems(p.get("name"), p.get("summary"), p.get("description"), ingredients,
                                p.get("how_to_use"), p.get("size"), p.get("best_for")):
                continue  # never import a product the dashboard would refuse
            cat_name = p.get("category")
            if cat_name and cat_name not in cats and not product_problems(cat_name):
                cur = con.execute("INSERT OR IGNORE INTO categories (name, sort) VALUES (?, ?)", (cat_name, len(cats)))
                cats[cat_name] = con.execute("SELECT id FROM categories WHERE name=?", (cat_name,)).fetchone()[0]
            con.execute(
                """INSERT INTO products (slug, name, category_id, size, price_pence, summary, description,
                                         ingredients, how_to_use, sort, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (p.get("id") or slugify(p["name"]), p["name"], cats.get(cat_name), p.get("size", ""),
                 round(float(p["price"]) * 100), p.get("summary", ""), p.get("description", p.get("best_for", "")),
                 ingredients, p.get("how_to_use", ""), i, now(), now()),
            )


def product_is_safe(p: sqlite3.Row | dict) -> bool:
    return not product_problems(p["name"], p["summary"], p["description"], p["ingredients"],
                                p["how_to_use"], p["size"])


def list_categories(active_only: bool = True) -> list[sqlite3.Row]:
    with ro() as con:
        q = "SELECT * FROM categories" + (" WHERE active=1" if active_only else "") + " ORDER BY sort, name"
        return con.execute(q).fetchall()


def list_products(category_id: int | None = None, *, shop_only: bool = True) -> list[sqlite3.Row]:
    q = "SELECT p.*, c.name AS category FROM products p LEFT JOIN categories c ON c.id = p.category_id WHERE p.deleted=0"
    args: list = []
    if shop_only:
        q += " AND p.active=1 AND (c.id IS NULL OR c.active=1)"
    if category_id is not None:
        q += " AND p.category_id=?"
        args.append(category_id)
    q += " ORDER BY p.sort, p.name"
    with ro() as con:
        rows = con.execute(q, args).fetchall()
    # Belt and braces: even if someone edits the database by hand, the bot never shows a blocked product.
    return [r for r in rows if not shop_only or product_is_safe(r)]


def get_product(pid: int, *, shop_only: bool = True) -> sqlite3.Row | None:
    with ro() as con:
        p = con.execute("SELECT p.*, c.name AS category FROM products p LEFT JOIN categories c "
                        "ON c.id=p.category_id WHERE p.id=? AND p.deleted=0", (pid,)).fetchone()
    if p is None:
        return None
    if shop_only and (not p["active"] or not product_is_safe(p)):
        return None
    return p


def in_stock(p: sqlite3.Row, qty: int = 1) -> bool:
    return p["stock"] is None or p["stock"] >= qty


class ProductRejected(ValueError):
    pass


def save_product(fields: dict, pid: int | None = None) -> int:
    """Create or update a product after validation. Raises ProductRejected with a friendly message."""
    name = (fields.get("name") or "").strip()
    if not name:
        raise ProductRejected("Give the product a name.")
    try:
        price_pence = round(float(str(fields.get("price", "")).replace("£", "").strip()) * 100)
    except ValueError:
        raise ProductRejected("Price must be a number, like 24 or 24.50.")
    if price_pence <= 0 or price_pence > 100000:
        raise ProductRejected("Price must be between £0.01 and £1,000.")
    stock_raw = str(fields.get("stock", "") or "").strip()
    stock = None
    if stock_raw:
        if not stock_raw.isdigit():
            raise ProductRejected("Stock must be a whole number, or left blank if you don't track it.")
        stock = int(stock_raw)
    text_fields = {k: (fields.get(k) or "").strip() for k in
                   ("size", "summary", "description", "ingredients", "how_to_use")}
    for k, limit in (("summary", 300), ("description", 2000), ("ingredients", 2000), ("how_to_use", 1000), ("size", 40)):
        if len(text_fields[k]) > limit:
            raise ProductRejected(f"{k.replace('_', ' ').capitalize()} is too long (max {limit} characters).")
    found = product_problems(name, *text_fields.values())
    if found:
        raise ProductRejected(f"PRODUCT_REFUSED:{', '.join(found)}")
    category_id = fields.get("category_id") or None
    category_id = int(category_id) if category_id else None
    active = 1 if fields.get("active") in (True, "1", "on", 1) else 0
    with tx() as con:
        if category_id is not None and not con.execute("SELECT 1 FROM categories WHERE id=?", (category_id,)).fetchone():
            raise ProductRejected("That category doesn't exist.")
        if pid is None:
            slug = base = slugify(name)
            n = 2
            while con.execute("SELECT 1 FROM products WHERE slug=?", (slug,)).fetchone():
                slug, n = f"{base}-{n}", n + 1
            cur = con.execute(
                """INSERT INTO products (slug, name, category_id, size, price_pence, summary, description,
                       ingredients, how_to_use, stock, active, sort, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           (SELECT COALESCE(MAX(sort), 0) + 1 FROM products), ?, ?)""",
                (slug, name, category_id, text_fields["size"], price_pence, text_fields["summary"],
                 text_fields["description"], text_fields["ingredients"], text_fields["how_to_use"],
                 stock, active, now(), now()))
            return cur.lastrowid
        con.execute(
            """UPDATE products SET name=?, category_id=?, size=?, price_pence=?, summary=?, description=?,
                   ingredients=?, how_to_use=?, stock=?, active=?, updated_at=? WHERE id=? AND deleted=0""",
            (name, category_id, text_fields["size"], price_pence, text_fields["summary"],
             text_fields["description"], text_fields["ingredients"], text_fields["how_to_use"],
             stock, active, now(), pid))
        return pid


def set_product_image(pid: int, path: str | None) -> None:
    with tx() as con:
        con.execute("UPDATE products SET image_path=?, image_file_id=NULL, updated_at=? WHERE id=?", (path, now(), pid))


def remember_image_file_id(pid: int, file_id: str) -> None:
    with tx() as con:
        con.execute("UPDATE products SET image_file_id=? WHERE id=?", (file_id, pid))


def remove_product(pid: int) -> None:
    """Hide a product everywhere. Its row stays so past orders still make sense."""
    with tx() as con:
        con.execute("UPDATE products SET deleted=1, active=0, updated_at=? WHERE id=?", (now(), pid))
        con.execute("DELETE FROM basket_items WHERE product_id=?", (pid,))


def save_category(name: str, cid: int | None = None, active: bool = True, sort: int | None = None) -> int:
    name = (name or "").strip()
    if not name or len(name) > 40:
        raise ProductRejected("Category names must be 1 to 40 characters.")
    found = product_problems(name)
    if found:
        raise ProductRejected(f"PRODUCT_REFUSED:{', '.join(found)}")
    with tx() as con:
        clash = con.execute("SELECT id FROM categories WHERE name=? COLLATE NOCASE", (name,)).fetchone()
        if clash and clash["id"] != cid:
            raise ProductRejected("There's already a category with that name.")
        if cid is None:
            return con.execute("INSERT INTO categories (name, sort, active) VALUES (?, "
                               "(SELECT COALESCE(MAX(sort),0)+1 FROM categories), ?)", (name, int(active))).lastrowid
        con.execute("UPDATE categories SET name=?, active=?, sort=COALESCE(?, sort) WHERE id=?",
                    (name, int(active), sort, cid))
        return cid


def delete_category(cid: int) -> None:
    with tx() as con:
        con.execute("UPDATE products SET category_id=NULL WHERE category_id=?", (cid,))
        con.execute("DELETE FROM categories WHERE id=?", (cid,))


# ---------------------------------------------------------------- customers
def _new_code(con: sqlite3.Connection) -> str:
    while True:
        code = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8].upper()
        if len(code) == 8 and not con.execute("SELECT 1 FROM users WHERE referral_code=?", (code,)).fetchone():
            return code


def upsert_user(tg_user, referral_code: str | None = None) -> tuple[sqlite3.Row, bool]:
    """Register a Telegram user automatically. Returns (user, is_new)."""
    with tx() as con:
        row = con.execute("SELECT * FROM users WHERE id=?", (tg_user.id,)).fetchone()
        if row:
            con.execute("UPDATE users SET username=?, first_name=?, last_seen=? WHERE id=?",
                        (tg_user.username, tg_user.first_name, now(), tg_user.id))
            return con.execute("SELECT * FROM users WHERE id=?", (tg_user.id,)).fetchone(), False
        referrer = None
        if referral_code:
            ref = con.execute("SELECT id FROM users WHERE referral_code=? AND blocked=0",
                              (referral_code.upper(),)).fetchone()
            if ref and ref["id"] != tg_user.id:
                referrer = ref["id"]
        con.execute(
            "INSERT INTO users (id, username, first_name, referral_code, referred_by, created_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tg_user.id, tg_user.username, tg_user.first_name, _new_code(con), referrer, now(), now()))
        if referrer:
            con.execute("INSERT OR IGNORE INTO referrals (referred_id, referrer_id, created_at) VALUES (?, ?, ?)",
                        (tg_user.id, referrer, now()))
        return con.execute("SELECT * FROM users WHERE id=?", (tg_user.id,)).fetchone(), True


def get_user(uid: int) -> sqlite3.Row | None:
    with ro() as con:
        return con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def set_language(uid: int, lang: str) -> None:
    with tx() as con:
        con.execute("UPDATE users SET language=? WHERE id=?", (lang, uid))


def set_blocked(uid: int, blocked: bool) -> None:
    with tx() as con:
        con.execute("UPDATE users SET blocked=? WHERE id=?", (int(blocked), uid))


def referral_stats(uid: int) -> tuple[int, int]:
    with ro() as con:
        joined = con.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,)).fetchone()[0]
        ordered = con.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND first_paid_order_id IS NOT NULL",
                              (uid,)).fetchone()[0]
    return joined, ordered


# ---------------------------------------------------------------- basket
class BasketError(ValueError):
    pass


def basket(uid: int) -> list[sqlite3.Row]:
    with ro() as con:
        rows = con.execute(
            """SELECT b.product_id, b.qty, p.name, p.price_pence, p.size, p.stock, p.active, p.deleted,
                      p.summary, p.description, p.ingredients, p.how_to_use
               FROM basket_items b JOIN products p ON p.id=b.product_id
               WHERE b.user_id=? ORDER BY b.added_at""", (uid,)).fetchall()
    return [r for r in rows if r["active"] and not r["deleted"] and product_is_safe(r)]


def basket_count(uid: int) -> int:
    return sum(r["qty"] for r in basket(uid))


def set_basket_qty(uid: int, pid: int, qty: int) -> int:
    """Set an item's quantity (0 removes it). Returns the new quantity."""
    max_qty = get_int("max_qty_per_item") or 10
    qty = max(0, min(qty, max_qty))
    p = get_product(pid)
    if qty and p is None:
        raise BasketError("sorry that product isnt available anymore")
    if qty and not in_stock(p, qty):
        raise BasketError(f"sorry only {p['stock']} left of that one" if p["stock"] else "sorry thats sold out rn")
    with tx() as con:
        if qty == 0:
            con.execute("DELETE FROM basket_items WHERE user_id=? AND product_id=?", (uid, pid))
        else:
            con.execute("INSERT INTO basket_items (user_id, product_id, qty, added_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(user_id, product_id) DO UPDATE SET qty=excluded.qty", (uid, pid, qty, now()))
    return qty


def add_to_basket(uid: int, pid: int, qty: int = 1) -> int:
    current = next((r["qty"] for r in basket(uid) if r["product_id"] == pid), 0)
    return set_basket_qty(uid, pid, current + qty)


def clear_basket(uid: int) -> None:
    with tx() as con:
        con.execute("DELETE FROM basket_items WHERE user_id=?", (uid,))


def delivery_for(subtotal: int) -> int:
    free_over = get_int("free_delivery_over_pence")
    if free_over and subtotal >= free_over:
        return 0
    return get_int("delivery_pence")


def basket_totals(items: list[sqlite3.Row]) -> tuple[int, int, int]:
    subtotal = sum(r["price_pence"] * r["qty"] for r in items)
    delivery = delivery_for(subtotal) if items else 0
    return subtotal, delivery, subtotal + delivery


# ---------------------------------------------------------------- orders
ORDER_STATUSES = ["awaiting_payment", "paid", "processing", "shipped", "delivered", "cancelled", "refunded"]
STATUS_LABELS = {
    "awaiting_payment": "Awaiting payment",
    "paid": "Paid",
    "processing": "Being packed",
    "shipped": "Shipped",
    "delivered": "Delivered",
    "cancelled": "Cancelled",
    "refunded": "Refunded",
}
PAYMENT_LABELS = {
    "unpaid": "Not paid", "pending": "Waiting for payment", "paid": "Paid", "failed": "Failed",
    "expired": "Expired", "refunded": "Refunded", "partially_refunded": "Partly refunded",
}


def _new_ref(con: sqlite3.Connection) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I mix-ups
    while True:
        ref = "AB-" + "".join(secrets.choice(alphabet) for _ in range(6))
        if not con.execute("SELECT 1 FROM orders WHERE ref=?", (ref,)).fetchone():
            return ref


def _basket_hash(items: list[sqlite3.Row]) -> str:
    key = ";".join(f"{r['product_id']}x{r['qty']}@{r['price_pence']}" for r in sorted(items, key=lambda r: r["product_id"]))
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def log_order_event(con: sqlite3.Connection, order_id: int, kind: str, detail: str = "", actor: str = "system") -> None:
    con.execute("INSERT INTO order_events (order_id, kind, detail, actor, created_at) VALUES (?, ?, ?, ?, ?)",
                (order_id, kind, detail, actor, now()))


def create_order_from_basket(uid: int) -> tuple[sqlite3.Row, bool]:
    """
    Turn the basket into an order awaiting payment. Returns (order, reused).

    If the customer already has an unpaid order for exactly the same basket (for example
    they tapped checkout twice, or came back after closing the payment page), that order
    is reused instead of creating a duplicate.
    """
    items = basket(uid)
    if not items:
        raise BasketError("ur basket is empty")
    for r in items:
        if not in_stock(r, r["qty"]):
            raise BasketError(f"sorry only {r['stock']} left of {r['name']}, change the amount in ur basket")
    subtotal, delivery, total = basket_totals(items)
    bh = _basket_hash(items)
    with tx() as con:
        existing = con.execute(
            "SELECT * FROM orders WHERE user_id=? AND basket_hash=? AND status='awaiting_payment' "
            "AND created_at > ? ORDER BY id DESC LIMIT 1", (uid, bh, now() - 23 * 3600)).fetchone()
        if existing and existing["total_pence"] == total:
            return existing, True
        # Any older unpaid orders for a different basket are superseded.
        for old in con.execute("SELECT id FROM orders WHERE user_id=? AND status='awaiting_payment'", (uid,)).fetchall():
            con.execute("UPDATE orders SET status='cancelled', updated_at=? WHERE id=?", (now(), old["id"]))
            log_order_event(con, old["id"], "cancelled", "Replaced by a newer checkout")
        ref = _new_ref(con)
        cur = con.execute(
            """INSERT INTO orders (ref, user_id, subtotal_pence, delivery_pence, total_pence, basket_hash,
                                   created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ref, uid, subtotal, delivery, total, bh, now(), now()))
        oid = cur.lastrowid
        for r in items:
            con.execute("INSERT INTO order_items (order_id, product_id, name, unit_pence, qty) VALUES (?, ?, ?, ?, ?)",
                        (oid, r["product_id"], r["name"] + (f" ({r['size']})" if r["size"] else ""),
                         r["price_pence"], r["qty"]))
        log_order_event(con, oid, "created", f"Total £{total / 100:.2f}", f"customer:{uid}")
        return con.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone(), False


def get_order(oid: int) -> sqlite3.Row | None:
    with ro() as con:
        return con.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()


def get_order_by_ref(ref: str) -> sqlite3.Row | None:
    with ro() as con:
        return con.execute("SELECT * FROM orders WHERE ref=?", (ref.strip().upper(),)).fetchone()


def order_items(oid: int) -> list[sqlite3.Row]:
    with ro() as con:
        return con.execute("SELECT * FROM order_items WHERE order_id=? ORDER BY id", (oid,)).fetchall()


def order_events(oid: int) -> list[sqlite3.Row]:
    with ro() as con:
        return con.execute("SELECT * FROM order_events WHERE order_id=? ORDER BY id", (oid,)).fetchall()


def user_orders(uid: int, limit: int = 10, *, include_unpaid: bool = False) -> list[sqlite3.Row]:
    q = "SELECT * FROM orders WHERE user_id=?"
    if not include_unpaid:
        q += " AND status NOT IN ('awaiting_payment','cancelled') "
    q += " ORDER BY id DESC LIMIT ?"
    with ro() as con:
        return con.execute(q, (uid, limit)).fetchall()


def update_order_status(oid: int, status: str, actor: str, *, tracking_number: str | None = None,
                        tracking_url: str | None = None) -> tuple[sqlite3.Row, bool]:
    """Change fulfilment status. Returns (order, changed)."""
    if status not in ORDER_STATUSES:
        raise ValueError("unknown status")
    with tx() as con:
        o = con.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if o is None:
            raise ValueError("order not found")
        if status in ("processing", "shipped", "delivered") and o["payment_status"] != "paid":
            raise ValueError("This order hasn't been paid, so it can't be packed or shipped.")
        if status == "paid":
            raise ValueError("Orders are only marked paid when Stripe confirms the payment.")
        tn = tracking_number.strip() if tracking_number else o["tracking_number"]
        tu = tracking_url.strip() if tracking_url else o["tracking_url"]
        if tu and not tu.startswith("https://"):
            raise ValueError("Tracking links must start with https://")
        changed = status != o["status"] or tn != o["tracking_number"] or tu != o["tracking_url"]
        con.execute("UPDATE orders SET status=?, tracking_number=?, tracking_url=?, updated_at=? WHERE id=?",
                    (status, tn, tu, now(), oid))
        if changed:
            log_order_event(con, oid, "status", f"{o['status']} → {status}" + (f" (tracking {tn})" if tn else ""), actor)
        return con.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone(), changed


def set_order_notes(oid: int, notes: str) -> None:
    with tx() as con:
        con.execute("UPDATE orders SET admin_notes=?, updated_at=? WHERE id=?", (notes[:2000], now(), oid))


# ---------------------------------------------------------------- payments
def record_payment_session(oid: int, provider: str, ref: str, url: str, amount: int, expires_at: int | None) -> None:
    with tx() as con:
        con.execute(
            """INSERT INTO payments (order_id, provider, provider_ref, checkout_url, status, amount_pence,
                                     expires_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)
               ON CONFLICT(provider, provider_ref) DO NOTHING""",
            (oid, provider, ref, url, amount, expires_at, now(), now()))
        con.execute("UPDATE orders SET payment_status='pending', updated_at=? WHERE id=? AND payment_status='unpaid'",
                    (now(), oid))


def open_payment_session(oid: int) -> sqlite3.Row | None:
    """A still-valid checkout page for this order, so we can resend the same link."""
    with ro() as con:
        return con.execute(
            "SELECT * FROM payments WHERE order_id=? AND status='pending' AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY id DESC LIMIT 1", (oid, now() + 120)).fetchone()


def latest_payment(oid: int) -> sqlite3.Row | None:
    with ro() as con:
        return con.execute("SELECT * FROM payments WHERE order_id=? ORDER BY id DESC LIMIT 1", (oid,)).fetchone()


def order_payments(oid: int) -> list[sqlite3.Row]:
    with ro() as con:
        return con.execute("SELECT * FROM payments WHERE order_id=? ORDER BY id", (oid,)).fetchall()


def seen_event(provider: str, event_id: str, typ: str) -> bool:
    """Record a webhook event. Returns True if it had already been processed."""
    with tx() as con:
        cur = con.execute("INSERT OR IGNORE INTO payment_events (provider, event_id, type, received_at) VALUES (?, ?, ?, ?)",
                          (provider, event_id, typ, now()))
        return cur.rowcount == 0


def mark_paid(provider: str, provider_ref: str, *, amount_pence: int, payment_id: str | None,
              customer_name: str | None, customer_email: str | None, address: str | None) -> tuple[sqlite3.Row | None, bool]:
    """
    Mark an order paid. Only call this with details confirmed by the payment provider.
    Returns (order, newly_paid). Safe to call more than once.
    """
    with tx() as con:
        pay = con.execute("SELECT * FROM payments WHERE provider=? AND provider_ref=?", (provider, provider_ref)).fetchone()
        if pay is None:
            return None, False
        o = con.execute("SELECT * FROM orders WHERE id=?", (pay["order_id"],)).fetchone()
        if amount_pence != o["total_pence"]:
            log_order_event(con, o["id"], "payment_mismatch",
                            f"Provider says £{amount_pence / 100:.2f}, order total £{o['total_pence'] / 100:.2f}. Not marked paid.")
            return o, False
        if pay["status"] == "paid":
            return o, False
        con.execute("UPDATE payments SET status='paid', provider_payment_id=?, updated_at=? WHERE id=?",
                    (payment_id, now(), pay["id"]))
        newly = o["payment_status"] != "paid"
        new_status = "paid" if o["status"] in ("awaiting_payment", "cancelled") else o["status"]
        con.execute(
            """UPDATE orders SET payment_status='paid', status=?, paid_at=COALESCE(paid_at, ?),
                   customer_name=COALESCE(?, customer_name), customer_email=COALESCE(?, customer_email),
                   shipping_address=COALESCE(?, shipping_address), updated_at=? WHERE id=?""",
            (new_status, now(), customer_name, customer_email, address, now(), o["id"]))
        if newly:
            log_order_event(con, o["id"], "paid", f"{provider} confirmed £{amount_pence / 100:.2f}")
            # Take stock and empty the basket only once the money is confirmed.
            for it in con.execute("SELECT * FROM order_items WHERE order_id=?", (o["id"],)).fetchall():
                con.execute("UPDATE products SET stock = MAX(stock - ?, 0) WHERE id=? AND stock IS NOT NULL",
                            (it["qty"], it["product_id"]))
            con.execute("DELETE FROM basket_items WHERE user_id=?", (o["user_id"],))
            con.execute("UPDATE referrals SET first_paid_order_id=? WHERE referred_id=? AND first_paid_order_id IS NULL",
                        (o["id"], o["user_id"]))
        return con.execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone(), newly


def mark_payment_failed(provider: str, provider_ref: str, status: str) -> sqlite3.Row | None:
    """status: failed or expired. Never downgrades a paid payment."""
    with tx() as con:
        pay = con.execute("SELECT * FROM payments WHERE provider=? AND provider_ref=?", (provider, provider_ref)).fetchone()
        if pay is None or pay["status"] != "pending":
            return None
        con.execute("UPDATE payments SET status=?, updated_at=? WHERE id=?", (status, now(), pay["id"]))
        o = con.execute("SELECT * FROM orders WHERE id=?", (pay["order_id"],)).fetchone()
        if o["payment_status"] in ("pending", "unpaid"):
            con.execute("UPDATE orders SET payment_status=?, updated_at=? WHERE id=?", (status, now(), o["id"]))
            log_order_event(con, o["id"], f"payment_{status}", provider_ref)
        return con.execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone()


def mark_refund(provider: str, payment_id: str, refunded_pence: int) -> sqlite3.Row | None:
    with tx() as con:
        pay = con.execute("SELECT * FROM payments WHERE provider=? AND provider_payment_id=?",
                          (provider, payment_id)).fetchone()
        if pay is None:
            return None
        full = refunded_pence >= pay["amount_pence"]
        pstatus = "refunded" if full else "partially_refunded"
        con.execute("UPDATE payments SET status=?, refunded_pence=?, updated_at=? WHERE id=?",
                    (pstatus, refunded_pence, now(), pay["id"]))
        o = con.execute("SELECT * FROM orders WHERE id=?", (pay["order_id"],)).fetchone()
        con.execute("UPDATE orders SET payment_status=?, status=?, updated_at=? WHERE id=?",
                    (pstatus, "refunded" if full else o["status"], now(), o["id"]))
        if o["payment_status"] != pstatus or refunded_pence != pay["refunded_pence"]:
            log_order_event(con, o["id"], "refund", f"£{refunded_pence / 100:.2f} refunded in total")
        return con.execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone()


# ---------------------------------------------------------------- support tickets
def open_ticket(uid: int) -> sqlite3.Row | None:
    with ro() as con:
        return con.execute("SELECT * FROM tickets WHERE user_id=? AND status!='closed' ORDER BY id DESC LIMIT 1",
                           (uid,)).fetchone()


def add_ticket_message(uid: int, body: str, sender: str = "customer", admin_id: int | None = None,
                       ticket_id: int | None = None) -> tuple[int, bool]:
    """Add a message to the customer's open ticket (or start one). Returns (ticket_id, is_new)."""
    body = body.strip()[:4000]
    with tx() as con:
        is_new = False
        if ticket_id is None:
            t = con.execute("SELECT id FROM tickets WHERE user_id=? AND status!='closed' ORDER BY id DESC LIMIT 1",
                            (uid,)).fetchone()
            if t is None:
                ticket_id = con.execute("INSERT INTO tickets (user_id, created_at, updated_at) VALUES (?, ?, ?)",
                                        (uid, now(), now())).lastrowid
                is_new = True
            else:
                ticket_id = t["id"]
        con.execute("INSERT INTO ticket_messages (ticket_id, sender, admin_id, body, created_at) VALUES (?, ?, ?, ?, ?)",
                    (ticket_id, sender, admin_id, body, now()))
        status = "answered" if sender == "admin" else "open"
        con.execute("UPDATE tickets SET status=?, updated_at=? WHERE id=?", (status, now(), ticket_id))
        return ticket_id, is_new


def close_ticket(tid: int) -> None:
    with tx() as con:
        con.execute("UPDATE tickets SET status='closed', updated_at=? WHERE id=?", (now(), tid))


def get_ticket(tid: int) -> sqlite3.Row | None:
    with ro() as con:
        return con.execute("SELECT t.*, u.username, u.first_name FROM tickets t JOIN users u ON u.id=t.user_id "
                           "WHERE t.id=?", (tid,)).fetchone()


def ticket_messages(tid: int) -> list[sqlite3.Row]:
    with ro() as con:
        return con.execute("SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY id", (tid,)).fetchall()


# ---------------------------------------------------------------- notifications (outbox)
def queue_notification(dedupe_key: str, chat_id: int, body: str) -> bool:
    """Queue a Telegram message. The same dedupe_key is only ever queued once. Returns True if queued."""
    if not chat_id:
        return False
    with tx() as con:
        cur = con.execute("INSERT OR IGNORE INTO notifications (dedupe_key, chat_id, body, created_at) VALUES (?, ?, ?, ?)",
                          (dedupe_key, chat_id, body[:4000], now()))
        return cur.rowcount == 1


def pending_notifications(limit: int = 25) -> list[sqlite3.Row]:
    with ro() as con:
        return con.execute("SELECT * FROM notifications WHERE status='pending' ORDER BY id LIMIT ?", (limit,)).fetchall()


def notification_result(nid: int, ok: bool, error: str | None = None, *, give_up: bool = False) -> None:
    with tx() as con:
        if ok:
            con.execute("UPDATE notifications SET status='sent', sent_at=?, attempts=attempts+1 WHERE id=?", (now(), nid))
        else:
            con.execute("UPDATE notifications SET attempts=attempts+1, last_error=?, "
                        "status=CASE WHEN ? OR attempts+1 >= 5 THEN 'failed' ELSE 'pending' END WHERE id=?",
                        ((error or "")[:500], int(give_up), nid))


# ---------------------------------------------------------------- admins
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2 ** 14, r=8, p=1)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


ROLES = ("owner", "manager", "staff")


def create_admin(username: str, password: str, role: str = "staff") -> int:
    username = username.strip()
    if role not in ROLES:
        raise ValueError("Unknown role")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise ValueError("Usernames are 3 to 32 letters, numbers, dots, dashes or underscores.")
    if len(password) < 10:
        raise ValueError("Passwords need at least 10 characters.")
    with tx() as con:
        if con.execute("SELECT 1 FROM admins WHERE username=?", (username,)).fetchone():
            raise ValueError("That username is taken.")
        return con.execute("INSERT INTO admins (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                           (username, hash_password(password), role, now())).lastrowid


def bootstrap_owner() -> None:
    """If there are no admins yet, create the owner from ADMIN_USERNAME / ADMIN_PASSWORD."""
    with ro() as con:
        if con.execute("SELECT COUNT(*) FROM admins").fetchone()[0]:
            return
    user, pw = os.getenv("ADMIN_USERNAME", "").strip(), os.getenv("ADMIN_PASSWORD", "")
    if user and pw:
        try:
            create_admin(user, pw, "owner")
        except ValueError as e:  # don't stop the shop starting; the login page explains what's missing
            import logging
            logging.getLogger("store").error("Couldn't create the first admin from ADMIN_USERNAME/ADMIN_PASSWORD: %s", e)


def authenticate(username: str, password: str) -> sqlite3.Row | None:
    with ro() as con:
        a = con.execute("SELECT * FROM admins WHERE username=? AND active=1", (username.strip(),)).fetchone()
    if a and check_password(password, a["password_hash"]):
        with tx() as con:
            con.execute("UPDATE admins SET last_login=? WHERE id=?", (now(), a["id"]))
        return a
    if a is None:
        check_password(password, hash_password("timing-equaliser"))
    return None


def get_admin(aid: int) -> sqlite3.Row | None:
    with ro() as con:
        return con.execute("SELECT * FROM admins WHERE id=? AND active=1", (aid,)).fetchone()


def init() -> None:
    migrate()
    seed_settings()
    seed_catalogue()
    bootstrap_owner()
