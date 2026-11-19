"""
Offline tests: no Telegram, Claude or Stripe calls. Run with:  python -m pytest -q

They use a throwaway database, fake Telegram updates and a fake Stripe, so they're
safe to run anywhere and never touch the live bot or real payments.
"""
import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

TMP = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(TMP, "shop.db")
os.environ["IMAGES_DIR"] = os.path.join(TMP, "images")
os.environ["ADMIN_CHAT_ID"] = "-100999"
os.environ["ADMIN_USERNAME"] = "ajadmin"
os.environ["ADMIN_PASSWORD"] = "correct-horse-battery"
os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_testsecret"
os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["COOKIE_SECURE"] = "0"

import bot  # noqa: E402
import guard  # noqa: E402
import notify  # noqa: E402
import payments  # noqa: E402
import store  # noqa: E402
from telegram.constants import ChatType  # noqa: E402

PRODUCTS = {p["slug"]: p for p in store.list_products()}
MOIST = PRODUCTS["daily-moisturiser"]["id"]
SPF50 = PRODUCTS["spf-50"]["id"]


# ---------------------------------------------------------------- fakes
class FakeMsg:
    def __init__(self, text="", photo=None):
        self.text = text
        self.photo = photo
        self.reply_to_message = None
        self.reply_text = AsyncMock()
        self.edit_text = AsyncMock()
        self.edit_caption = AsyncMock()
        self.delete = AsyncMock()
        self.reply_photo = AsyncMock()


def fake_update(uid=42, text="hi", data=None, chat_type=ChatType.PRIVATE, chat_id=None, username="jane"):
    user = SimpleNamespace(id=uid, is_bot=False, username=username, first_name="Jane", full_name="Jane D")
    msg = FakeMsg(text)
    tg_bot = SimpleNamespace(username="ABLabsBot", id=7, send_chat_action=AsyncMock(), send_message=AsyncMock(),
                             send_photo=AsyncMock())
    q = None
    if data is not None:
        q = SimpleNamespace(data=data, from_user=user, message=msg, answer=AsyncMock())
    update = SimpleNamespace(
        effective_message=msg, message=msg, effective_user=user, callback_query=q,
        effective_chat=SimpleNamespace(id=chat_id or uid, type=chat_type),
        get_bot=lambda: tg_bot)
    ctx = SimpleNamespace(user_data={}, args=[], bot=tg_bot)
    return update, ctx, msg


def press(data, uid=42, ctx=None):
    update, c, msg = fake_update(uid=uid, data=data)
    if ctx is not None:
        c.user_data = ctx.user_data
    asyncio.run(bot.on_button(update, c))
    return update, c, msg


def last_screen(update, msg):
    """Text and keyboard of whatever the bot last showed."""
    for m in (msg.edit_text, update.get_bot().send_message, msg.reply_text):
        if m.call_args:
            args, kw = m.call_args
            text = args[1] if m is update.get_bot().send_message else args[0]
            return text, kw.get("reply_markup")
    raise AssertionError("nothing shown")


def buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


def labels(kb):
    return [b.text for b in buttons(kb)]


def datas(kb):
    return [b.callback_data for b in buttons(kb)]


class FakeStripe:
    name = "stripe"
    test_mode = True

    def __init__(self):
        self.sessions = {}
        self.created = 0
        self.refunds = []
        self.webhook_secret = "whsec_testsecret"

    def create_checkout(self, order, items, bot_username):
        self.created += 1
        ref = f"cs_test_{order['ref']}_{self.created}"
        self.sessions[ref] = payments.Verified(ref=ref, status="pending", amount_pence=order["total_pence"])
        return payments.CheckoutSession(ref=ref, url=f"https://checkout.stripe.com/c/pay/{ref}", expires_at=int(time.time()) + 3600)

    def verify(self, ref):
        return self.sessions[ref]

    def pay(self, ref, amount=None):
        v = self.sessions[ref]
        self.sessions[ref] = payments.Verified(ref=ref, status="paid", amount_pence=amount or v.amount_pence,
                                               payment_id=f"pi_{ref}", customer_name="Jane Doe",
                                               customer_email="jane@example.com", address="Jane Doe, 1 High St, London, SW1A 1AA, GB")

    def parse_webhook(self, payload, sig):
        return payments.StripeProvider.parse_webhook(self, payload, sig)

    def refund(self, pi, amount, idem):
        self.refunds.append((pi, amount))

    @property
    def stripe(self):
        import stripe
        return stripe

    @property
    def client(self):
        import stripe
        return stripe.StripeClient("sk_test_fake")


