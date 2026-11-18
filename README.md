# Pepper: your Telegram shop bot

Pepper sits in your Telegram group and in private chats. It does four jobs without you or Bert:

1. **Welcomes** new group members and invites them to a private chat.
2. **Talks to customers** in your voice. It asks about their skin, recommends one product and answers questions from your FAQ.
3. **Takes them to payment.** Each recommendation comes with a button that opens a secure Stripe checkout, where they pay and enter their address.
4. **Tracks every lead.** Your private team chat gets an alert whenever someone opens checkout or asks for a person. Type `/leads` for stats and `/export` for a spreadsheet.

It refuses to talk about injectables, "research" peptides, weight-loss drugs or dosing. Those messages never reach the AI.

---

## What you'll need (about 1 hour the first time)

| Thing | Where | Cost |
| --- | --- | --- |
| Telegram bot token | @BotFather in Telegram | Free |
| Claude API key (the AI brain) | console.anthropic.com | Pay as you go, usually pennies per chat |
| Stripe account (payments) | stripe.com | 1.5% + 20p per UK card payment |
| Somewhere to run it 24/7 | railway.com | About $5 a month |

---

## Step 1: Make the bot in Telegram

1. In Telegram, open **@BotFather** and send `/newbot`. Name it **Pepper** and give it a username such as `ABLabsSkinBot`.
2. Copy the **token** it gives you and keep it private.
3. Send `/setprivacy`, pick your bot and choose **Disable**, so it can see group messages.
4. Optional: use `/setuserpic` to give it a profile photo and `/setdescription` to add a description.

## Step 2: Make two Telegram groups

- **Customer group** (for example "AB Labs Skin Club"): add the bot and make it an **admin**.
- **Team group** (just you, Bert and the bot): this is where the alerts arrive. Once the bot is running (step 6), send `/chatid` here and copy the number it replies with.

## Step 3: Set up Stripe payment links

1. Create a Stripe account and add your company and bank details.
2. Go to **Product catalogue**, then **Add product**. Add each product with its price.
   - For the Refill Club, add a second price on the same product: **Recurring**, custom interval, **every 6 weeks**.
3. Go to **Payment Links**, then **New**. Pick a product, tick **Collect customers' addresses** (shipping, UK), then create the link. Do this for each product, plus one link for each subscription price.
4. Go to **Settings**, then **Billing**, then **Customer portal**, and switch it on so subscribers can cancel themselves.
5. Paste each link into `catalogue.json`, replacing the `https://buy.stripe.com/REPLACE_ME...` placeholders. A product whose link is still a placeholder shows "checkout not switched on yet" instead of a broken link.

Stripe emails you on every sale and keeps the order list with each customer's address, so that's your packing list.

## Step 4: Get a Claude API key

At console.anthropic.com, add a payment card under **Billing**, then go to **API keys** and **Create key**. Set a monthly spend limit (for example £20) so there are no surprises.

## Step 5: Make it yours (edit the text files, no coding)

| File | What to change |
| --- | --- |
| `catalogue.json` | Brand name, bot name, products, prices, descriptions, payment links |
| `voice.md` | How the bot talks. Paste in 5 to 10 real messages you've sent customers and it will copy your style |
| `faq.md` | Delivery, returns and payment answers. The bot only answers from this file and the catalogue, so if something's missing here it says so and passes the customer to you |

Keep the quote marks and commas in `catalogue.json` exactly as they are. If the bot won't start after an edit, a missing comma is usually why.

## Step 6: Put it online

**Option A: test it on your own computer first (10 minutes)**
1. Install Python 3.11 or newer from python.org.
2. Open a terminal in this folder and run `pip install -r requirements.txt`.
3. Copy `.env.example` to a new file called `.env` and fill in the token, API key and team chat id.
4. Run `python bot.py`, then message your bot in Telegram. It only runs while this window is open.

**Option B: run it 24/7 on Railway**
1. Create a free GitHub account, make a new **private** repository, and drag this folder's files into it. Don't upload `.env`.
2. At railway.com, choose **New project**, then **Deploy from GitHub repo**, and pick that repository.
3. Under **Variables**, add `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY` and `ADMIN_CHAT_ID`.
4. Under **Settings**, add a **Volume** mounted at `/data`, and add the variable `DB_PATH=/data/leads.db` so your leads survive restarts.
5. Railway starts it automatically using the `Procfile`. From now on, every change you save on GitHub redeploys the bot.

## Team commands (only work in your team group)

| Command | What it does |
| --- | --- |
| `/leads` | Lead counts and the 10 most recent people |
| `/export` | Every lead as a spreadsheet (CSV) |
| `/reply 123456789 your message` | Reply privately to a customer. The number is in each alert |
| `/chatid` | Shows a chat's id (works anywhere) |

## Customer commands

`/start` opens the menu, `/shop` shows the range and `/help` asks for a person. In the group, customers tag `@YourBot` or reply to it to ask a question.

## Checking it still works after edits

Run `python -m pytest -q`. All tests should pass. They don't need internet access or keys.
