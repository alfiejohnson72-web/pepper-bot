"""
Admin dashboard (website) and Stripe webhook.

Runs inside the same Railway service as the bot. Sign in with the owner account
created from ADMIN_USERNAME / ADMIN_PASSWORD the first time it starts.

Roles:
  staff    dashboard, orders (view and update status), customers (view), support
  manager  everything staff can, plus products, categories, refunds, blocking customers,
           announcements and shop text
  owner    everything, plus payment / notification settings and admin accounts
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import guard
import notify
import payments
import store

log = logging.getLogger("web")
BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
ROLE_RANK = {"staff": 1, "manager": 2, "owner": 3}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def images_dir() -> Path:
    d = Path(os.getenv("IMAGES_DIR", str(store.db_path().parent / "images")))
    d.mkdir(parents=True, exist_ok=True)
    return d


def money(pence: int | None) -> str:
    return f"£{(pence or 0) / 100:,.2f}"


def ts(t: int | None, fmt: str = "%d %b %Y %H:%M") -> str:
    return time.strftime(fmt, time.localtime(t)) if t else ""


templates.env.filters["money"] = money
templates.env.filters["ts"] = ts
templates.env.globals["STATUS_LABELS"] = store.STATUS_LABELS
templates.env.globals["PAYMENT_LABELS"] = store.PAYMENT_LABELS


class NeedLogin(Exception):
    pass


def refusal_message(e: Exception) -> str:
    msg = str(e)
    if msg.startswith("PRODUCT_REFUSED:"):
        return guard.PRODUCT_REFUSAL.format(found=msg.split(":", 1)[1])
    return msg


# ---------------------------------------------------------------- login throttling
_attempts: dict[str, deque] = defaultdict(deque)


def too_many_attempts(key: str, limit: int = 8, window: int = 900) -> bool:
    q = _attempts[key]
    cutoff = time.time() - window
    while q and q[0] < cutoff:
        q.popleft()
    return len(q) >= limit


def create_app(bot=None) -> FastAPI:
    app = FastAPI(title="AB Labs admin", docs_url=None, redoc_url=None, openapi_url=None)
    secret = os.getenv("SESSION_SECRET")
    if not secret:
        log.warning("SESSION_SECRET is not set: everyone gets signed out whenever the app restarts.")
        secret = secrets.token_urlsafe(32)
    app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="ablabs_admin", max_age=12 * 3600,
                       same_site="strict", https_only=os.getenv("COOKIE_SECURE", "1") == "1")
    app.state.bot = bot

    @app.exception_handler(NeedLogin)
    async def _need_login(request: Request, exc: NeedLogin):
        return RedirectResponse("/login", status_code=303)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "font-src 'self' data:; form-action 'self'; frame-ancestors 'none'")
        return resp

    # ------------------------------------------------------------ helpers
    def current_admin(request: Request):
        aid = request.session.get("admin_id")
        admin = store.get_admin(aid) if aid else None
        if admin is None:
            request.session.clear()
            raise NeedLogin()
        return admin

    def need(role: str):
        def dep(request: Request, admin=Depends(current_admin)):
            if ROLE_RANK[admin["role"]] < ROLE_RANK[role]:
                raise HTTPException(403, "Your account doesn't have permission for this page.")
            return admin
        return dep

    def csrf_token(request: Request) -> str:
        tok = request.session.get("csrf")
        if not tok:
            tok = secrets.token_urlsafe(24)
            request.session["csrf"] = tok
        return tok

    async def check_csrf(request: Request) -> None:
        form = await request.form()
        if not secrets.compare_digest(str(form.get("csrf", "")), str(request.session.get("csrf", "x"))):
            raise HTTPException(400, "This form expired. Go back, refresh the page and try again.")

    def render(request: Request, name: str, admin=None, status_code: int = 200, **ctx):
        flash = request.session.pop("flash", None)
        return templates.TemplateResponse(request, name, status_code=status_code, context={
            "admin": admin, "csrf": csrf_token(request), "flash": flash,
            "can": (lambda role: admin is not None and ROLE_RANK[admin["role"]] >= ROLE_RANK[role]),
            "brand": store.get_setting("brand"), **ctx})

    def flash(request: Request, text: str, kind: str = "ok") -> None:
        request.session["flash"] = {"text": text, "kind": kind}

    def back(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    # ------------------------------------------------------------ health
    @app.get("/healthz")
    async def healthz():
        with store.ro() as con:
            con.execute("SELECT 1").fetchone()
        return {"ok": True}

    # ------------------------------------------------------------ auth
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        with store.ro() as con:
            has_admins = con.execute("SELECT COUNT(*) FROM admins").fetchone()[0] > 0
        return render(request, "login.html", has_admins=has_admins)

    @app.post("/login")
    async def login(request: Request, username: str = Form(""), password: str = Form(""), _=Depends(check_csrf)):
        ip = request.client.host if request.client else "?"
        if too_many_attempts(ip) or too_many_attempts(f"u:{username.lower()}"):
            flash(request, "Too many attempts. Wait 15 minutes and try again.", "error")
            return back("/login")
        admin = await asyncio.to_thread(store.authenticate, username, password)
        if admin is None:
            _attempts[ip].append(time.time())
            _attempts[f"u:{username.lower()}"].append(time.time())
            flash(request, "Wrong username or password.", "error")
            return back("/login")
        request.session.clear()
        request.session["admin_id"] = admin["id"]
        return back("/")

    @app.post("/logout")
    async def logout(request: Request, _=Depends(check_csrf)):
        request.session.clear()
        return back("/login")

    # ------------------------------------------------------------ dashboard
    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, admin=Depends(current_admin)):
        day = 86400
        t = store.now()
        with store.ro() as con:
            customers = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            new_customers = con.execute("SELECT COUNT(*) FROM users WHERE created_at>?", (t - 7 * day,)).fetchone()[0]
            paid_q = "FROM orders WHERE payment_status IN ('paid','partially_refunded')"
            orders_paid = con.execute(f"SELECT COUNT(*) {paid_q}").fetchone()[0]
            revenue = con.execute(f"SELECT COALESCE(SUM(total_pence),0) {paid_q}").fetchone()[0]
            refunds = con.execute("SELECT COALESCE(SUM(refunded_pence),0) FROM payments").fetchone()[0]
            rev_7 = con.execute(f"SELECT COALESCE(SUM(total_pence),0) {paid_q} AND paid_at>?", (t - 7 * day,)).fetchone()[0]
            rev_30 = con.execute(f"SELECT COALESCE(SUM(total_pence),0) {paid_q} AND paid_at>?", (t - 30 * day,)).fetchone()[0]
            to_ship = con.execute("SELECT COUNT(*) FROM orders WHERE status IN ('paid','processing')").fetchone()[0]
            awaiting = con.execute("SELECT COUNT(*) FROM orders WHERE status='awaiting_payment'").fetchone()[0]
            open_tickets = con.execute("SELECT COUNT(*) FROM tickets WHERE status='open'").fetchone()[0]
            pay_stats = con.execute("SELECT status, COUNT(*) n, COALESCE(SUM(amount_pence),0) amt FROM payments GROUP BY status").fetchall()
            recent = con.execute("SELECT o.*, u.username, u.first_name FROM orders o JOIN users u ON u.id=o.user_id "
                                 "WHERE o.status!='cancelled' ORDER BY o.id DESC LIMIT 8").fetchall()
            totals_by_day = con.execute(
                f"SELECT paid_at, total_pence {paid_q} AND paid_at>?", (t - 14 * day,)).fetchall()
            top = con.execute("""SELECT oi.name, SUM(oi.qty) qty, SUM(oi.qty*oi.unit_pence) amt FROM order_items oi
                                 JOIN orders o ON o.id=oi.order_id WHERE o.payment_status IN ('paid','partially_refunded')
                                 GROUP BY oi.name ORDER BY amt DESC LIMIT 5""").fetchall()
        days = []
        for i in range(13, -1, -1):
            start = time.mktime(time.localtime(t - i * day)[:3] + (0, 0, 0, 0, 0, -1))
            amt = sum(r["total_pence"] for r in totals_by_day if start <= r["paid_at"] < start + day)
            days.append({"label": time.strftime("%d %b", time.localtime(start)), "amt": amt})
        peak = max([d["amt"] for d in days] + [1])
        return render(request, "dashboard.html", admin, customers=customers, new_customers=new_customers,
                      orders_paid=orders_paid, revenue=revenue - refunds, refunds=refunds, rev_7=rev_7, rev_30=rev_30,
                      avg=(revenue // orders_paid if orders_paid else 0), to_ship=to_ship, awaiting=awaiting,
                      open_tickets=open_tickets, pay_stats=pay_stats, recent=recent, days=days, peak=peak, top=top,
                      payments_live=payments.provider() is not None,
                      db_ephemeral=not os.getenv("DB_PATH"),
                      test_mode=bool(payments.provider() and payments.provider().test_mode))

    # ------------------------------------------------------------ products
    @app.get("/products", response_class=HTMLResponse)
    async def products_page(request: Request, admin=Depends(need("staff"))):
        return render(request, "products.html", admin, products=store.list_products(shop_only=False),
                      safe=store.product_is_safe)

    def product_form(request, admin, product=None, values=None, error=None):
        return render(request, "product_form.html", admin, product=product, values=values or (dict(product) if product else {}),
                      categories=store.list_categories(active_only=False), error=error)

    @app.get("/products/new", response_class=HTMLResponse)
    async def product_new(request: Request, admin=Depends(need("manager"))):
        return product_form(request, admin, values={"active": 1})

    @app.get("/products/{pid}", response_class=HTMLResponse)
    async def product_edit(request: Request, pid: int, admin=Depends(need("manager"))):
        p = store.get_product(pid, shop_only=False)
        if p is None:
            raise HTTPException(404)
        v = dict(p)
        v["price"] = f"{p['price_pence'] / 100:.2f}"
        v["stock"] = "" if p["stock"] is None else p["stock"]
        return product_form(request, admin, p, v)

    @app.post("/products/save")
    async def product_save(request: Request, admin=Depends(need("manager")), _=Depends(check_csrf)):
        form = dict(await request.form())
        pid = int(form["id"]) if form.get("id") else None
        try:
            new_id = await asyncio.to_thread(store.save_product, form, pid)
        except store.ProductRejected as e:
            p = store.get_product(pid, shop_only=False) if pid else None
            return product_form(request, admin, p, form, refusal_message(e))
        flash(request, "Product saved.")
        return back(f"/products/{new_id}")

    @app.post("/products/{pid}/image")
    async def product_image(request: Request, pid: int, image: UploadFile = File(...),
                            admin=Depends(need("manager")), _=Depends(check_csrf)):
        if store.get_product(pid, shop_only=False) is None:
            raise HTTPException(404)
        data = await image.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            flash(request, "That image is over 5 MB. Use a smaller one.", "error")
            return back(f"/products/{pid}")
        if data[:3] == b"\xff\xd8\xff":
            ext = "jpg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            ext = "png"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ext = "webp"
        else:
            flash(request, "Upload a JPG, PNG or WebP image.", "error")
            return back(f"/products/{pid}")
        path = images_dir() / f"product-{pid}-{secrets.token_hex(4)}.{ext}"
        path.write_bytes(data)
        old = store.get_product(pid, shop_only=False)["image_path"]
        store.set_product_image(pid, str(path))
        if old and Path(old).exists() and Path(old).parent == images_dir():
            Path(old).unlink(missing_ok=True)
        flash(request, "Image uploaded.")
        return back(f"/products/{pid}")

    @app.post("/products/{pid}/image/remove")
    async def product_image_remove(request: Request, pid: int, admin=Depends(need("manager")), _=Depends(check_csrf)):
        store.set_product_image(pid, None)
        flash(request, "Image removed.")
        return back(f"/products/{pid}")

    @app.post("/products/{pid}/delete")
    async def product_delete(request: Request, pid: int, confirm: str = Form(""), admin=Depends(need("manager")),
                             _=Depends(check_csrf)):
        if confirm != "yes":
            flash(request, "Tick the box to confirm you want to remove it.", "error")
            return back(f"/products/{pid}")
        store.remove_product(pid)
        flash(request, "Product removed from the shop. Past orders still show it.")
        return back("/products")

    @app.get("/media/product/{pid}")
    async def product_media(request: Request, pid: int, admin=Depends(current_admin)):
        p = store.get_product(pid, shop_only=False)
        if p is None or not p["image_path"] or not Path(p["image_path"]).exists():
            raise HTTPException(404)
        return FileResponse(p["image_path"])

    # ------------------------------------------------------------ categories
    @app.get("/categories", response_class=HTMLResponse)
    async def categories_page(request: Request, admin=Depends(need("manager"))):
        with store.ro() as con:
            counts = {r["category_id"]: r["n"] for r in con.execute(
                "SELECT category_id, COUNT(*) n FROM products WHERE deleted=0 GROUP BY category_id")}
        return render(request, "categories.html", admin, categories=store.list_categories(active_only=False), counts=counts)

    @app.post("/categories/save")
    async def category_save(request: Request, admin=Depends(need("manager")), _=Depends(check_csrf)):
        form = await request.form()
        cid = int(form["id"]) if form.get("id") else None
        try:
            store.save_category(str(form.get("name", "")), cid, active=form.get("active") == "on",
                                sort=int(form["sort"]) if str(form.get("sort", "")).lstrip("-").isdigit() else None)
            flash(request, "Category saved.")
        except store.ProductRejected as e:
            flash(request, refusal_message(e), "error")
        return back("/categories")

    @app.post("/categories/{cid}/delete")
    async def category_delete(request: Request, cid: int, confirm: str = Form(""), admin=Depends(need("manager")),
                              _=Depends(check_csrf)):
        if confirm != "yes":
            flash(request, "Tick the box next to Delete to confirm.", "error")
            return back("/categories")
        store.delete_category(cid)
        flash(request, "Category deleted. Its products are now uncategorised.")
        return back("/categories")

    # ------------------------------------------------------------ orders
    @app.get("/orders", response_class=HTMLResponse)
    async def orders_page(request: Request, q: str = "", status: str = "", admin=Depends(need("staff"))):
        sql = "SELECT o.*, u.username, u.first_name FROM orders o JOIN users u ON u.id=o.user_id WHERE 1=1"
        args: list = []
        if status:
            sql += " AND o.status=?"
            args.append(status)
        else:
            sql += " AND o.status!='cancelled'"
        if q.strip():
            like = f"%{q.strip()}%"
            sql += (" AND (o.ref LIKE ? OR u.username LIKE ? OR u.first_name LIKE ? OR o.customer_email LIKE ?"
                    " OR o.customer_name LIKE ? OR CAST(o.user_id AS TEXT)=?)")
            args += [like, like, like, like, like, q.strip()]
        sql += " ORDER BY o.id DESC LIMIT 200"
        with store.ro() as con:
            rows = con.execute(sql, args).fetchall()
        return render(request, "orders.html", admin, orders=rows, q=q, status=status, statuses=store.ORDER_STATUSES)

    @app.get("/orders/{oid}", response_class=HTMLResponse)
    async def order_page(request: Request, oid: int, admin=Depends(need("staff"))):
        o = store.get_order(oid)
        if o is None:
            raise HTTPException(404)
        return render(request, "order.html", admin, o=o, items=store.order_items(oid), events=store.order_events(oid),
                      pays=store.order_payments(oid), customer=store.get_user(o["user_id"]), statuses=store.ORDER_STATUSES)

    @app.post("/orders/{oid}/status")
    async def order_status(request: Request, oid: int, status: str = Form(...), tracking_number: str = Form(""),
                           tracking_url: str = Form(""), admin=Depends(need("staff")), _=Depends(check_csrf)):
        try:
            o, changed = store.update_order_status(oid, status, f"admin:{admin['username']}",
                                                   tracking_number=tracking_number, tracking_url=tracking_url)
        except ValueError as e:
            flash(request, str(e), "error")
            return back(f"/orders/{oid}")
        if changed:
            notify.order_status_changed(o)
        flash(request, "Order updated." + (" The customer has been messaged." if changed and store.get_flag("notify_customer_status") else ""))
        return back(f"/orders/{oid}")

    @app.post("/orders/{oid}/notes")
    async def order_notes(request: Request, oid: int, notes: str = Form(""), admin=Depends(need("staff")), _=Depends(check_csrf)):
        store.set_order_notes(oid, notes)
        flash(request, "Notes saved.")
        return back(f"/orders/{oid}")

    @app.post("/orders/{oid}/refresh-payment")
    async def order_refresh(request: Request, oid: int, admin=Depends(need("staff")), _=Depends(check_csrf)):
        o = store.get_order(oid)
        try:
            o2, newly = await asyncio.to_thread(payments.recheck_order, o)
            if newly and o2 is not None:
                notify.order_paid(o2)
            flash(request, "Checked with Stripe." + (" Payment confirmed." if newly else ""))
        except payments.PaymentError as e:
            flash(request, f"Couldn't check with Stripe: {e}", "error")
        return back(f"/orders/{oid}")

    @app.post("/orders/{oid}/refund")
    async def order_refund(request: Request, oid: int, amount: str = Form(""), confirm: str = Form(""),
                           admin=Depends(need("manager")), _=Depends(check_csrf)):
        if confirm != "yes":
            flash(request, "Tick the box to confirm the refund.", "error")
            return back(f"/orders/{oid}")
        o = store.get_order(oid)
        pay = next((p for p in reversed(store.order_payments(oid)) if p["status"] in ("paid", "partially_refunded")), None)
        prov = payments.provider()
        if o is None or pay is None or not pay["provider_payment_id"] or prov is None:
            flash(request, "There's no confirmed Stripe payment to refund on this order.", "error")
            return back(f"/orders/{oid}")
        try:
            pence = round(float(amount) * 100) if amount.strip() else None
        except ValueError:
            flash(request, "Refund amount must be a number, or blank for a full refund.", "error")
            return back(f"/orders/{oid}")
        remaining = pay["amount_pence"] - pay["refunded_pence"]
        if pence is not None and not (0 < pence <= remaining):
            flash(request, f"Refund must be between £0.01 and {money(remaining)}.", "error")
            return back(f"/orders/{oid}")
        try:
            await asyncio.to_thread(prov.refund, pay["provider_payment_id"], pence,
                                    f"refund-{o['ref']}-{pay['refunded_pence']}-{pence or 'full'}")
        except payments.PaymentError as e:
            flash(request, f"Stripe refused the refund: {e}", "error")
            return back(f"/orders/{oid}")
        with store.tx() as con:
            store.log_order_event(con, oid, "refund_requested", money(pence or remaining), f"admin:{admin['username']}")
        flash(request, "Refund sent to Stripe. The order updates when Stripe confirms it (usually seconds).")
        return back(f"/orders/{oid}")

    # ------------------------------------------------------------ customers
    @app.get("/customers", response_class=HTMLResponse)
    async def customers_page(request: Request, q: str = "", admin=Depends(need("staff"))):
        sql = """SELECT u.*, (SELECT COUNT(*) FROM orders o WHERE o.user_id=u.id AND o.payment_status='paid') paid_orders,
                        (SELECT COALESCE(SUM(total_pence),0) FROM orders o WHERE o.user_id=u.id AND o.payment_status='paid') spent
                 FROM users u"""
        args: list = []
        if q.strip():
            like = f"%{q.strip()}%"
            sql += " WHERE u.username LIKE ? OR u.first_name LIKE ? OR CAST(u.id AS TEXT)=? OR u.referral_code=?"
            args = [like, like, q.strip(), q.strip().upper()]
        sql += " ORDER BY u.last_seen DESC LIMIT 300"
        with store.ro() as con:
            rows = con.execute(sql, args).fetchall()
        return render(request, "customers.html", admin, customers=rows, q=q)

    @app.get("/customers/{uid}", response_class=HTMLResponse)
    async def customer_page(request: Request, uid: int, admin=Depends(need("staff"))):
        u = store.get_user(uid)
        if u is None:
            raise HTTPException(404)
        with store.ro() as con:
            tickets = con.execute("SELECT * FROM tickets WHERE user_id=? ORDER BY id DESC", (uid,)).fetchall()
            referrer = con.execute("SELECT * FROM users WHERE id=?", (u["referred_by"],)).fetchone() if u["referred_by"] else None
        joined, ordered = store.referral_stats(uid)
        return render(request, "customer.html", admin, u=u, orders=store.user_orders(uid, 100, include_unpaid=True),
                      tickets=tickets, referrer=referrer, joined=joined, ordered=ordered)

    @app.post("/customers/{uid}/block")
    async def customer_block(request: Request, uid: int, blocked: str = Form("0"), admin=Depends(need("manager")),
                             _=Depends(check_csrf)):
        store.set_blocked(uid, blocked == "1")
        flash(request, "Customer paused. They can't shop until you unpause them." if blocked == "1" else "Customer unpaused.")
        return back(f"/customers/{uid}")

    @app.post("/customers/{uid}/message")
    async def customer_message(request: Request, uid: int, body: str = Form(""), admin=Depends(need("staff")),
                               _=Depends(check_csrf)):
        if not body.strip():
            return back(f"/customers/{uid}")
        tid, _new = store.add_ticket_message(uid, body, sender="admin", admin_id=admin["id"])
        mid = store.ticket_messages(tid)[-1]["id"]
        notify.ticket_reply(uid, tid, mid, body.strip())
        flash(request, "Message queued. It's sent within a few seconds.")
        return back(f"/customers/{uid}")

    # ------------------------------------------------------------ support
    @app.get("/support", response_class=HTMLResponse)
    async def support_page(request: Request, status: str = "open", admin=Depends(need("staff"))):
        sql = """SELECT t.*, u.username, u.first_name,
                   (SELECT body FROM ticket_messages m WHERE m.ticket_id=t.id ORDER BY id DESC LIMIT 1) last_body
                 FROM tickets t JOIN users u ON u.id=t.user_id"""
        args: list = []
        if status in ("open", "answered", "closed"):
            sql += " WHERE t.status=?"
            args.append(status)
        sql += " ORDER BY t.updated_at DESC LIMIT 200"
        with store.ro() as con:
            rows = con.execute(sql, args).fetchall()
        return render(request, "support.html", admin, tickets=rows, status=status)

    @app.get("/support/{tid}", response_class=HTMLResponse)
    async def ticket_page(request: Request, tid: int, admin=Depends(need("staff"))):
        t = store.get_ticket(tid)
        if t is None:
            raise HTTPException(404)
        return render(request, "ticket.html", admin, t=t, messages=store.ticket_messages(tid))

    @app.post("/support/{tid}/reply")
    async def ticket_reply(request: Request, tid: int, body: str = Form(""), close: str = Form(""),
                           admin=Depends(need("staff")), _=Depends(check_csrf)):
        t = store.get_ticket(tid)
        if t is None:
            raise HTTPException(404)
        if body.strip():
            store.add_ticket_message(t["user_id"], body, sender="admin", admin_id=admin["id"], ticket_id=tid)
            mid = store.ticket_messages(tid)[-1]["id"]
            notify.ticket_reply(t["user_id"], tid, mid, body.strip())
        if close:
            store.close_ticket(tid)
        flash(request, "Reply sent." if body.strip() else "Ticket closed.")
        return back(f"/support/{tid}" if not close else "/support")

    # ------------------------------------------------------------ announcements
    @app.get("/announce", response_class=HTMLResponse)
    async def announce_page(request: Request, admin=Depends(need("manager"))):
        with store.ro() as con:
            past = con.execute("SELECT * FROM announcements ORDER BY id DESC LIMIT 20").fetchall()
            audience = con.execute("SELECT COUNT(*) FROM users WHERE blocked=0").fetchone()[0]
        return render(request, "announce.html", admin, past=past, audience=audience)

    @app.post("/announce")
    async def announce_send(request: Request, body: str = Form(""), confirm: str = Form(""),
                            admin=Depends(need("manager")), _=Depends(check_csrf)):
        body = body.strip()
        if not body or confirm != "yes":
            flash(request, "Write a message and tick the box to confirm.", "error")
            return back("/announce")
        if guard.product_problems(body):
            flash(request, guard.PRODUCT_REFUSAL.format(found=", ".join(guard.product_problems(body))), "error")
            return back("/announce")
        with store.tx() as con:
            aid = con.execute("INSERT INTO announcements (body, admin_id, created_at) VALUES (?, ?, ?)",
                              (body, admin["id"], store.now())).lastrowid
        n = notify.announcement(aid, body)
        with store.tx() as con:
            con.execute("UPDATE announcements SET recipients=? WHERE id=?", (n, aid))
        flash(request, f"Announcement queued for {n} customers.")
        return back("/announce")

    # ------------------------------------------------------------ settings
    CONTENT_KEYS = ["brand", "bot_name", "welcome", "about", "faq", "terms", "support_intro", "support_hours", "referral_text"]
    OWNER_FLAGS = ["payments_enabled", "notify_admin_new_order", "notify_admin_new_ticket", "notify_customer_status", "ai_chat_enabled"]

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, admin=Depends(need("manager"))):
        prov = payments.provider() if store.get_flag("payments_enabled") else None
        env = {k: bool(os.getenv(k)) for k in ["TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "ADMIN_CHAT_ID", "STRIPE_SECRET_KEY",
                                               "STRIPE_WEBHOOK_SECRET", "SESSION_SECRET", "DB_PATH"]}
        return render(request, "settings.html", admin, s=store.all_settings(), env=env,
                      stripe_mode=("test" if prov and prov.test_mode else "live" if prov else "off"),
                      webhook_url=str(request.base_url).rstrip("/") + "/stripe/webhook")

    @app.post("/settings")
    async def settings_save(request: Request, admin=Depends(need("manager")), _=Depends(check_csrf)):
        form = await request.form()
        for k in CONTENT_KEYS:
            if k in form:
                v = str(form[k]).replace("\r\n", "\n").strip()
                found = guard.product_problems(v) if k not in ("faq", "terms") else []
                if found:
                    flash(request, guard.PRODUCT_REFUSAL.format(found=", ".join(found)), "error")
                    return back("/settings")
                store.set_setting(k, v[:6000])
        if ROLE_RANK[admin["role"]] >= ROLE_RANK["owner"] and form.get("section") == "owner":
            for k in OWNER_FLAGS:
                store.set_setting(k, "1" if form.get(k) == "on" else "0")
            for k in ("delivery_pence", "free_delivery_over_pence"):
                raw = str(form.get(k.replace("_pence", ""), "")).replace("£", "").strip()
                try:
                    store.set_setting(k, str(max(0, round(float(raw or 0) * 100))))
                except ValueError:
                    flash(request, "Delivery prices must be numbers.", "error")
                    return back("/settings")
            mq = str(form.get("max_qty_per_item", "10"))
            if mq.isdigit() and 1 <= int(mq) <= 99:
                store.set_setting("max_qty_per_item", mq)
            payments.reset()
        flash(request, "Settings saved.")
        return back("/settings")

    # ------------------------------------------------------------ admin accounts
    @app.get("/admins", response_class=HTMLResponse)
    async def admins_page(request: Request, admin=Depends(need("owner"))):
        with store.ro() as con:
            rows = con.execute("SELECT id, username, role, active, created_at, last_login FROM admins ORDER BY id").fetchall()
        return render(request, "admins.html", admin, admins=rows, roles=store.ROLES)

    @app.post("/admins")
    async def admins_save(request: Request, admin=Depends(need("owner")), _=Depends(check_csrf)):
        form = await request.form()
        action = form.get("action")
        try:
            if action == "create":
                store.create_admin(str(form.get("username", "")), str(form.get("password", "")), str(form.get("role", "staff")))
                flash(request, "Admin added.")
            elif action in ("role", "toggle", "password"):
                aid = int(form["id"])
                if aid == admin["id"] and action != "password":
                    raise ValueError("You can't change your own role or switch yourself off.")
                with store.tx() as con:
                    if action == "role":
                        if form.get("role") not in store.ROLES:
                            raise ValueError("Unknown role")
                        con.execute("UPDATE admins SET role=? WHERE id=?", (form["role"], aid))
                    elif action == "toggle":
                        con.execute("UPDATE admins SET active=1-active WHERE id=?", (aid,))
                    else:
                        pw = str(form.get("password", ""))
                        if len(pw) < 10:
                            raise ValueError("Passwords need at least 10 characters.")
                        con.execute("UPDATE admins SET password_hash=? WHERE id=?", (store.hash_password(pw), aid))
                    owners = con.execute("SELECT COUNT(*) FROM admins WHERE role='owner' AND active=1").fetchone()[0]
                    if owners == 0:
                        raise ValueError("There must always be at least one active owner.")
                flash(request, "Saved.")
        except ValueError as e:
            flash(request, str(e), "error")
        return back("/admins")

    @app.get("/account", response_class=HTMLResponse)
    async def account_page(request: Request, admin=Depends(current_admin)):
        return render(request, "account.html", admin)

    @app.post("/account")
    async def account_save(request: Request, current: str = Form(""), new: str = Form(""),
                           admin=Depends(current_admin), _=Depends(check_csrf)):
        if not store.check_password(current, admin["password_hash"]):
            flash(request, "Your current password is wrong.", "error")
        elif len(new) < 10:
            flash(request, "New passwords need at least 10 characters.", "error")
        else:
            with store.tx() as con:
                con.execute("UPDATE admins SET password_hash=? WHERE id=?", (store.hash_password(new), admin["id"]))
            flash(request, "Password changed.")
        return back("/account")

    # ------------------------------------------------------------ Stripe webhook
    @app.post("/stripe/webhook")
    async def stripe_webhook(request: Request):
        prov = payments.provider()
        if prov is None:
            return PlainTextResponse("payments off", status_code=503)
        payload = await request.body()
        try:
            event = prov.parse_webhook(payload, request.headers.get("stripe-signature", ""))
        except payments.PaymentError as e:
            log.warning("Rejected webhook: %s", e)
            return PlainTextResponse("bad signature", status_code=400)
        if await asyncio.to_thread(store.seen_event, "stripe", event.id, event.type):
            return JSONResponse({"ok": True, "duplicate": True})
        try:
            await handle_stripe_event(prov, event)
        except Exception:
            log.exception("Webhook %s failed", event.id)
            with store.tx() as con:  # let Stripe retry it
                con.execute("DELETE FROM payment_events WHERE provider='stripe' AND event_id=?", (event.id,))
            return PlainTextResponse("error", status_code=500)
        return JSONResponse({"ok": True})

    async def handle_stripe_event(prov, event) -> None:
        obj = event.data.object
        if event.type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            # Don't trust the event body alone: look the session up directly with Stripe.
            v = await asyncio.to_thread(prov.verify, obj.id)
            order, newly = payments.apply_verified(v, prov.name)
            if newly and order is not None:
                notify.order_paid(order)
        elif event.type in ("checkout.session.expired", "checkout.session.async_payment_failed"):
            order = store.mark_payment_failed(prov.name, obj.id, "expired" if event.type.endswith("expired") else "failed")
            if order is not None and event.type.endswith("failed"):
                notify.payment_problem(order, "failed")
        elif event.type == "charge.refunded":
            pi = obj.payment_intent if isinstance(obj.payment_intent, str) else getattr(obj.payment_intent, "id", None)
            order = store.mark_refund(prov.name, pi, obj.amount_refunded) if pi else None
            if order is not None and order["status"] == "refunded":
                notify.order_status_changed(order)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/stripe/"):
            return PlainTextResponse(str(exc.detail), status_code=exc.status_code)
        aid = request.session.get("admin_id") if "session" in request.scope else None
        admin = store.get_admin(aid) if aid else None
        msg = exc.detail if exc.status_code != 404 else "That page doesn't exist."
        return render(request, "error.html", admin, status_code=exc.status_code, code=exc.status_code, message=msg)

    return app
