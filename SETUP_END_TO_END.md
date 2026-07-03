# IPOPredict - End to End Setup (Windows)

Everything runs on your laptop, out of ONE folder. There are two long-running
programs: the **website** (serves the site + handles signups) and the
**scheduler** (scrapes, scores, publishes, and emails on its own). Set it up
once in the order below.

Your folder: `C:\Users\newbp\Downloads\ipo-ledger`
Your Python: `py -V:3.12`

---

## STEP 0 - Put everything in one folder

1. Extract `ipo-ledger.zip` into `C:\Users\newbp\Downloads\ipo-ledger`.
2. Copy your working scraper in as `code.py` (it must live here, this is where
   the scheduler runs it and where `active_ipos_v13.xlsx` gets saved):
   ```
   copy "C:\Users\newbp\OneDrive\Pictures\ml submission\final.py" "C:\Users\newbp\Downloads\ipo-ledger\code.py"
   ```
3. Put your dataset `GMP_ML_READY_FINAL_v3.xlsx` in the same folder.

Open a terminal in the folder for everything below:
```
cd C:\Users\newbp\Downloads\ipo-ledger
```

---

## STEP 1 - Install the Python packages (one time)

```
py -V:3.12 -m pip install fastapi uvicorn pydantic selenium webdriver-manager beautifulsoup4 pandas openpyxl requests yfinance xgboost lightgbm scikit-learn
```

---

## STEP 2 - First manual run (prove the pipeline works before automating)

```
py -V:3.12 code.py
py -V:3.12 publish.py
py -V:3.12 -m uvicorn app:app --reload
```
Open http://127.0.0.1:8000 . You should see the live IPOs with calls. Leave
this terminal running (it is the website). Ctrl+C stops it.

If `code.py` printed `Found 0 total`, re-run it; the list table is slow to
load. The `[3]` line should show GMP rows, and the result table should list
your active IPOs.

---

## STEP 3 - Email alerts (Gmail)

1. Use the alerts Gmail account `ipopredict@gmail.com`.
2. Turn on 2-Step Verification on that account, then create an **App Password**
   (Google Account -> Security -> App passwords). It is a 16-character code.
3. You will paste it into `run_scheduler.bat` in Step 6. It stays on your
   laptop only; never upload it.

Recipients sign up themselves on the site's **Alerts** page (email + tier). No
login needed.

---

## STEP 4 - Telegram alerts (optional, recommended)

1. In Telegram, open **@BotFather**, send `/newbot`, follow the prompts, copy
   the token (looks like `123456789:ABC-Def...`).
2. You will paste this token into `run_scheduler.bat` in Step 6 as
   `TELEGRAM_BOT_TOKEN`. The same token powers the alerts.
3. To let people connect, run the helper once:
   ```
   set TELEGRAM_BOT_TOKEN=123456789:ABC-your-token
   py -V:3.12 telegram_bot.py
   ```
   It prints your bot link (`https://t.me/yourbot`). Anyone who opens it and
   taps **Start** instantly gets a reply with their chat ID. They paste that
   into the **Telegram field** on the Alerts page and pick a tier.
4. Telegram rule: a bot cannot message someone until they have messaged it
   first, which is why the Start step matters. You only need `telegram_bot.py`
   running while people are onboarding, not 24/7.

After this, every alert goes to email AND Telegram automatically.

---

## STEP 5 - Gemini (optional - leave OFF for launch)

The Ask agent answers from the data with built-in logic and does not need
Gemini. Gemini is only an optional question-parser (it never writes answers,
so it cannot invent IPO facts). Turn it on later if you want sharper handling
of complex analytic questions:

1. Get a free key at https://aistudio.google.com (no card, 1,500 requests/day).
2. Add `set GEMINI_API_KEY=your-key` to `run_scheduler.bat`.

That is all. Skip this for tomorrow.

---

## STEP 6 - Configure run_scheduler.bat

Open `run_scheduler.bat` and set the lines at the top. Use your real values:

