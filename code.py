"""
IPO SCRAPER v13.2 — list-step fix:
  Chittorgarh's report list table is loaded by JavaScript/AJAX ("Loading...
  Total Records: 0" in the raw HTML). v13.2 waits for that table to actually
  render, and if it never does (blocked / slow), falls back to the homepage,
  which lists the current mainboard IPOs in plain server-rendered HTML.
  Everything else is unchanged from v13.
"""
import subprocess, sys
for pkg in ["selenium","webdriver-manager","beautifulsoup4","pandas","openpyxl","requests","yfinance"]:
    try: __import__(pkg.replace("-","_"))
    except ImportError:
        subprocess.run([sys.executable,"-m","pip","install","-q",pkg])

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
import time, re, math, pandas as pd, requests
from datetime import datetime, date

MONTHS = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
          "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}

def make_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1400,900")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches",["enable-automation"])
    opts.add_experimental_option("useAutomationExtension",False)
    opts.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36")
    svc = Service(ChromeDriverManager().install())
    drv = webdriver.Chrome(service=svc, options=opts)
    # extra stealth: hide the navigator.webdriver flag some sites check for bots
    try:
        drv.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"})
    except Exception:
        pass
    return drv

def polite_pause(a=1.5, b=4.0):
    """Small randomized delay between page loads so requests look human-paced.
    At ~6 pages a few times a day this keeps us well under any rate limit and
    reduces the chance a datacenter IP (VPS) gets challenged/blocked."""
    import random, time
    time.sleep(random.uniform(a, b))

def sf(s):
    if s is None: return None
    try: return float(re.sub(r"[^\d.\-]","",str(s).strip()) or "")
    except: return None

def parse_close_date(s):
    """Parse '9 Jun, 2026' or '5 May, 2026' -> date object. Returns None if fails."""
    if not s: return None
    s = re.sub(r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s*','',s).strip()
    m = re.search(r'(\d+)\s+([A-Za-z]+),?\s*(\d{4})', s)
    if m:
        try:
            day=int(m.group(1)); mon=MONTHS.get(m.group(2)[:3].lower()); yr=int(m.group(3))
            if mon: return date(yr,mon,day)
        except: pass
    return None

def is_active(close_str, listing_str):
    """Return True if IPO closed within last 14 days OR listing is in future."""
    today = date.today()
    close_d = parse_close_date(close_str)
    listing_d = parse_close_date(listing_str)
    if close_d and (today - close_d).days <= 14: return True
    if listing_d and listing_d >= today: return True
    return False

def parse_table_rows(soup):
    rows = {}
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all(["td","th"])]
            if len(cells)>=2 and cells[0]:
                rows[cells[0].strip()] = cells[1:]
    return rows

# ---- STEP 1: chittorgarh mainboard list (v13.2 - AJAX-aware + fallback) ----
_LINK_RE = re.compile(r'/ipo/([a-z0-9\-]+)/(\d+)')
_EXCLUDE = ["compare","review","recommendation","allotment","faq","news",
            "basis","anchor","subscription"]

def _parse_ipo_links(html):
    soup = BeautifulSoup(html or "", "html.parser")
    ipos, seen = [], set()
    for a in soup.find_all("a", href=True):
        m = _LINK_RE.search(a["href"])
        if not m: continue
        slug, cid = m.group(1), m.group(2)
        if any(x in slug for x in _EXCLUDE): continue
        if cid in seen: continue
        seen.add(cid)
        name = a.get_text(strip=True)
        if not name or len(name) < 3: continue
        ipos.append({"IPO_Name":name,"cg_slug":slug,"cg_id":cid,
                     "cg_url":f"https://www.chittorgarh.com/ipo/{slug}/{cid}/"})
    return ipos