@pytest.fixture
def fake_stripe():
    fs = FakeStripe()
    with patch.object(payments, "provider", lambda: fs if store.get_flag("payments_enabled") else None):
        yield fs


def reset_user(uid):
    with store.tx() as con:
        con.execute("DELETE FROM basket_items WHERE user_id=?", (uid,))


# ---------------------------------------------------------------- guardrails
def test_chat_blocked_words():
    for t in ["do you sell reta?", "Retatrutide price", "BPC-157 dose", "got any sema",
              "is it injectable", "how many mcg", "bac water", "research peptides", "weight loss"]:
        assert guard.is_blocked(t), t
    for t in ["I have fine lines", "retail price?", "semantic question", "is the spf 50 greasy",
              "is it vegan", "puffy eyes", "pinch of salt"]:
        assert not guard.is_blocked(t), t


def test_product_blocklist_catches_code_names_and_formats():
    for t in ["RET", "BP15", "BP-157", "MT", "MT2", "Melanotan II", "Nasal spray", "10mg", "5 mg per vial",
              "Research use only", "pen", "Tirzepatide", "lose weight fast", "Capsules", "GHRP-6", "not for human use"]:
        assert guard.product_problems(t), t
    for t in ["Daily Moisturiser", "Daily SPF 50", "SPF 30", "50 ml", "Hyaluronic acid, glycerin, niacinamide",
              "Apply 15 minutes before sun exposure", "Sun protection", "Moisturisers", "Floral scent", "Open pores"]:
        assert not guard.product_problems(t), t


def test_save_product_refuses_blocked_products():
    for fields in [{"name": "RET", "price": "40"}, {"name": "Recovery blend", "price": "30", "description": "BPC-157 5mg vial"},
                   {"name": "Tan booster", "price": "25", "summary": "MT2 nasal spray"}]:
        with pytest.raises(store.ProductRejected) as e:
            store.save_product(fields)
        assert str(e.value).startswith("PRODUCT_REFUSED:")
    with pytest.raises(store.ProductRejected):
        store.save_category("Research peptides")


def test_hand_edited_blocked_product_never_shown():
    with store.tx() as con:
        con.execute("INSERT INTO products (slug, name, price_pence, created_at, updated_at) VALUES ('x','Reta',100,0,0)")
        pid = con.execute("SELECT id FROM products WHERE slug='x'").fetchone()[0]
    assert all(p["slug"] != "x" for p in store.list_products())
    assert store.get_product(pid) is None
    with pytest.raises(store.BasketError):
        store.add_to_basket(1, pid)
    with store.tx() as con:
        con.execute("DELETE FROM products WHERE id=?", (pid,))


# ---------------------------------------------------------------- catalogue and database
def test_catalogue_seeded_with_skincare_range():
    names = sorted(p["name"] for p in store.list_products())
    assert names == ["Daily Moisturiser", "Daily SPF 30", "Daily SPF 50"]
    assert {c["name"] for c in store.list_categories()} == {"Moisturisers", "Sun protection"}


def test_migrations_are_repeatable_and_keep_data():
    store.init()
    store.init()
    assert len(store.list_products()) == 3


def test_old_leads_database_upgrades_without_losing_rows(tmp_path, monkeypatch):
    import sqlite3
    old = tmp_path / "leads.db"
    con = sqlite3.connect(old)
    con.execute("""CREATE TABLE leads (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, source TEXT,
        first_seen INTEGER, last_seen INTEGER, messages INTEGER DEFAULT 0, last_product TEXT,
        checkout_clicks INTEGER DEFAULT 0, wanted_human INTEGER DEFAULT 0)""")
    con.execute("INSERT INTO leads (user_id, username) VALUES (5, 'oldlead')")
    con.commit()
    con.close()
    monkeypatch.setenv("DB_PATH", str(old))
    store.init()
    with store.ro() as c:
        assert c.execute("SELECT username FROM leads WHERE user_id=5").fetchone()[0] == "oldlead"
        assert c.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 3