```bat
cd /d C:\Users\newbp\Downloads\ipo-ledger
set FAST_EVERY_MIN=30
set MARKET_OPEN=9
set MARKET_CLOSE=16
set GMAIL_USER=ipopredict@gmail.com
set GMAIL_APP_PASS=your-16-char-app-password
set TELEGRAM_BOT_TOKEN=123456789:ABC-your-token
rem set GEMINI_API_KEY=your-key   (optional, leave commented for launch)
py -V:3.12 scheduler.py
```

Test it by double-clicking the .bat (or running it in a terminal). You should
see heartbeat lines, and it writes a log to `scheduler.log` in the folder.
Ctrl+C to stop the test.

---

## STEP 7 - Make the scheduler run automatically (Task Scheduler)

1. Open **Task Scheduler** -> Create Task.
2. General: name it "IPOPredict". Tick "Run only when user is logged on".
3. Triggers -> New -> "On a schedule" -> Daily -> then tick "Repeat task every
   30 minutes" for a duration of "1 day". Set the start time to 9:00 AM.
4. Actions -> New -> Program/script: browse to `run_scheduler.bat`.
   Set "Start in" to `C:\Users\newbp\Downloads\ipo-ledger`.
5. Conditions: untick "Start only on AC power" if you want it on battery.
6. Save.

Now the scheduler wakes every 30 minutes on its own.

---

## How often it runs and mails

- **Scrapes/refreshes:** every 30 minutes during 9 AM-4 PM. The site numbers
  update about every half hour. It does NOT email every tick.
- **Emails/Telegram:** only at three windows on an IPO's CLOSING day - about
  9:30 AM, 1:30 PM, and 3:00 PM - at most once each per qualifying IPO per
  person. Plus one "closes tomorrow" heads-up the evening before.
- So one closing IPO = up to 3 alerts that day. If two IPOs close the same day
  and both clear someone's tier, that person gets up to 6 that day.

---

## Daily flow once it is all on (nothing for you to do)

1. Scheduler scrapes the latest IPOs and subscription/GMP numbers.
2. `publish.py` scores them and writes `qualified_ipos.json` (and filters out
   SME IPOs).
3. The website (must be running) serves the fresh JSON, so the site updates.
4. On a closing day, the staged alerts go out at 9:30 / 13:30 / 15:00.
5. After an IPO lists, the actual price is fed back into the history.

Keep BOTH running for the full experience:
- Website: `py -V:3.12 -m uvicorn app:app` (or with `--reload` while testing).
- Scheduler: the Task Scheduler task (or `run_scheduler.bat`).

---

## Verify checklist

- [ ] `py -V:3.12 code.py` prints `Found N total` (N >= 2) and GMP rows.
- [ ] `py -V:3.12 publish.py` writes `qualified_ipos.json`.
- [ ] Site at http://127.0.0.1:8000 shows the IPOs, calls, and the
      "Closed, awaiting listing" section for any IPO past its close date.
- [ ] Alerts page accepts an email + tier (you get "Done").
- [ ] (Telegram) Tapping Start on the bot replies with a chat ID.
- [ ] `run_scheduler.bat` runs and writes `scheduler.log`.
- [ ] Task Scheduler task is enabled, repeats every 30 min, Start-in is the folder.

---

## Quick troubleshooting

- **Site shows old data:** the scheduler updated the JSON but the website was
  not running, or your browser cached it. Hard-refresh (Ctrl+Shift+R).
- **No emails:** `GMAIL_USER`/`GMAIL_APP_PASS` not set in the .bat, or no IPO
  qualifies at the subscriber's tier today, or it is not a closing day.
- **No Telegram:** the person never tapped Start, or the chat ID is wrong, or
  `TELEGRAM_BOT_TOKEN` is not set.
- **`Found 0 total`:** re-run; the list table loads slowly. Persisting zero with
  a "blocked" note means wait and retry later.
- **Scheduler seems dead:** open `scheduler.log` - it logs a heartbeat every tick.