def get_mainboard_list(driver):
    print("\n[1] chittorgarh mainboard list ...")
    # --- attempt 1: the report page (full list, but the table is JS/AJAX) ---
    driver.get("https://www.chittorgarh.com/report/ipo-in-india-list-main-board-sme/82/mainboard/")
    html = ""
    for _ in range(16):                       # poll up to ~32s for the table to fill in
        time.sleep(2)
        html = driver.page_source or ""
        tail = html.split("Total Records")[-1][:120].lower()   # area right after the count
        if len(_parse_ipo_links(html)) >= 2 and "loading" not in tail:
            break                              # table has actually rendered
    ipos = _parse_ipo_links(html)
    low = html.lower()
    blocked = (len(ipos) < 2) and any(k in low for k in
              ["captcha","access denied","are you a human","too many requests","unusual traffic"])
    print(f"  [report] page={len(html)}B  parsed={len(ipos)}"
          + ("  *** PAGE LOOKS BLOCKED ***" if blocked else ""))
    if len(ipos) >= 2:
        print(f"  Found {len(ipos)} total")
        return ipos

    # --- attempt 2: homepage, MAINBOARD section only (server-rendered HTML) ---
    driver.get("https://www.chittorgarh.com/")
    time.sleep(5)
    html = driver.page_source or ""
    mb = html.find("Mainboard IPOs &amp; FPOs")
    if mb < 0: mb = html.find("Mainboard IPOs & FPOs")
    sme = html.find("SME IPOs &amp; FPOs")
    if sme < 0: sme = html.find("SME IPOs & FPOs")
    section = html[mb:sme] if (mb >= 0 and sme > mb) else html   # isolate mainboard table
    ipos = _parse_ipo_links(section)
    print(f"  [homepage] mainboard-section parsed={len(ipos)}")
    if ipos:
        print(f"  Found {len(ipos)} total (homepage fallback - the most recent few)")
        return ipos

    print("  Found 0 total")
    return []

# ---- STEP 2: scrape chittorgarh detail ----
def parse_listed_info(rows, r):
    """For an IPO that has ALREADY listed, Chittorgarh shows the trading ticker
    and a 'Listing Day Trading Information' table. Pull the NSE/BSE symbol and
    the real listing-day open, and stamp the true Listing Gain. All optional -
    if the page doesn't have these (still-open IPO), nothing changes."""
    # ticker: row "BSE Script Code / NSE Symbol" -> "544802 / CORDELIA"
    for k in list(rows.keys()):
        if "NSE Symbol" in k or "Script Code" in k:
            m = re.search(r'(\d{5,6})\s*/\s*([A-Za-z0-9&\-]+)', " ".join([k]+rows[k]))
            if m:
                r["_bse_code"] = m.group(1)
                r["_symbol"]   = m.group(2).upper()
            break
    # real listing-day open + gain, straight from the listing table (NSE preferred)
    if "Final Issue Price" in rows and "Open" in rows:
        fip = rows["Final Issue Price"]
        issue = sf(fip[-1]) or sf(fip[0]) or r.get("Offer Price")
        opn = rows["Open"]
        nse_open = sf(opn[-1]) if len(opn) >= 2 else None
        bse_open = sf(opn[0]) if opn else None
        lo = nse_open or bse_open
        if lo and issue:
            r["_listing_open"] = lo
            r["Listing Gain"]  = round((lo - issue) / issue * 100, 2)
            r["_status"]       = "listed"
    return r