# ---------------------------------------------------------------- Telegram customer interface
def test_start_registers_customer_and_shows_menu():
    update, ctx, msg = fake_update(uid=1001)
    asyncio.run(bot.start(update, ctx))
    text, kb = msg.reply_text.call_args.args[0], msg.reply_text.call_args.kwargs["reply_markup"]
    assert "welcome to AB Labs" in text
    for need in ["all", "cats", "bk", "orders", "track", "faq", "terms", "ref", "lang", "about", "support", "choose"]:
        assert need in datas(kb), need
    assert store.get_user(1001) is not None


def test_every_main_menu_button_does_something():
    update, ctx, msg = fake_update(uid=1002)
    asyncio.run(bot.start(update, ctx))
    kb = msg.reply_text.call_args.kwargs["reply_markup"]
    for data in datas(kb):
        u, c, m = press(data, uid=1002)
        text, k = last_screen(u, m)
        assert text and "something went wrong" not in text, data


def test_browse_categories_product_and_quantity():
    u, c, m = press("cats", uid=1003)
    text, kb = last_screen(u, m)
    assert {"Moisturisers", "Sun protection"} <= set(labels(kb))
    sun = next(b.callback_data for b in buttons(kb) if b.text == "Sun protection")
    u, c, m = press(sun, uid=1003)
    text, kb = last_screen(u, m)
    assert any("SPF 50" in l for l in labels(kb)) and not any("Moisturiser" in l for l in labels(kb))
    u, c, m = press(f"p:{SPF50}:3", uid=1003)
    text, kb = last_screen(u, m)
    assert "Daily SPF 50" in text and "£20" in text
    assert "Qty 3" in labels(kb) and f"add:{SPF50}:3" in datas(kb)
    assert any(d.startswith("cat:") for d in datas(kb)) and "menu" in datas(kb)


def test_basket_add_change_remove_and_totals():
    uid = 1004
    press(f"add:{MOIST}:2", uid=uid)
    press(f"add:{SPF50}:1", uid=uid)
    u, c, m = press("bk", uid=uid)
    text, kb = last_screen(u, m)
    assert "2 × Daily Moisturiser  £44" in text and "1 × Daily SPF 50  £20" in text
    assert "Delivery  free" in text and "Total  £64" in text  # free delivery over £50
    press(f"bk-:{MOIST}", uid=uid)
    u, c, m = press(f"bkx:{SPF50}", uid=uid)
    text, kb = last_screen(u, m)
    assert "1 × Daily Moisturiser  £22" in text and "SPF" not in text
    assert "Delivery  £3.95" in text and "Total  £25.95" in text
    press("bkclear", uid=uid)
    assert store.basket(uid) == []


def test_quantity_limits_and_stock():
    uid = 1005
    with pytest.raises(store.BasketError):
        store.set_basket_qty(uid, 99999, 1)
    store.upsert_user(SimpleNamespace(id=uid, username="q", first_name="Q"))
    assert store.set_basket_qty(uid, MOIST, 500) == 10  # capped by max_qty_per_item
    store.save_product({**dict(store.get_product(SPF50)), "price": "20", "stock": "2", "active": "1"}, SPF50)
    with pytest.raises(store.BasketError):
        store.set_basket_qty(uid, SPF50, 3)
    store.save_product({**dict(store.get_product(SPF50)), "price": "20", "stock": "", "active": "1"}, SPF50)
    reset_user(uid)


def test_checkout_without_payments_tells_customer_and_admin():
    uid = 1006
    press(f"add:{MOIST}:1", uid=uid)
    with patch.object(payments, "provider", lambda: None):
        u, c, m = press("co", uid=uid)
    text, kb = last_screen(u, m)
    assert "isnt switched on" in text
    alert = u.get_bot().send_message.call_args_list[0].args
    assert alert[0] == -100999 and "payments aren't set up" in alert[1]


