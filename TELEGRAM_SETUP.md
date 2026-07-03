# Telegram alerts setup (free, ~10 minutes)

Telegram is the best free channel: no per-user key step like WhatsApp/CallMeBot,
no per-message cost like SMS. Each subscriber just messages your bot once.

## One-time: create your bot
1. In Telegram, search for **@BotFather** and open it.
2. Send `/newbot`. Pick a name and a username ending in "bot"
   (e.g. ipo_ledger_bot).
3. BotFather replies with a **token** like `7712345678:AAH...`. Keep it secret.
4. Set it where the alert sender runs (your laptop):
   ```
   export TELEGRAM_BOT_TOKEN="7712345678:AAH..."
   ```

## How a subscriber connects (what your users do)
1. They open your bot link: `https://t.me/your_bot_username`
2. They tap **Start** (sends `/start`).
3. You need their **chat_id**. Two ways:
   - Simple manual: visit
     `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser after they
     /start. Find `"chat":{"id":123456789}`. That number is their chat_id.
   - Automated (later): run a tiny webhook that captures chat_id on /start and
     saves it against their email. Not needed for v1 / small numbers.
4. Store the chat_id in their subscriber record (the `telegram` field on
   /subscribe accepts it).

## Test it
```
export TELEGRAM_BOT_TOKEN="..."
python -c "import send_alerts as A; print(A.send_telegram('YOUR_CHAT_ID','test from IPOPredict'))"
```
A `True` and a message on your phone means it works.

## When you scale
The manual getUpdates step is fine for tens of users. For more, add a /start
webhook that auto-saves chat_ids. The sender code does not change: it already
reads the `telegram` field per subscriber.