def scrape_detail(driver, ipo):
    driver.get(ipo["cg_url"]); time.sleep(5)
    soup=BeautifulSoup(driver.page_source,"html.parser")
    rows=parse_table_rows(soup)
    text=soup.get_text(" ",strip=True)
    r=dict(ipo)

    # Dates
    if "IPO Date" in rows:
        parts=re.split(r'\s+to\s+',rows["IPO Date"][0])
        if len(parts)==2: r["open_date"]=parts[0].strip(); r["Close_Date"]=parts[1].strip()
    if "Listing Date" in rows:
        r["_listing_date"]=rows["Listing Date"][0].strip()
    if "Listed on" in rows and not r.get("_listing_date"):
        r["_listing_date"]=rows["Listed on"][0].strip()   # already-listed page uses this label

    # ---- ACTIVE FILTER ----
    if not is_active(r.get("Close_Date",""), r.get("_listing_date","")):
        return None

    # Price Band
    if "Price Band" in rows:
        m=re.search(r'\u20b9(\d+)\s+to\s+\u20b9(\d+)',rows["Price Band"][0])
        if m: r["_lower_band"]=sf(m.group(1)); r["Offer Price"]=sf(m.group(2))

    # Issue size
    for label in ["Total Issue Size","Issue Size"]:
        if label in rows:
            m=re.search(r'\u20b9([\d,.]+)\s*Cr'," ".join(rows[label]))
            if m: r["Issue_Size(crores)"]=sf(m.group(1).replace(",","")); break

    # OFS/Fresh
    if "Sale Type" in rows:
        st=rows["Sale Type"][0].lower()
        if "ofs" in st and r.get("Issue_Size(crores)"):
            r["_ofs_cr"]=r["Issue_Size(crores)"]; r["_fresh_cr"]=0.0
        elif "fresh" in st and r.get("Issue_Size(crores)"):
            r["_fresh_cr"]=r["Issue_Size(crores)"]; r["_ofs_cr"]=0.0
    for label in ["Offer for Sale","OFS"]:
        if label in rows:
            m=re.search(r'\u20b9([\d,.]+)\s*Cr'," ".join(rows[label]))
            if m: r["_ofs_cr"]=sf(m.group(1).replace(",",""))
    for label in ["Fresh Issue","Fresh"]:
        if label in rows:
            m=re.search(r'\u20b9([\d,.]+)\s*Cr'," ".join(rows[label]))
            if m: r["_fresh_cr"]=sf(m.group(1).replace(",",""))

    # Subscription
    for label,key in [("QIB (Ex Anchor)","QIB"),("QIB","QIB"),
                       ("NII","HNI"),("NII (HNI)","HNI"),
                       ("Retail","RII"),("Total","Total")]:
        if label in rows and key not in r:
            v=sf(rows[label][0])
            if v is not None: r[key]=v

    # Financials
    for label in ["P/E (x)","P/E","PE (x)"]:
        if label in rows: r["pre_issue_pe"]=sf(rows[label][0]); break
    if "ROE" in rows: r["roe_ronw"]=sf(rows["ROE"][0])
    for label in ["Debt/Equity","D/E"]:
        if label in rows: r["_de"]=sf(rows[label][0]); break
    if "Promoter Holding" in rows:
        vals=rows["Promoter Holding"]
        r["promoter_holding_post_ipo"]=sf(vals[1] if len(vals)>1 else vals[0])
    if "Lot Size" in rows: r["_lot_size"]=sf(rows["Lot Size"][0])

    # Sector from peer keywords
    # Order matters - more specific first, broad keywords last
    PEER_MAP=[
        # Manufacturing (specific peers first)
        ("nutrition","FMCG/Consumer"),
        ("hindalco","Manufacturing"),("tata steel","Manufacturing"),
        ("jsw steel","Manufacturing"),("vedanta","Manufacturing"),
        ("aluminium recycling","Manufacturing"),("scrap metal","Manufacturing"),
        ("aluminium","Manufacturing"),("steel","Manufacturing"),
        ("cable","Manufacturing"),("wire","Manufacturing"),("pipe","Manufacturing"),
        ("auto component","Manufacturing"),("precision engineering","Manufacturing"),
        # Pharma/Healthcare (specific)
        ("sun pharma","Pharma/Healthcare"),("cipla","Pharma/Healthcare"),
        ("dr reddy","Pharma/Healthcare"),("divis","Pharma/Healthcare"),
        ("aurobindo","Pharma/Healthcare"),("ipca","Pharma/Healthcare"),
        ("hospital","Pharma/Healthcare"),("diagnostic","Pharma/Healthcare"),
        ("pharmaceutical","Pharma/Healthcare"),
        # FMCG/Consumer
        ("zydus wellness","FMCG/Consumer"),("nestle","FMCG/Consumer"),
        ("marico","FMCG/Consumer"),("dabur","FMCG/Consumer"),
        ("emami","FMCG/Consumer"),("britannia","FMCG/Consumer"),
        ("tata consumer","FMCG/Consumer"),("hindustan unilever","FMCG/Consumer"),

        # BFSI (specific - avoid broad matches)
        ("hdfc bank","BFSI"),("bajaj finance","BFSI"),("muthoot","BFSI"),
        ("microfinance","BFSI"),("small finance bank","BFSI"),
        ("life insurance","BFSI"),("general insurance","BFSI"),
        # Technology
        ("infosys","Technology"),("tcs","Technology"),("wipro","Technology"),
        ("hcl tech","Technology"),("mphasis","Technology"),
        # Energy
        ("ntpc","Energy"),("power grid","Energy"),("tata power","Energy"),
        ("solar energy","Energy"),("renewable energy","Energy"),
        # Real Estate
        ("dlf","Real Estate/Infra"),("godrej properties","Real Estate/Infra"),
        ("prestige","Real Estate/Infra"),("brigade","Real Estate/Infra"),
        # Logistics
        ("delhivery","Logistics"),("blue dart","Logistics"),
        ("container corporation","Logistics"),
    ]
    tl=text.lower()
    m=re.search(r'(?:Sector|Industry)[:\s]+([A-Za-z /&,]+?)(?:\s{3,}|\n|\|)',text)
    if m: r["sector"]=m.group(1).strip()
    else:
        for kw,sec in PEER_MAP:
            if kw in tl: r["sector"]=sec; break

    # if it has already listed, capture ticker + real listing-day result
    parse_listed_info(rows, r)

    print(f"  [ok] {r['IPO_Name'][:40]}")
    print(f"     Rs {r.get('Offer Price')} | close={r.get('Close_Date')} | "
          f"listing={r.get('_listing_date')}")
    if r.get("_status") == "listed":
        print(f"     [LISTED] {r.get('_symbol')} open Rs {r.get('_listing_open')} "
              f"-> listing gain {r.get('Listing Gain')}%")
    print(f"     QIB={r.get('QIB')}x HNI={r.get('HNI')}x "
          f"RII={r.get('RII')}x Total={r.get('Total')}x")
    print(f"     PE={r.get('pre_issue_pe')} ROE={r.get('roe_ronw')}% "
          f"promo={r.get('promoter_holding_post_ipo')}% sector={r.get('sector')}")
    return r