def test_checkout_creates_one_order_and_reuses_it(fake_stripe):
    uid = 1007
    press(f"add:{MOIST}:1", uid=uid)
    u, c, m = press("co", uid=uid)
    text, kb = last_screen(u, m)
    pay_btn = buttons(kb)[0]
    assert pay_btn.url.startswith("https://checkout.stripe.com/")
    ref = text.split()[1]
    # Tapping checkout again (or coming back later) reuses the same order and payment page.
    u, c, m = press("co", uid=uid)
    assert buttons(last_screen(u, m)[1])[0].url == pay_btn.url
    with store.ro() as con:
        assert con.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (uid,)).fetchone()[0] == 1
    assert fake_stripe.created == 1
    # Changing the basket replaces the unpaid order instead of leaving two open.
    press(f"add:{SPF50}:1", uid=uid)
    press("co", uid=uid)
    with store.ro() as con:
        rows = con.execute("SELECT status FROM orders WHERE user_id=? ORDER BY id", (uid,)).fetchall()
    assert [r[0] for r in rows] == ["cancelled", "awaiting_payment"]
    assert store.get_order_by_ref(ref)["status"] == "cancelled"


def test_ive_paid_button_checks_with_stripe_not_the_customer(fake_stripe):
    uid = 1008
    press(f"add:{MOIST}:3", uid=uid)
    u, c, m = press("co", uid=uid)
    ref = last_screen(u, m)[0].split()[1]
    u, c, m = press(f"paid:{ref}", uid=uid)
    assert "hasnt come through yet" in last_screen(u, m)[0]
    assert store.get_order_by_ref(ref)["payment_status"] == "pending"
    session = store.latest_payment(store.get_order_by_ref(ref)["id"])["provider_ref"]
    fake_stripe.pay(session)
    u, c, m = press(f"paid:{ref}", uid=uid)
    assert "payment confirmed" in last_screen(u, m)[0]
    o = store.get_order_by_ref(ref)
    assert o["payment_status"] == "paid" and o["status"] == "paid"
    assert o["shipping_address"].startswith("Jane Doe, 1 High St")
    assert store.basket(uid) == []
    with store.ro() as con:
        keys = [r[0] for r in con.execute("SELECT dedupe_key FROM notifications WHERE dedupe_key LIKE ?", (f"order:{o['id']}:%",))]
    assert sorted(keys) == [f"order:{o['id']}:paid:admin", f"order:{o['id']}:paid:customer"]
    press(f"paid:{ref}", uid=uid)  # pressing again doesn't notify twice
    with store.ro() as con:
        assert con.execute("SELECT COUNT(*) FROM notifications WHERE dedupe_key LIKE ?", (f"order:{o['id']}:%",)).fetchone()[0] == 2


def test_wrong_amount_is_never_marked_paid(fake_stripe):
    uid = 1009
    press(f"add:{MOIST}:1", uid=uid)
    u, c, m = press("co", uid=uid)
    ref = last_screen(u, m)[0].split()[1]
    session = store.latest_payment(store.get_order_by_ref(ref)["id"])["provider_ref"]
    fake_stripe.pay(session, amount=100)
    press(f"paid:{ref}", uid=uid)
    assert store.get_order_by_ref(ref)["payment_status"] != "paid"


def test_customers_only_see_their_own_orders(fake_stripe):
    press(f"add:{MOIST}:1", uid=1010)
    u, c, m = press("co", uid=1010)
    ref = last_screen(u, m)[0].split()[1]
    u, c, m = press(f"o:{ref}", uid=1011)
    assert "couldnt find that order" in last_screen(u, m)[0]
    u, c, m = press(f"paid:{ref}", uid=1011)
    assert "couldnt find that order" in last_screen(u, m)[0]
    update, ctx, msg = fake_update(uid=1011, text=ref)
    ctx.user_data["awaiting"] = "track"
    asyncio.run(bot.on_text(update, ctx))
    assert "couldnt find that order" in msg.reply_text.call_args.args[0]


