"""Regenerate the embedded ARCHIVE in index.html from the training dataset.

The website's comparison feature and Archive page use a baked-in ARCHIVE array
(331+ past IPOs). It is embedded rather than fetched so the site works without a
heavy backend (your local Python 3.14 cannot load pandas). This script rebuilds
that array from the latest dataset, so when new IPOs are appended (via
ipo_update.append_listing), they join the comparison pool too.

Columns (index): name 0, date 1, sector 2, offerRs 3, gmp% 4, demand x 5,
day-1 % 6, P/E 7, ROE 8, promoter 9, OFS% 10, fresh% 11, size cr 12, sentiment 13

Run:  python build_archive.py            # rewrites index.html in place
      python build_archive.py --json     # also writes archive.json (for an API)
"""
import os, re, json, sys

DATASET   = os.environ.get("IPO_DATASET",
            "GMP_ML_READY_FINAL_v3.xlsx")
INDEX     = os.environ.get("INDEX_HTML", "index.html")

# Map full sector names to the short codes the archive/UI use.
SECTOR_CODE = {
    "manufacturing": "MFG", "pharma/healthcare": "PHARMA", "pharma": "PHARMA",
    "fmcg/consumer": "FMCG", "fmcg": "FMCG", "technology": "TECH", "tech": "TECH",
    "bfsi": "BFSI", "real estate/infra": "REALTY", "realty": "REALTY",
    "energy": "ENERGY", "hospitality": "HOSP", "logistics": "LOGI",
    "agriculture": "AGRI", "agri": "AGRI", "education": "EDU",
    "telecom": "TELECOM", "reit/invit": "REIT", "reit": "REIT",
}


def code_for(sector):
    if not sector:
        return "OTHER"
    return SECTOR_CODE.get(str(sector).strip().lower(), str(sector).strip().upper()[:8])


def build_rows():
    import pandas as pd
    df = pd.read_excel(DATASET)

    def g(row, col):
        v = row.get(col)
        try:
            if v is None or pd.isna(v):
                return None
            return round(float(v), 1)
        except (TypeError, ValueError):
            return None

    def ym(row):
        # date as "YYYY-MM" for the archive's date column
        for c in ("Date", "Listing Date", "listing_date"):
            if c in df.columns and pd.notna(row.get(c)):
                try:
                    return pd.to_datetime(row.get(c), dayfirst=True).strftime("%Y-%m")
                except Exception:
                    pass
        return ""

    rows = []
    for _, row in df.iterrows():
        name = str(row.get("IPO_Name", "")).replace(" IPO", "").strip()
        if not name:
            continue
        rows.append([
            name,
            ym(row),
            code_for(row.get("sector")),
            g(row, "Offer Price"),
            g(row, "gmp_closing_gain_pct"),
            g(row, "Total"),
            g(row, "Listing Gain"),
            g(row, "pre_issue_pe"),
            g(row, "roe_ronw"),
            g(row, "promoter_holding_post_ipo"),
            g(row, "ofs_pct"),
            g(row, "fresh_issue_pct"),
            g(row, "Issue_Size(crores)"),
            g(row, "market_sentiment_ratio"),
            g(row, "QIB"),
            g(row, "HNI"),
            g(row, "RII"),
            g(row, "List Price"),
        ])
    # newest first (matches how the Archive page reads)
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows


def write_into_index(rows):
    src = open(INDEX, encoding="utf-8").read()
    arch_str = "const ARCHIVE = " + json.dumps(rows, separators=(",", ":")) + ";"
    new_src, n = re.subn(r"const ARCHIVE = \[\[.*?\]\];", lambda _: arch_str, src, flags=re.S)
    if n != 1:
        raise SystemExit(f"Expected exactly one ARCHIVE constant, found {n}. Aborting.")
    open(INDEX, "w", encoding="utf-8").write(new_src)
    return len(rows)


def main():
    rows = build_rows()
    n = write_into_index(rows)
    with_funds = sum(1 for r in rows if r[7] is not None or r[8] is not None)
    print(f"Archive rebuilt: {n} IPOs written into {INDEX} ({with_funds} with fundamentals).")
    if "--json" in sys.argv:
        json.dump(rows, open("archive.json", "w"), separators=(",", ":"))
        print(f"Also wrote archive.json ({n} rows) for an optional /archive API.")


if __name__ == "__main__":
    main()