# ---- STEP 3: GMP from investorgain (v13.2 - load table once, AJAX-aware) ----
# The live-GMP table is JS/AJAX ("0 records / No data available" in raw HTML)
# until the script fills it. We poll until it renders, parse every row once,
# then look up each IPO by name. GMP shows in a cell as "Rs 6.5(14.44%) ...".
_GMP_RE = re.compile(r'\u20b9\s*([\d.]+)\s*\(\s*([\d.\-]+)\s*%\)')
_GMP_CACHE = {"rows": None}

def _load_gmp_rows(driver):
    """Load investorgain live GMP once; cache + return [(row_text_lower, gmp_val, gmp_pct)]."""
    if _GMP_CACHE["rows"] is not None:
        return _GMP_CACHE["rows"]
    print("\n[3] investorgain live GMP table ...")
    rows = []
    for url in ["https://www.investorgain.com/report/live-ipo-gmp/331/ipo/",
                "https://www.investorgain.com/report/live-ipo-gmp/331/"]:
        try:
            driver.get(url)
            html = ""
            for _ in range(16):                       # poll up to ~32s for the AJAX table
                time.sleep(2)
                html = driver.page_source or ""
                if "No data available" not in html and _GMP_RE.search(html):
                    break
            soup = BeautifulSoup(html, "html.parser")
            for tr in soup.find_all("tr"):
                cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td","th"])]
                if len(cells) < 2:
                    continue
                row_text = " ".join(cells)
                m = _GMP_RE.search(row_text)              # Rs X(Y%) only lives in the GMP cell
                if m:
                    rows.append((row_text.lower(), float(m.group(1)), float(m.group(2))))
            tag = url.rstrip("/").rsplit("/", 1)[-1] or "list"
            print(f"  [gmp-table] {tag}: {len(rows)} rows with GMP  (page={len(html)}B)")
            if rows:
                break
        except Exception as e:
            print(f"  [gmp-table] error: {e}")
    _GMP_CACHE["rows"] = rows
    if not rows:
        print("  [gmp-table] no GMP rows rendered (grey market may be quiet, or table slow)")
    return rows