def test_my_orders_and_tracking(fake_stripe):
    uid = 1012
    press(f"add:{SPF50}:1", uid=uid)
    u, c, m = press("co", uid=uid)
    ref = last_screen(u, m)[0].split()[1]
    o = store.get_order_by_ref(ref)
    fake_stripe.pay(store.latest_payment(o["id"])["provider_ref"])
    press(f"paid:{ref}", uid=uid)
    store.update_order_status(o["id"], "shipped", "test", tracking_number="RM123GB",
                              tracking_url="https://www.royalmail.com/track-your-item#/tracking-results/RM123GB")
    u, c, m = press("orders", uid=uid)
    assert any(ref in l and "Shipped" in l for l in labels(last_screen(u, m)[1]))
    update, ctx, msg = fake_update(uid=uid, text=ref.lower())
    ctx.user_data["awaiting"] = "track"
    asyncio.run(bot.on_text(update, ctx))
    out = msg.reply_text.call_args
    assert "Status: Shipped" in out.args[0] and "RM123GB" in out.args[0]
    assert buttons(out.kwargs["reply_markup"])[0].url.startswith("https://www.royalmail.com/")


def test_support_ticket_from_telegram():
    uid = 1013
    u, c, m = press("support", uid=uid)
    assert c.user_data["awaiting"] == "support"
    update, ctx, msg = fake_update(uid=uid, text="my parcel hasnt arrived")
    ctx.user_data = c.user_data
    with patch.object(bot, "ask_ai", AsyncMock()) as ai:
        asyncio.run(bot.on_text(update, ctx))
        ai.assert_not_called()
    assert "sent to the team" in msg.reply_text.call_args.args[0]
    t = store.open_ticket(uid)
    assert t is not None and store.ticket_messages(t["id"])[0]["body"] == "my parcel hasnt arrived"
    with store.ro() as con:
        n = con.execute("SELECT body FROM notifications WHERE dedupe_key LIKE ?", (f"ticket:{t['id']}:%",)).fetchone()
    assert "New support ticket" in n[0]


def test_referral_link_and_tracking():
    u, c, m = press("ref", uid=1014)
    text = last_screen(u, m)[0]
    code = store.get_user(1014)["referral_code"]
    assert f"https://t.me/ABLabsBot?start=ref_{code}" in text
    update, ctx, msg = fake_update(uid=1015)
    ctx.args = [f"ref_{code}"]
    asyncio.run(bot.start(update, ctx))
    assert store.get_user(1015)["referred_by"] == 1014
    assert store.referral_stats(1014) == (1, 0)
    # Referring yourself or re-using a code later does nothing.
    update, ctx, msg = fake_update(uid=1014)
    ctx.args = [f"ref_{code}"]
    asyncio.run(bot.start(update, ctx))
    assert store.get_user(1014)["referred_by"] is None


def test_paused_customer_cannot_shop():
    uid = 1016
    store.upsert_user(SimpleNamespace(id=uid, username="p", first_name="P"))
    store.set_blocked(uid, True)
    u, c, m = press(f"add:{MOIST}:1", uid=uid)
    u.callback_query.answer.assert_awaited_with("ur account is paused", show_alert=True)
    assert store.basket(uid) == []
    store.set_blocked(uid, False)


def test_ai_reply_gets_add_to_basket_button():
    update, ctx, msg = fake_update(uid=1017, text="i have dry skin")
    with patch.object(bot, "ask_ai", AsyncMock(return_value="moisturiser deffo\n[[product:daily-moisturiser]]")):
        asyncio.run(bot.on_text(update, ctx))
    args, kwargs = msg.reply_text.call_args
    assert args[0] == "moisturiser deffo"
    assert datas(kwargs["reply_markup"])[0] == f"add:{MOIST}:1"


def test_blocked_never_reaches_ai():
    update, ctx, msg = fake_update(uid=1018, text="can I get retatrutide")
    ai = AsyncMock()
    with patch.object(bot, "ask_ai", ai):
        asyncio.run(bot.on_text(update, ctx))
    ai.assert_not_called()
    assert "only sell skincare" in msg.reply_text.call_args.args[0]


def test_group_ignores_untagged_and_links_to_dm():
    update, ctx, msg = fake_update(chat_type=ChatType.SUPERGROUP, text="anyone tried it?", chat_id=-100123)
    with patch.object(bot, "ask_ai", AsyncMock(return_value="x")) as ai:
        asyncio.run(bot.on_text(update, ctx))
        ai.assert_not_called()
    update, ctx, msg = fake_update(chat_type=ChatType.SUPERGROUP, text="@ABLabsBot best spf?", chat_id=-100123)
    with patch.object(bot, "ask_ai", AsyncMock(return_value="spf 50\n[[product:spf-50]]")):
        asyncio.run(bot.on_text(update, ctx))
    kb = msg.reply_text.call_args.kwargs["reply_markup"]
    assert buttons(kb)[0].url == f"https://t.me/ABLabsBot?start=p_{SPF50}"


