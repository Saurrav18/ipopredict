# IPOPredict - two builds in one, and why

This project runs in two modes from a single codebase. Flip one line in `.env`:

    COMPLIANCE_MODE=0   ->  FULL build (APPLY/SKIP, scores, ranges, accuracy tiers, alerts)
    COMPLIANCE_MODE=1   ->  SEBI-SAFE build (public facts + education only)

Restart `run_web.bat` and `run_scheduler.bat` after changing it. Preview both
offline with `demo.html` (full) and `demo_compliant.html` (SEBI-safe).

Note: this is a plain-English summary of how the app is built to reduce risk. It
is not legal advice or a SEBI sign-off. It reflects the 2024-25 Research Analyst
rules and the finfluencer circular as they apply to this app.

---

## Why a compliant build exists

In India, giving public buy/sell-style calls on specific securities is a
"research service" under SEBI's Research Analyst Regulations. The 2024 definition
covers buy/sell/hold recommendations, price targets, and opinions on securities
**including IPOs**, and "consideration" includes non-cash benefit. The 2024
finfluencer circular also bars unregistered educators from recommending specific
securities, from using recent market data on a named security to imply a future
price, and from making return/performance claims.

The FULL build does exactly those flagged things (that is what makes it useful),
so it is for **personal / private use**. The COMPLIANCE build strips them so the
public site stays on the safe side of the line: an IPO **data + education** tool.

---

## What each SEBI flag is, and how COMPLIANCE_MODE handles it

1. APPLY / SKIP verdict on a named IPO (buy recommendation)
   -> removed. No verdict is computed into the public data at all.

2. Accuracy tiers + win-rate % (performance claim)
   -> removed from the public UI. The tier slider is hidden; the "live record"
      and returns tables on the method page are hidden.

3. Live GMP/subscription used to predict a named IPO's move (specifically barred)
   -> the public site shows GMP and subscription only as **reported facts**, with
      no forward inference attached to the IPO.

4. Listing-gain range (a price target)
   -> removed from the public data and detail page.

5. Alerts pushing IPO calls to a list (distribution of research)
   -> forward alerts are disabled; the alert sender stands down in this mode.

6. The "Ask" agent answering "should I apply to X"
   -> keep it educational; it should explain concepts, not recommend a security.

7. Charging money -> turns grey into clear-cut. Keep the compliant build FREE.

8. "predict IPOs" branding -> the compliant UI reframes to "track & understand".

### What stays (genuinely fine)
- Factual data on current/upcoming IPOs (price, dates, size, sector, reported
  subscription/GMP, fundamentals).
- Education on how IPOs, GMP, subscription, P/E, allotment work.
- Sector- and factor-level historical patterns (SEBI exempts sector/index-level
  analysis). This is the "Patterns" page.
- Factual listing outcomes of past IPOs ("LISTED +X%"), shown as facts, not as
  "our model's hit/miss".

---

## Enforcement is at the data layer (not just the UI)

When `COMPLIANCE_MODE=1`, `publish.py` removes every forward-looking field
(prediction, win_score, qualifying_tiers, confidence, range, cases, tier flags)
before writing `qualified_ipos.json`. So even if a UI element were missed, there
is simply no forward-looking data in the file for the browser to show. The
frontend also detects the mode (`/config` + a per-record flag) and renders
factual-only cards, hides the tier slider, alerts, archive hit/miss, and the
method-page performance tables, and shows the not-advice / not-SEBI-registered
disclaimers.

---

## Your options, to choose from later

- Personal / private use of the FULL build: safest, no registration.
- Public COMPLIANCE build, free, low-profile: much lower risk, still not a formal
  shield - keep it free and do not advertise.
- Register as a Research Analyst (graduate + NISM-Series-XV + deposit + website +
  AI-use disclosure) to run the FULL build publicly and legally.

Residual items to tighten if you go fully public on the compliance build:
the deep-link `page_*.html` snapshots and the `/ask` agent's answers should also
be regenerated/constrained in compliance mode (ask me to finish those when you
decide to launch).