def get_gmp(driver, name, offer_price):
    rows = _load_gmp_rows(driver)
    words = [w for w in re.sub(r'[^a-z0-9 ]+', ' ', name.lower()).split() if len(w) > 2]
    words = [w for w in words if w not in ("ipo", "ltd", "the", "and", "india")][:4]
    for row_text, gval, gpct in rows:
        hits = sum(1 for w in words if w in row_text)
        first_ok = bool(words) and len(words[0]) >= 4 and words[0] in row_text
        if hits >= 2 or first_ok:                 # 2+ name words, or a strong lead word
            print(f"  [GMP] {name}: Rs {gval} ({gpct}%)")
            return {"gmp_closing_day": gval, "gmp_closing_gain_pct": gpct}
    print(f"  [GMP] {name}: not in GMP table - using 0")
    return {"gmp_closing_day": 0.0, "gmp_closing_gain_pct": 0.0}

# ---- STEP 4: Nifty features via yfinance (no auth needed) ----
def get_nifty_features():
    print("\n[4] Nifty features via yfinance ...")
    try:
        import yfinance as yf
        nifty = yf.download("^NSEI", period="6mo", interval="1d",
                            progress=False, auto_adjust=True)
        if nifty.empty: raise Exception("No data")
        prices = [float(x) for x in nifty["Close"].dropna().values.flatten()]
        if len(prices) < 22: raise Exception("Not enough data")
        curr=prices[-1]; m1=prices[-22]
        m3=prices[-63] if len(prices)>=63 else prices[0]
        r1m=curr/m1; r1m3m=(curr/m1)/(curr/m3) if m3 else 1.0
        rets=[(prices[i]-prices[i-1])/prices[i-1]
              for i in range(max(1,len(prices)-30),len(prices))]
        vol=math.sqrt(sum((x-sum(rets)/len(rets))**2
            for x in rets)/len(rets))*math.sqrt(252)*100 if rets else 15.0
        trend=2 if r1m>1.02 else(1 if r1m>1.0 else(-1 if r1m<0.98 else 0))
        print(f"  Nifty={curr:.0f} 1m_ratio={r1m:.3f} vol={vol:.1f}% trend={trend}")
        return {"market_sentiment_ratio":round(r1m,4),
                "ratio_1m_vs_3m":round(r1m3m,4),
                "nifty_volatility_30d":round(vol,4),
                "trend_score":trend}
    except Exception as e:
        print(f"  yfinance failed: {e} - using recent dataset averages")
    return {"market_sentiment_ratio":1.08,"ratio_1m_vs_3m":1.05,
            "nifty_volatility_30d":21.5,"trend_score":0}