def test_prompt_rules():
    p = bot.build_system_prompt()
    assert "never deny being an AI" in p and "Daily SPF 50" in p and "checkout.stripe.com" not in p


def test_app_builds():
    app = bot.build_app("123456:TEST-TOKEN")
    assert len(app.handlers[0]) == 13


# ---------------------------------------------------------------- notifications
def test_notifications_dedupe_and_outbox_sends_once():
    assert store.queue_notification("unit:1", 555, "hello")
    assert not store.queue_notification("unit:1", 555, "hello again")
    tg = SimpleNamespace(send_message=AsyncMock())
    stop = asyncio.Event()

    async def run():
        task = asyncio.create_task(notify.outbox_loop(tg, stop, interval=0.01))
        await asyncio.sleep(0.3)
        stop.set()
        await task
    asyncio.run(run())
    sent = [c.args for c in tg.send_message.call_args_list if c.args[0] == 555]
    assert sent == [(555, "hello")]
    with store.ro() as con:
        assert con.execute("SELECT status FROM notifications WHERE dedupe_key='unit:1'").fetchone()[0] == "sent"


# ---------------------------------------------------------------- admin dashboard
@pytest.fixture
def client(fake_stripe):
    from fastapi.testclient import TestClient
    import web
    with TestClient(web.create_app(bot=None)) as c:
        yield c


def csrf_of(client, path="/login"):
    import re
    html = client.get(path).text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def login(client, user="ajadmin", pw="correct-horse-battery"):
    tok = csrf_of(client)
    r = client.post("/login", data={"username": user, "password": pw, "csrf": tok}, follow_redirects=False)
    return r


def test_dashboard_requires_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert login(client, pw="wrong-password-123").headers["location"] == "/login"
    assert login(client).headers["location"] == "/"
    r = client.get("/")
    assert r.status_code == 200 and "Revenue" in r.text and "test mode" in r.text


def test_forms_need_csrf(client):
    login(client)
    r = client.post("/products/save", data={"name": "Cleanser", "price": "10"})
    assert r.status_code == 400


def test_admin_adds_edits_and_removes_product(client):
    login(client)
    tok = csrf_of(client, "/products/new")
    r = client.post("/products/save", data={"csrf": tok, "name": "Gentle Cleanser", "price": "14.50", "size": "150 ml",
                                            "summary": "Soft foaming cleanser", "active": "on"}, follow_redirects=False)
    assert r.status_code == 303
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    assert store.get_product(pid)["price_pence"] == 1450
    assert any(p["name"] == "Gentle Cleanser" for p in store.list_products())
    r = client.post("/products/save", data={"csrf": tok, "id": pid, "name": "Gentle Cleanser", "price": "15"}, follow_redirects=False)
    assert store.get_product(pid, shop_only=False)["price_pence"] == 1500
    assert store.get_product(pid) is None  # unticked "Show in the shop" hides it
    client.post(f"/products/{pid}/delete", data={"csrf": tok, "confirm": "yes"})
    assert store.get_product(pid, shop_only=False) is None


def test_admin_refuses_blocked_products(client):
    login(client)
    tok = csrf_of(client, "/products/new")
    for name, desc in [("RET", ""), ("BP15", ""), ("MT", ""), ("Recovery peptide", "BPC-157 10mg vial, research use only"),
                       ("Tanning", "melanotan nasal spray")]:
        r = client.post("/products/save", data={"csrf": tok, "name": name, "price": "30", "description": desc, "active": "on"})
        assert r.status_code == 200 and "AB Labs sells cosmetic skincare only" in r.text, name
    assert sorted(p["name"] for p in store.list_products() if p["name"] != "Gentle Cleanser") == \
        ["Daily Moisturiser", "Daily SPF 30", "Daily SPF 50"]


