# AB Labs shop: Telegram bot + admin dashboard

One Railway service runs everything:

- **The Telegram shop (Pepper).** Customers browse by category, see product pages with photos, pick a quantity, fill a basket, pay on a secure Stripe page, and check their orders and tracking. They can also contact support, use a referral link, and chat to the AI assistant ("Help me choose").
- **The admin dashboard** (a website). Manage products, photos, prices, stock, categories, orders, refunds, customers, support tickets, announcements, shop text, settings and admin accounts without touching code.
- **Payments.** Stripe Checkout. An order is only marked paid when Stripe itself confirms it (a signed webhook, or a direct lookup). A customer's message or screenshot never marks anything paid.
- **Notifications.** Payment confirmations, shipping updates, support replies and team alerts. Each one is sent once, and retried if Telegram fails.

AB Labs uses Pepper for catalogue lookup and order support. Draft products stay hidden until a manager confirms their details and enables them.

## Files

| File | What it is |
| --- | --- |
| `bot.py` | The Telegram shop. Start the whole thing with `python bot.py` |
| `web.py` + `templates/` | The admin dashboard and the Stripe webhook |
| `store.py` | The database (SQLite): customers, products, orders, payments, tickets, settings, admins |
| `payments.py` | Stripe checkout, verification and refunds |
| `notify.py` | Customer and team notifications |
| `guard.py` | Chat and catalogue validation |
| `catalogue.json` | The draft catalogue applied once per database |
| `faq.md` | The starting Q&A. After launch, edit it in the dashboard (Settings) |
| `voice.md` | How the AI assistant talks |
| `test_bot.py` | Offline tests: `python -m pytest -q` |

## Railway variables

Set these in Railway → your service → **Variables**. Values are never shown in the dashboard or logs.

| Variable | Needed? | Where it comes from |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Yes (already set) | @BotFather |
| `ANTHROPIC_API_KEY` | For the AI chat (already set) | console.anthropic.com |
| `DB_PATH` | Yes: `/data/shop.db` | Must point at a Railway volume, or orders are lost on every deploy |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Yes, first start only | You choose. Creates the owner login |
| `SESSION_SECRET` | Yes | Any long random text |
| `ADMIN_CHAT_ID` | Recommended | Send `/chatid` in your team group |
| `STRIPE_SECRET_KEY` | To take payments | Stripe → Developers → API keys. Use `sk_test_...` first |
| `STRIPE_WEBHOOK_SECRET` | To take payments | Stripe → Developers → Webhooks → your endpoint → Signing secret |

`PORT` is set by Railway automatically. The dashboard only runs when it's there.

## Going live on Railway (in order)

1. **Volume.** Railway → service → right-click → **Attach volume**, mount path `/data`.
2. **Variables.** Add `DB_PATH=/data/shop.db`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `SESSION_SECRET` and `ADMIN_CHAT_ID`.
3. **Public address.** Service → **Settings** → **Networking** → **Generate domain**. That's your dashboard address.
4. **Merge the pull request.** Railway redeploys. Sign in at the dashboard address.
5. **Stripe, test mode first.** Add `STRIPE_SECRET_KEY=sk_test_...`. In Stripe → Developers → Webhooks → **Add endpoint**: URL `https://<your-domain>/stripe/webhook`, events `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `checkout.session.async_payment_failed`, `checkout.session.expired`, `charge.refunded`. Copy its signing secret into `STRIPE_WEBHOOK_SECRET`.
6. **Test order.** Buy something in the bot with Stripe's test card `4242 4242 4242 4242`. It should show as paid in the dashboard and you should get the Telegram alerts.
7. **Live.** Swap both Stripe values for the live ones.

## AB Labs catalogue update

On startup, the update hides the three original listings, adds the new catalogue as drafts and replaces outdated shop copy. Historical orders and existing products remain in the database. Confirm formats, sizes and prices in Products before enabling new listings.

## Team group commands

`/leads` quick stats · `/export` leads spreadsheet · `/reply 123456789 message` reply to a customer · `/chatid` show a chat's id.

## Customer commands

`/start` menu · `/shop` all products · `/basket` · `/orders` · `/help` contact support.

## Tests

`python -m pytest -q` runs 40 offline tests covering every menu button, the basket, checkout, duplicate-order protection, payment verification (including forged webhooks and wrong amounts), refunds, access control, notifications, support, referrals, the product blocklist and every dashboard page. They use a throwaway database and a fake Stripe, so they never touch the live bot or real money.