# ---- FINALISE ----
def finalise(row, mkt):
    issue=row.get("Issue_Size(crores)")
    fresh=row.get("_fresh_cr"); ofs=row.get("_ofs_cr")
    fp=op=None
    if issue and issue>0:
        if fresh is not None: fp=round(float(fresh)/issue*100,1)
        if ofs   is not None: op=round(float(ofs)/issue*100,1)
    if fp is not None and op is None: op=round(100-fp,1)
    if op is not None and fp is None: fp=round(100-op,1)
    offer=row.get("Offer Price")
    gd=row.get("gmp_closing_day"); gp=row.get("gmp_closing_gain_pct")
    if gd is not None and offer and not gp:
        gp=round(float(gd)/float(offer)*100,2)
    return {
        "Date":                      datetime.today().strftime("%d/%m/%y"),
        "IPO_Name":                  row.get("IPO_Name",""),
        "Issue_Size(crores)":        issue,
        "QIB":                       row.get("QIB"),
        "HNI":                       row.get("HNI"),
        "RII":                       row.get("RII"),
        "Total":                     row.get("Total"),
        "Offer Price":               offer,
        "List Price":                row.get("_listing_open"),
        "Listing Gain":              row.get("Listing Gain"),
        "CMP(BSE)":                  None,
        "CMP(NSE)":                  None,
        "Current Gains":             None,
        "Close_Date":                row.get("Close_Date"),
        "gmp_closing_day":           gd,
        "gmp_closing_gain_pct":      gp,
        "gmp_closing_est_price":     round(float(offer)+(gd or 0),2) if offer else None,
        "gmp_date_used":             datetime.today().strftime("%Y-%m-%d"),
        "market_sentiment_ratio":    mkt.get("market_sentiment_ratio"),
        "ratio_1m_vs_3m":            mkt.get("ratio_1m_vs_3m"),
        "nifty_volatility_30d":      mkt.get("nifty_volatility_30d"),
        "trend_score":               mkt.get("trend_score"),
        "pre_issue_pe":              row.get("pre_issue_pe"),
        "roe_ronw":                  row.get("roe_ronw"),
        "ofs_pct":                   op,
        "fresh_issue_pct":           fp,
        "promoter_holding_post_ipo": row.get("promoter_holding_post_ipo"),
        "sector":                    row.get("sector"),
        "_de":                       row.get("_de"),
        "_lot_size":                 row.get("_lot_size"),
        "_open_date":                row.get("open_date"),
        "_listing_date":             row.get("_listing_date"),
        "_symbol":                   row.get("_symbol"),
        "_bse_code":                 row.get("_bse_code"),
        "_status":                   row.get("_status"),
        "_cg_url":                   row.get("cg_url"),
    }

# ---- MAIN ----
def run():
    print("="*60)
    print(f"IPO SCRAPER v13.2 | {date.today()}")
    print("Active filter: closed within 14 days OR listing in future")
    print("="*60)
    driver=make_driver()
    all_ipos=get_mainboard_list(driver)
    active=[]

    for ipo in all_ipos:
        try:
            polite_pause()          # human-like gap between pages (anti-block on VPS)
            r=scrape_detail(driver,ipo)
            if r is None:
                print(f"  [skip] {ipo['IPO_Name'][:40]} - old, skip")
                continue
            gmp=get_gmp(driver,ipo["IPO_Name"],r.get("Offer Price"))
            for k,v in gmp.items():
                if k not in r or r[k] is None: r[k]=v
            active.append(r)
            time.sleep(1)
        except Exception as e:
            print(f"  ERROR {ipo['IPO_Name']}: {e}")

    mkt=get_nifty_features()
    driver.quit()

    if not active:
        print("No active IPOs today."); return pd.DataFrame()

    rows=[finalise(ipo,mkt) for ipo in active]
    df=pd.DataFrame(rows)

    CORE=["IPO_Name","Close_Date","Offer Price","Issue_Size(crores)",
          "QIB","HNI","RII","Total","gmp_closing_gain_pct","gmp_closing_day",
          "pre_issue_pe","roe_ronw","promoter_holding_post_ipo","sector",
          "ofs_pct","fresh_issue_pct",
          "market_sentiment_ratio","nifty_volatility_30d","trend_score"]

    print(f"\n{'='*60}\nRESULT - {len(df)} active mainboard IPOs\n{'='*60}")
    for c in CORE:
        n=int(df[c].notna().sum()) if c in df.columns else 0
        pct=n/len(df)*100 if len(df)>0 else 0
        flag="[ok]" if pct==100 else("[!]" if pct>=50 else"[x]")
        print(f"  {flag} {c:<35} {n}/{len(df)} ({pct:.0f}%)")

    print(f"\n{df[[c for c in CORE if c in df.columns]].to_string(index=False)}")
    df.to_excel("active_ipos_v13.xlsx",index=False)
    print(f"\n[saved] active_ipos_v13.xlsx")
    return df

if __name__=="__main__":
    df=run()