def test_image_upload(client):
    login(client)
    tok = csrf_of(client, f"/products/{MOIST}")
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    r = client.post(f"/products/{MOIST}/image", data={"csrf": tok}, files={"image": ("a.png", png, "image/png")})
    assert "Image uploaded" in r.text
    assert client.get(f"/media/product/{MOIST}").content == png
    r = client.post(f"/products/{MOIST}/image", data={"csrf": tok}, files={"image": ("a.exe", b"MZ....", "image/png")})
    assert "Upload a JPG, PNG or WebP" in r.text


def test_staff_permissions(client):
    store.create_admin("packer", "packer-password-1", "staff")
    login(client, "packer", "packer-password-1")
    assert client.get("/orders").status_code == 200
    assert client.get("/support").status_code == 200
    for path in ["/products/new", "/settings", "/admins", "/announce"]:
        assert client.get(path).status_code == 403, path


def stripe_sig(payload: bytes, secret="whsec_testsecret"):
    t = int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


def make_paid_order(fake_stripe, uid):
    press(f"add:{MOIST}:1", uid=uid)
    u, c, m = press("co", uid=uid)
    ref = last_screen(u, m)[0].split()[1]
    o = store.get_order_by_ref(ref)
    session = store.latest_payment(o["id"])["provider_ref"]
    return o, session


def test_stripe_webhook_marks_paid_once_and_rejects_fakes(client, fake_stripe):
    o, session = make_paid_order(fake_stripe, 1020)
    event = {"id": "evt_1", "object": "event", "type": "checkout.session.completed",
             "data": {"object": {"id": session, "object": "checkout.session"}}}
    payload = json.dumps(event).encode()
    # Forged webhook: rejected, order stays unpaid.
    r = client.post("/stripe/webhook", content=payload, headers={"stripe-signature": stripe_sig(payload, "whsec_wrong")})
    assert r.status_code == 400 and store.get_order(o["id"])["payment_status"] != "paid"
    # Real signature but Stripe says not paid yet: still unpaid.
    r = client.post("/stripe/webhook", content=payload, headers={"stripe-signature": stripe_sig(payload)})
    assert r.status_code == 200 and store.get_order(o["id"])["payment_status"] != "paid"
    with store.tx() as con:
        con.execute("DELETE FROM payment_events WHERE event_id='evt_1'")
    fake_stripe.pay(session)
    for _ in range(2):  # Stripe can deliver the same event twice
        r = client.post("/stripe/webhook", content=payload, headers={"stripe-signature": stripe_sig(payload)})
        assert r.status_code == 200
    assert store.get_order(o["id"])["payment_status"] == "paid"
    with store.ro() as con:
        assert con.execute("SELECT COUNT(*) FROM notifications WHERE dedupe_key LIKE ?", (f"order:{o['id']}:paid:%",)).fetchone()[0] == 2


def test_webhook_expired_and_refund(client, fake_stripe):
    o, session = make_paid_order(fake_stripe, 1021)
    ev = {"id": "evt_exp", "object": "event", "type": "checkout.session.expired",
          "data": {"object": {"id": session, "object": "checkout.session"}}}
    p = json.dumps(ev).encode()
    client.post("/stripe/webhook", content=p, headers={"stripe-signature": stripe_sig(p)})
    assert store.get_order(o["id"])["payment_status"] == "expired"
    o, session = make_paid_order(fake_stripe, 1022)
    fake_stripe.pay(session)
    payments.apply_verified(fake_stripe.verify(session))
    ev = {"id": "evt_ref", "object": "event", "type": "charge.refunded",
          "data": {"object": {"id": "ch_1", "object": "charge", "payment_intent": f"pi_{session}",
                              "amount_refunded": o["total_pence"]}}}
    p = json.dumps(ev).encode()
    client.post("/stripe/webhook", content=p, headers={"stripe-signature": stripe_sig(p)})
    o2 = store.get_order(o["id"])
    assert o2["payment_status"] == "refunded" and o2["status"] == "refunded"


