"""
telegram_bot.py - the chat-ID helper for IPOPredict Telegram alerts.

WHY THIS EXISTS
A Telegram bot can only message a person AFTER that person has messaged the
bot first, and you address them by a numeric chat_id (not their @username).
So every subscriber needs to (a) open your bot, (b) tap Start, and (c) learn
their chat_id to paste into the Alerts page. This script does (b)->(c)
automatically: it watches for anyone who messages the bot and instantly
replies with their chat_id.

ONE-TIME SETUP
  1) In Telegram, open @BotFather -> /newbot -> follow prompts -> copy the token.
  2) set TELEGRAM_BOT_TOKEN=123456:ABC...   (the SAME token send_alerts.py uses)
  3) py -V:3.12 telegram_bot.py
  4) Tell users: "Open <your bot link> in Telegram and tap Start."
     The bot replies with their chat ID; they paste it into the Alerts page
     (Telegram field) and pick a tier. Done.

Leave it running while people sign up, or just run it on demand. It also
prints every chat_id it sees so you can confirm who is connected. Ctrl+C stops.
This is only for onboarding; the actual alerts are sent by send_alerts.py.
"""
import os, sys, json, time, urllib.parse, urllib.request

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
API = "https://api.telegram.org/bot" + TOKEN

REPLY = ("You're connected to IPOPredict.\n\n"
         "Your Telegram chat ID is:\n{cid}\n\n"
         "Paste this into the Telegram field on the Alerts page and pick your "
         "accuracy tier. You'll then get IPO alerts here on closing day "
         "(morning, midday, and a final read before the 5 PM UPI mandate cutoff).\n\n"
         "Information only, not investment advice.")


def _api(method, post=False, **params):
    url = API + "/" + method
    if post:
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data)
    else:
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode())


def main():
    if not TOKEN:
        print("Set TELEGRAM_BOT_TOKEN first (the token @BotFather gave you).")
        sys.exit(1)
    try:
        me = _api("getMe")
        if not me.get("ok"):
            print("Token rejected by Telegram:", me)
            sys.exit(1)
        uname = me["result"].get("username", "?")
        print("Bot @%s is live. Share this link: https://t.me/%s" % (uname, uname))
        print("Tell users to open it and tap Start. Watching for messages (Ctrl+C to stop)...\n")
    except Exception as e:
        print("Could not reach Telegram:", e)
        sys.exit(1)

    seen = set()
    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            resp = _api("getUpdates", **params)
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message") or {}
                chat = msg.get("chat") or {}
                cid = chat.get("id")
                if cid is None:
                    continue
                who = chat.get("username") or chat.get("first_name") or "?"
                if cid not in seen:
                    seen.add(cid)
                    print("  new contact -> chat_id %s  (@%s)" % (cid, who))
                try:
                    _api("sendMessage", post=True, chat_id=cid, text=REPLY.format(cid=cid))
                except Exception as e:
                    print("  reply failed for %s: %s" % (cid, e))
        except KeyboardInterrupt:
            print("\nstopped.")
            break
        except Exception as e:
            print("  poll error:", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
