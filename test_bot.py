"""Offline tests: no Telegram or Claude calls. Run with:  python -m pytest -q"""
import asyncio
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "leads.db")
os.environ["ADMIN_CHAT_ID"] = "-100999"

import bot  # noqa: E402
from telegram.constants import ChatType  # noqa: E402


def test_catalogue_ids_and_prices():
    assert bot.PRODUCTS, "catalogue is empty"
    for p in bot.PRODUCTS.values():
        assert p["price"] > 0 and p["subscription_price"] <= p["price"]


def test_blocked_words():
    for t in ["do you sell reta?", "Retatrutide price", "BPC-157 dose", "got any sema",
              "is it injectable", "how many mcg", "bac water", "research peptides", "weight loss"]:
        assert bot.is_blocked(t), t
    for t in ["I have fine lines", "retail price?", "semantic question", "copper peptide serum",
              "is it vegan", "puffy eyes", "pinch of salt"]:
        assert not bot.is_blocked(t), t


def test_parse_reply():
    text, ids, handoff = bot.parse_reply("Go copper.\n[[product:copper-serum]]\n[[product:nope]]\n[[handoff]]")
    assert text == "Go copper."
    assert ids == ["copper-serum"]
    assert handoff


def test_system_prompt_hides_links():
    assert "buy.stripe.com" not in bot.SYSTEM_PROMPT
    assert "Copper Peptide Serum" in bot.SYSTEM_PROMPT


def _fake(chat_type=ChatType.PRIVATE, text="hi", chat_id=111):
    user = SimpleNamespace(id=42, is_bot=False, username="jane", first_name="Jane", full_name="Jane D")
    msg = SimpleNamespace(text=text, reply_text=AsyncMock(), reply_to_message=None)
    update = SimpleNamespace(effective_message=msg, message=msg, effective_user=user,
                             effective_chat=SimpleNamespace(id=chat_id, type=chat_type), callback_query=None)
    ctx = SimpleNamespace(user_data={}, args=[],
                          bot=SimpleNamespace(username="CelluneBot", id=7, send_chat_action=AsyncMock(),
                                              send_message=AsyncMock()))
    return update, ctx, msg


def test_ai_reply_gets_checkout_button():
    update, ctx, msg = _fake(text="I've got fine lines and dull skin")
    with patch.object(bot, "ask_ai", AsyncMock(return_value="Copper's your one.\n[[product:copper-serum]]")):
        asyncio.run(bot.on_text(update, ctx))
    args, kwargs = msg.reply_text.call_args
    assert args[0] == "Copper's your one."
    kb = kwargs["reply_markup"].inline_keyboard
    assert kb[0][0].callback_data == "buy:copper-serum"
    assert ctx.user_data["history"][-1]["role"] == "assistant"


def test_blocked_never_reaches_ai():
    update, ctx, msg = _fake(text="can I get retatrutide")
    ai = AsyncMock()
    with patch.object(bot, "ask_ai", ai):
        asyncio.run(bot.on_text(update, ctx))
    ai.assert_not_called()
    assert "only sell skincare" in msg.reply_text.call_args[0][0]


def test_group_ignores_untagged_and_links_to_dm():
    update, ctx, msg = _fake(chat_type=ChatType.SUPERGROUP, text="anyone tried it?", chat_id=-100123)
    with patch.object(bot, "ask_ai", AsyncMock(return_value="x")) as ai:
        asyncio.run(bot.on_text(update, ctx))
        ai.assert_not_called()
    update, ctx, msg = _fake(chat_type=ChatType.SUPERGROUP, text="@CelluneBot what's best for eyes?", chat_id=-100123)
    with patch.object(bot, "ask_ai", AsyncMock(return_value="Eye cream.\n[[product:eye-cream]]")):
        asyncio.run(bot.on_text(update, ctx))
    kb = msg.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
    assert kb[0][0].url == "https://t.me/CelluneBot?start=p_eye-cream"


def test_checkout_with_unset_link_alerts_admin():
    update, ctx, msg = _fake()
    asyncio.run(bot.send_checkout(update, ctx, "copper-serum", subscribe=False))
    assert "isnt live yet" in msg.reply_text.call_args[0][0]
    ctx.bot.send_message.assert_awaited()


def test_checkout_with_real_link():
    update, ctx, msg = _fake()
    with patch.dict(bot.PRODUCTS["eye-cream"], {"buy_link": "https://buy.stripe.com/test_123"}):
        asyncio.run(bot.send_checkout(update, ctx, "eye-cream", subscribe=False))
    kb = msg.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
    assert kb[0][0].url == "https://buy.stripe.com/test_123"
    alert = ctx.bot.send_message.call_args[0]
    assert alert[0] == -100999 and "Checkout opened" in alert[1]


def test_leads_recorded():
    con = bot.db()
    row = con.execute("SELECT username, messages, checkout_clicks FROM leads WHERE user_id=42").fetchone()
    assert row[0] == "jane" and row[1] >= 1


def test_app_builds():
    app = bot.build_app("123456:TEST-TOKEN")
    assert len(app.handlers[0]) == 10


def test_prompt_requires_honesty_about_being_ai():
    assert "never deny being an AI" in bot.SYSTEM_PROMPT


def test_bursts_sent_as_separate_messages():
    update, ctx, msg = _fake(text="hi")
    with patch.object(bot, "ask_ai", AsyncMock(return_value="yh its our best seller\n\nwant the link?\n[[product:copper-serum]]")), \
         patch.object(bot.asyncio, "sleep", AsyncMock()):
        asyncio.run(bot.on_text(update, ctx))
    calls = msg.reply_text.call_args_list
    assert [c.args[0] for c in calls] == ["yh its our best seller", "want the link?"]
    assert calls[0].kwargs["reply_markup"] is None and calls[1].kwargs["reply_markup"] is not None