def test_admin_order_flow_and_customer_notified(client, fake_stripe):
    login(client)
    o, session = make_paid_order(fake_stripe, 1023)
    tok = csrf_of(client, f"/orders/{o['id']}")
    r = client.post(f"/orders/{o['id']}/status", data={"csrf": tok, "status": "shipped"})
    assert "been paid, so it can" in r.text
    fake_stripe.pay(session)
    r = client.post(f"/orders/{o['id']}/refresh-payment", data={"csrf": tok})
    assert "Payment confirmed" in r.text
    r = client.post(f"/orders/{o['id']}/status", data={"csrf": tok, "status": "shipped", "tracking_number": "RM9",
                                                       "tracking_url": "https://www.royalmail.com/track-your-item"})
    assert "customer has been messaged" in r.text
    with store.ro() as con:
        body = con.execute("SELECT body FROM notifications WHERE dedupe_key=?", (f"order:{o['id']}:status:shipped:RM9",)).fetchone()[0]
    assert "on its way" in body and "RM9" in body
    r = client.post(f"/orders/{o['id']}/refund", data={"csrf": tok, "amount": "5", "confirm": "yes"})
    assert fake_stripe.refunds == [(f"pi_{session}", 500)]
    assert client.get(f"/orders?q={o['ref']}").text.count(o["ref"]) >= 1


def test_support_reply_from_dashboard(client):
    login(client)
    tid, _ = store.add_ticket_message(1013, "still waiting")
    tok = csrf_of(client, f"/support/{tid}")
    client.post(f"/support/{tid}/reply", data={"csrf": tok, "body": "sorry about that, sending a new one today"})
    with store.ro() as con:
        assert con.execute("SELECT COUNT(*) FROM notifications WHERE chat_id=1013 AND body LIKE '%new one today%'").fetchone()[0] == 1
    assert store.get_ticket(tid)["status"] == "answered"


def test_announcement_reaches_each_customer_once(client):
    login(client)
    tok = csrf_of(client, "/announce")
    r = client.post("/announce", data={"csrf": tok, "body": "new spf drop friday", "confirm": "yes"})
    assert "Announcement queued" in r.text
    with store.ro() as con:
        users = con.execute("SELECT COUNT(*) FROM users WHERE blocked=0").fetchone()[0]
        sent = con.execute("SELECT COUNT(*) FROM notifications WHERE body='new spf drop friday'").fetchone()[0]
    assert users == sent > 0
    r = client.post("/announce", data={"csrf": tok, "body": "reta back in stock", "confirm": "yes"})
    assert "skincare only" in r.text


def test_settings_and_admins(client):
    login(client)
    tok = csrf_of(client, "/settings")
    client.post("/settings", data={"csrf": tok, "section": "owner", "welcome": "hey 👋", "delivery": "4.50",
                                   "free_delivery_over": "40", "payments_enabled": "on", "max_qty_per_item": "5"})
    assert store.get_setting("welcome") == "hey 👋" and store.get_int("delivery_pence") == 450
    assert store.get_flag("payments_enabled") and not store.get_flag("ai_chat_enabled")
    page = client.get("/settings").text
    assert "sk_test_fake" not in page and "whsec_testsecret" not in page  # secrets are never shown
    client.post("/settings", data={"csrf": tok, "section": "owner", "delivery": "3.95", "free_delivery_over": "50",
                                   "payments_enabled": "on", "ai_chat_enabled": "on", "notify_admin_new_order": "on",
                                   "notify_admin_new_ticket": "on", "notify_customer_status": "on", "max_qty_per_item": "10"})
    r = client.post("/admins", data={"csrf": tok, "action": "create", "username": "bert", "password": "short", "role": "manager"})
    assert "at least 10" in r.text
    r = client.post("/admins", data={"csrf": tok, "action": "create", "username": "bert", "password": "bert-password-1", "role": "manager"})
    assert "Admin added" in r.text


def test_every_dashboard_page_loads(client, fake_stripe):
    login(client)
    o, _ = make_paid_order(fake_stripe, 1024)
    tid, _ = store.add_ticket_message(1024, "hi")
    for path in ["/", "/orders", f"/orders/{o['id']}", "/products", "/products/new", f"/products/{MOIST}", "/categories",
                 "/customers", "/customers/1024", "/support", f"/support/{tid}", "/announce", "/settings", "/admins",
                 "/account", "/healthz", "/orders?status=shipped", "/support?status=all"]:
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code)
    assert client.get("/orders/999999").status_code == 404
