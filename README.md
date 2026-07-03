# IPO Advisor, autonomous scrape-to-decision system for Indian IPOs

Predicts listing-day outcomes for Indian mainboard IPOs and explains every
call in plain English. Runs end to end without manual steps: scrape live
GMP/subscription data, score through a validated ensemble, calibrate
confidence, qualify against an accuracy slider, alert.

Built and validated on 331 mainboard IPOs (Sept 2020 - May 2026).

## Headline numbers (all walk-forward, 4 time splits)

| What | Result |
|---|---|
| Apply/skip win rate | 93.8 - 96.6% across splits, ~95% avg |
| Avg listing gain on picks | +24% |
| Prediction range coverage (on picks) | 88.5% at ~34pt width |
| Decision confidence calibration | 90+ bucket = 95.5-100% correct |
| Calibration agent vs brute force | 34/34 tiers identical, 42x fewer evals |

Live validation: CMR Green predicted APPLY conf 98, listed +31.8%.

## How it works

```
scraper (code.py, laptop, Indian IP) --> active_ipos_v13.xlsx
publish.py   reads the xlsx, trains the ensemble, writes qualified_ipos.json
    4 signals: apply/skip ensemble, conformal range, big-win prob, allotment math
    + decision confidence + nearest-neighbour context
    + slider tiers 65-98% from alert_calibration.json
app.py (FastAPI)   serves the site, the /ask agent, sign-in, subscriptions

scheduler.py   automates scrape -> publish -> update and sends staged alerts
ipo_update.py  on listing day grades the result; every 10 listings it retrains
               and recalibrates (ipo_calibration_agent.py), then rebuilds the archive
Q&A:           ipo_research_agent.py (rules first, optional Gemini refinement)
alerts:        send_alerts.py (email) + telegram_bot.py (Telegram)
```

## The agentic parts

- **Calibration agent**: finds best (model, threshold, GMP/QIB/SUB floors) per
  accuracy tier via bisection inside a coordinate walk. Verified 34/34 against
  exhaustive 21,600-combo search at 42x fewer evaluations.
- **Research agent**: Q&A over the dataset. Rules parse the question first and
  cover most queries; an optional Gemini key refines the harder ones. Execution
  stays rule-based, so answers are deterministic and grounded in the data.
- **Learning loop**: when an IPO lists, its real listing price is fetched, sanity
  and name checked, and graded into the dataset; every 10 new listings the models
  retrain and the accuracy tiers recalibrate, then the archive rebuilds.

## Honest limitations

- Range coverage is 75% overall / 88% on picks, not 90%+. Listing gains span
  -25% to +140%; tighter honest intervals are not possible at this width.
- High slider tiers (95-98) rest on ~40-50 historical picks. Every new IPO is
  an out-of-sample test and the system logs them.
- Scrapers need an Indian residential IP (sources block datacenter IPs), so
  scraping runs locally and pushes JSON; the API serves from anywhere.

## Run it

```
pip install -r requirements.txt
python code.py                  # scrape live IPOs (run on laptop, Indian IP)
python publish.py               # score + write qualified_ipos.json
python ipo_update.py            # grade listings, retrain every 10, recalibrate
uvicorn app:app                 # serve the site + API
```

See SETUP_END_TO_END.md for the full laptop setup (scheduler, Gmail, Telegram).

## License

MIT (see [LICENSE](LICENSE)). Not investment advice; not a SEBI-registered
advisory service. Educational and engineering demonstration only.
