"""
Notifications. Everything goes through the `notifications` table (an outbox):
  * each message has a unique key, so the same alert is never sent twice,
    even if a webhook arrives twice or the bot restarts mid-send;
  * a background loop sends them, retries failures, and stays under Telegram's limits.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3

import store

log = logging.getLogger("notify")


def admin_chat_id() -> int:
    return int(os.getenv("ADMIN_CHAT_ID", "0") or 0)


def money(pence: int) -> str:
    return f"£{pence / 100:,.2f}"


def order_lines(order: sqlite3.Row) -> str:
    lines = [f"{it['qty']} × {it['name']}  {money(it['unit_pence'] * it['qty'])}" for it in store.order_items(order["id"])]
    lines.append(f"Delivery  {money(order['delivery_pence'])}" if order["delivery_pence"] else "Delivery  free")
    lines.append(f"Total  {money(order['total_pence'])}")
    return "\n".join(lines)


def order_paid(order: sqlite3.Row) -> None:
    store.queue_notification(
        f"order:{order['id']}:paid:customer", order["user_id"],
        f"payment received, thank u 🙏\n\nOrder {order['ref']}\n{order_lines(order)}\n\n"
        "we'll message u here when it ships. tap My orders any time to check on it")
    if store.get_flag("notify_admin_new_order"):
        store.queue_notification(
            f"order:{order['id']}:paid:admin", admin_chat_id(),
            f"💷 New paid order {order['ref']} ({money(order['total_pence'])})\n{order_lines(order)}\n\n"
            f"Ship to: {order['shipping_address'] or 'see Stripe'}\nOpen it in the dashboard to pack and ship.")


def payment_problem(order: sqlite3.Row, status: str) -> None:
    store.queue_notification(
        f"order:{order['id']}:payment:{status}:{store.latest_payment(order['id'])['id'] if store.latest_payment(order['id']) else 0}",
        order["user_id"],
        f"looks like the payment for {order['ref']} didnt go through ({'the page timed out' if status == 'expired' else 'it was declined'}). "
        "ur basket is still saved, tap Basket to try again whenever")


CUSTOMER_STATUS_TEXT = {
    "processing": "ur order {ref} is being packed now 📦",
    "shipped": "ur order {ref} is on its way 🚚{tracking}",
    "delivered": "ur order {ref} has been delivered. hope u love it, any problems just tap Contact support",
    "cancelled": "ur order {ref} has been cancelled. if u didnt expect this tap Contact support and we'll sort it",
    "refunded": "ur refund for {ref} has been processed. it can take 5 to 10 days to show on ur statement",
}


def order_status_changed(order: sqlite3.Row) -> None:
    if not store.get_flag("notify_customer_status"):
        return
    tmpl = CUSTOMER_STATUS_TEXT.get(order["status"])
    if not tmpl:
        return
    tracking = ""
    if order["status"] == "shipped" and (order["tracking_number"] or order["tracking_url"]):
        tracking = "\n\ntracking: " + " ".join(x for x in [order["tracking_number"], order["tracking_url"]] if x)
    store.queue_notification(
        f"order:{order['id']}:status:{order['status']}:{order['tracking_number'] or ''}",
        order["user_id"], tmpl.format(ref=order["ref"], tracking=tracking))


def new_ticket_message(user: sqlite3.Row | None, tid: int, mid_key: str, text: str, is_new: bool) -> None:
    if not store.get_flag("notify_admin_new_ticket"):
        return
    name = (f"@{user['username']}" if user and user["username"] else (user["first_name"] if user else "A customer"))
    store.queue_notification(
        f"ticket:{tid}:{mid_key}:admin", admin_chat_id(),
        f"{'🆕 New support ticket' if is_new else '💬 New message on ticket'} #{tid} from {name}\n\n{text[:800]}\n\n"
        "Reply in the dashboard (Support), or here with:\n"
        f"/reply {user['id'] if user else ''} your message")


def ticket_reply(user_id: int, tid: int, msg_id: int, text: str) -> None:
    store.queue_notification(f"ticket:{tid}:reply:{msg_id}", user_id, f"💬 reply from the team:\n\n{text}")


def announcement(aid: int, body: str) -> int:
    count = 0
    with store.ro() as con:
        users = con.execute("SELECT id FROM users WHERE blocked=0").fetchall()
    for u in users:
        if store.queue_notification(f"announce:{aid}:{u['id']}", u["id"], body):
            count += 1
    return count


async def outbox_loop(bot, stop: asyncio.Event | None = None, interval: float = 2.0) -> None:
    """Send queued notifications forever. Roughly 20 messages a second at most."""
    from telegram.error import BadRequest, Forbidden, RetryAfter

    while not (stop and stop.is_set()):
        try:
            batch = await asyncio.to_thread(store.pending_notifications, 20)
            for n in batch:
                try:
                    await bot.send_message(n["chat_id"], n["body"])
                    await asyncio.to_thread(store.notification_result, n["id"], True)
                except RetryAfter as e:
                    await asyncio.sleep(float(getattr(e, "retry_after", 5)) + 1)
                    break
                except (Forbidden, BadRequest) as e:  # blocked the bot, or chat doesn't exist: don't retry
                    await asyncio.to_thread(store.notification_result, n["id"], False, str(e), give_up=True)
                except Exception as e:
                    log.warning("notification %s failed: %s", n["id"], e)
                    await asyncio.to_thread(store.notification_result, n["id"], False, str(e))
                await asyncio.sleep(0.05)
        except Exception:
            log.exception("outbox loop error")
        await asyncio.sleep(interval)
