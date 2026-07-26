"""
drhp_tables.py - REAL table extraction for the sections where tables carry the
truth: financial statements, basis-for-issue-price KPIs, litigation summaries.

Text-flattening loses table structure; this module goes back to the PDF with
pdfplumber.extract_tables() on ONLY the pages we know matter (from the section
map), and returns structured rows. Feeds: fundamentals (multi-year statements),
red-flag thresholds (OCF trend, litigation amounts), and the UI's statements block.
"""
import re

# canonical financial line-items we hunt for in statement/KPI tables
LINE_ITEMS = [
    ("revenue",   r"revenue\s+from\s+operations?"),
    ("total_income", r"total\s+income"),
    ("ebitda",    r"\bebitda\b(?!\s*margin)"),
    ("pat",       r"(profit|loss)\s+(after\s+tax|for\s+the\s+(year|period))|\bpat\b"),
    ("ocf",       r"(net\s+)?cash\s+(flows?\s+)?(generated|used)\s*(/\s*\(used(\s+in)?\))?\s*(from|in)\s*"
                  r"(/\s*\(used(\s+in)?\))?\s*operat\w*\s+activit"
                  r"|net\s+cash\s+(flows?\s+)?(generated\s+from|from|used\s+in|\(used\s+in\)/?\s*generated)"
                  r"\s*(/\s*\(used\s+in\))?\s*operating|operating\s+cash\s*flow|cash\s+flows?\s+from\s+operating"),
    ("borrowings",r"total\s+borrowings|total\s+debt"),
    ("networth",  r"net\s*worth|total\s+equity|net worth\(1\)"),
    ("eps",       r"\beps\b|earnings\s+per\s+share"),
    ("roe",       r"\bronw\b|\broe\b|return\s+on\s+(net\s*worth|equity)"),
    ("roce",      r"\broce\b"),
    ("current_ratio", r"current\s+ratio"),
    ("ebitda_margin", r"ebitda\s+margin"),
    ("rpt_total", r"total.{0,30}related\s+part|related\s+part.{0,40}total"),
    ("total_assets",  r"total\s+assets"),
    ("finance_cost",  r"finance\s+costs?"),
    ("depreciation",  r"depreciation\s+and\s+amorti"),
    ("pbt",           r"profit\s+before\s+tax"),
    ("inventory",     r"^\s*inventories"),
    ("receivables",   r"trade\s+receivables"),
    ("cash",          r"cash\s+and\s+cash\s+equivalents"),
    ("payables",      r"(?<!in )(?<!in\s)trade\s+payables(?!\s*\d.*decrease)"),
    ("ppe",           r"property,?\s+plant\s+and\s+equipment"),
    ("employee_cost", r"employee\s+benefits?\s+expenses?"),
    ("top1_customer",  r"top\s*1\s+customer"),
    ("top5_customers", r"top\s*5\s+customers?"),
    ("top10_customers",r"top\s*10\s+customers?"),
    ("top10_suppliers",r"top\s*10\s+suppliers?"),
    ("capacity_util",  r"capacity\s+utili[sz]ation"),
    ("order_book",     r"order\s+book"),
]
_PREF_PCT = {"top1_customer","top5_customers","top10_customers","capacity_util"}

_NUMRX = re.compile(r"\(?-?\d[\d,]*\.?\d*\)?%?")
_MAG = {"revenue","total_income","ebitda","pat","ocf","borrowings","networth","rpt_total","payables","ppe","employee_cost",
        "total_assets","finance_cost","depreciation","pbt","inventory","receivables","cash"}
_PCT = {"roe","roce","ebitda_margin"}

_BS_BALANCE = {"inventory", "receivables", "payables", "ppe", "borrowings",
               "networth", "cash", "total_assets"}

def _typed(key, vals):
    if key in _BS_BALANCE:
        # balance-sheet items are positive balances; a leading "(" OR any bracketed
        # value in the trio means a cash-flow movement row leaked in - reject it
        if vals and any(str(v).lstrip().startswith("(") for v in vals[:3]):
            return []
    """Type-check values per metric; drop cell-split fragments like '13.' or '2,50'."""
    vals = [v for v in vals if not v.endswith(".")]
    vals = [v for v in vals if not re.fullmatch(r"\(?\d,\d{1,2}\)?%?", v)]  # broken comma split
    if key in _MAG:
        vals = [v for v in vals if not v.endswith("%")]
        big = any(re.search(r",|\d{4,}", v) for v in vals)
        # small-cap statements are in lakhs without commas: accept rows where
        # at least two values look like proper decimals (e.g. 710.23, 234.19)
        two_dec = sum(bool(re.fullmatch(r"\(?\d{1,3}\.\d{2}\)?", v)) for v in vals) >= 2
        return vals if (big or two_dec) else []
    if key in _PCT:
        pct = [v for v in vals if v.endswith("%") and "." in v]     # '6%' is a split fragment
        return pct or [v for v in vals if re.match(r"\d{1,2}\.\d", v)]
    if key in _PREF_PCT:
        pct = [v for v in vals if v.endswith("%")]
        return pct or vals
    return [v for v in vals if not v.endswith("%")]


def _cellnums(cells):
    """Numeric values from a table row's cells, cleaned. The row's cells are
    JOINED first, then repaired for PDF digit-shredding - because shreds split
    ACROSS cell boundaries too ('8 ,559' in one cell, '.67' in the next), which
    no per-cell repair can rejoin. Left-to-right value order is identical either
    way. Only the two SAFE merges are applied ('8 ,559.67' / '559 .67'); looser
    merging would fuse adjacent columns ('14 9,770.90' is note-14 beside a
    value) and corrupt data."""
    joined = " ".join(str(c) for c in cells if c)
    joined = re.sub(r"(\d) ,(\d{3})", r"\1,\2", joined)
    joined = re.sub(r"(\d),? \.(\d)", lambda m: (m.group(0).replace(" ", "")), joined)
    vals = []
    if True:
        for v in _NUMRX.findall(joined):
            v = v.strip().rstrip(",")
            if not any(ch.isdigit() for ch in v): continue
            if re.fullmatch(r"\(?(19|20)\d{2}\)?", v): continue        # years
            if re.fullmatch(r"\(?\d{1,2}\)?", v) and "." not in v: continue  # footnotes/serials
            vals.append(v)
    return vals


_STMT_ANCHOR = re.compile(
    r"(restated\s+(consolidated\s+)?(summary\s+)?statement|statement\s+of\s+profit"
    r"|statement\s+of\s+cash\s*flows?|cash\s+flow\s+statement|balance\s+sheet"
    r"|revenue\s+from\s+operations|key\s+performance\s+indicators"
    r"|top\s*\d+\s+customers?|capacity\s+utili[sz]ation|order\s+book"
    r"|industry\s+composite|peer\s+group)", re.I)

def _pages_for(raw_pages, cap=60):
    """Target pages by CONTENT. Peer/KPI pages are guaranteed targets even when
    the cap fills with statement pages (a 535-page RHP mentions revenue on
    dozens of pages before the peer table appears)."""
    main = [p for p, t in raw_pages if t and _STMT_ANCHOR.search(t)][:cap]
    must = [p for p, t in raw_pages if t and re.search(
        r"industry\s+composite|peer\s+group|key\s+performance\s+indicators|"
        r"net\s+worth\(?1?\)?\s+[\d,]+\.\d", t, re.I)]
    return sorted(set(main) | set(must[:15]))


def extract_financial_tables(pdf_path, raw_pages):
    """Return {item_key: {"label","values"[newest-first up to 3],"page"}} plus
    raw statement rows for the UI. Visits only statement-looking pages."""
    import pdfplumber
    targets = _pages_for(raw_pages)
    if not targets:
        return {}, []
    peer_pages = {p for p, t in raw_pages
                  if t and re.search(r"listed (industry )?peers?|peer group|"
                                     r"comparison (with|of).{0,80}(peers|industry)|accounting ratios with|"
                                     r"basis for (the )?(issue|offer) price|industry peer", t, re.I)}
    # pages that literally tabulate peer companies by name also poison company rows
    for p, t in raw_pages:
        if t and re.search(r"(united polyfab|nandan denim|sanathan|ganesha ecosphere|"
                           r"nitin spinners|indo count|s\.?\s*p\.?\s*apparels)", t, re.I):
            peer_pages.add(p)
    found, rows_out, cands = {}, [], {}
    def _years_of(tb):
        for r in tb[:4]:
            yrs = re.findall(r"\b(20\d\d)[\s-]*(?:2\d)?\b", " ".join(str(c) for c in r if c))
            if len(yrs) >= 2: return yrs[:3]
        return None
    with pdfplumber.open(pdf_path) as pdf:
        npages = len(pdf.pages)
        for pno in targets:
            if pno > npages: continue
            page = pdf.pages[pno - 1]
            tables = []
            for settings in (None,                                   # ruled tables
                             {"vertical_strategy": "text",           # borderless tables
                              "horizontal_strategy": "text",
                              "min_words_vertical": 2}):
                try:
                    t = page.extract_tables(settings) if settings else page.extract_tables()
                    if t: tables += t
                except Exception:
                    pass
            for tb in tables:
                tb_years = _years_of(tb)
                for row in tb:
                    if not row: continue
                    rowtext = " ".join(str(c).replace("\n", " ") for c in row if c)
                    if re.search(r"increase\s*/?\s*\(?\s*decrease|changes?\s+in\s+(working\s+capital|"
                                 r"inventor|trade\s+receivable|trade\s+payable)", rowtext, re.I):
                        continue                     # cash-flow movement line, not a balance
                    low = re.sub(r"\s{2,}", " ", rowtext.lower())
                    if len(low) > 220: continue
                    _bsonly = ("inventor", "receivable", "payable")
                    if any(w in low for w in _bsonly) and re.search(r"^\s*\(", rowtext):
                        continue                     # bracketed = cash-flow delta, skip
                    for key, pat in LINE_ITEMS:
                        if re.search(pat, low):
                            label = rowtext.strip()
                            if key == "borrowings":
                                _bn = [c for c in _cellnums(row)]
                                _bvals = []
                                for _v in _bn:
                                    try: _bvals.append(abs(float(_v.replace(",",""))))
                                    except: pass
                                # D/E ratio rows are single-digit; real borrowings are large
                                if _bvals and max(_bvals) < 50:
                                    break
                            if key in ("peer_pe_hi", "peer_pe_avg"):
                                if pno not in peer_pages: break
                            elif pno in peer_pages:
                                break            # peer tables carry OTHER companies' numbers
                            if key in _MAG and re.search(r"grew|growth|increase|decrease|margin", low):
                                break
                            vals = _typed(key, _cellnums(row))[:3]
                            if not vals: break
                            score = len(vals) * 2 + (1 if len(vals) == 3 else 0)
                            cands.setdefault(key, []).append(
                                {"label": label[:60], "values": vals, "years": tb_years,
                                 "page": pno, "_score": score})
                            break
            try: page.flush_cache()
            except Exception: pass
    def _num(v):
        v = v.strip("()%").replace(",", "")
        try: return abs(float(v))
        except ValueError: return None

    def _sane(key, vals, rev_med):
        nums = [n for n in (_num(v) for v in vals) if n]
        if not nums: return False
        # PAT and OCF legitimately swing wildly (turnarounds, sign flips);
        # stable magnitudes must be internally consistent
        if key not in ("pat", "ocf") and len(nums) > 1 \
                and max(nums) / max(min(nums), 1e-9) > 25:
            return False                          # internally misaligned row
        if key in _MAG and key != "revenue" and rev_med:
            med = sorted(nums)[len(nums)//2]
            if not (rev_med / 600 <= med <= rev_med * 2.5):
                return False                      # wrong scale vs revenue
        return True

    for p, t in raw_pages:
        if not t or not re.search(r"research and development", t, re.I): continue
        m = re.search(r"(?:R&D|research and development)[\s\S]{0,600}?percentage of revenue"
                      r"[\s\S]{0,120}?(\d{1,2}\.\d{1,2})\s*%", t, re.I)
        if m:
            cands.setdefault("rnd_pct", []).append(
                {"label": "R&D spend (% of revenue)", "values": [m.group(1) + "%"],
                 "page": p, "_score": 9})
            break
    # peer P/E lives in plain text on the basis-for-price page, not in a table
    for p, t in raw_pages:
        if not t or not re.search(r"industry\s+composite", t, re.I): continue
        m1 = re.search(r"Highest\s+([\d.]+)", t)
        m2 = re.search(r"Industry\s+composite\*?\s+([\d.]+)", t, re.I)
        if m1: cands.setdefault("peer_pe_hi", []).append(
            {"label": "Peer P/E (highest)", "values": [m1.group(1)], "page": p, "_score": 9})
        if m2: cands.setdefault("peer_pe_avg", []).append(
            {"label": "Industry composite P/E", "values": [m2.group(1)], "page": p, "_score": 9})
        break

    # core company series must not originate on a peer/basis page - filter first.
    # basis-for-issue-price pages tabulate the company AGAINST peers, so a 3-col
    # row there mixes company + peer years; prefer statement pages instead.
    basis_pages = {p for p, t in raw_pages if t and re.search(
        r"basis for (the )?(issue|offer) price|comparison with.{0,40}(listed )?peers?|"
        r"industry peer group", t, re.I)}
    for _k in ("revenue", "pat", "eps", "networth", "total_income", "roe", "roce",
               "ebitda_margin", "ebitda", "cash", "borrowings"):
        if _k in cands:
            clean = [c for c in cands[_k] if c["page"] not in peer_pages
                     and c["page"] not in basis_pages]
            if clean: cands[_k] = clean
    # company P/E: if it ONLY appears on a basis/peer page, it is a peer figure -
    # drop it so the report shows [●] rather than a misleading company P/E
    if "pe" in cands:
        nonbasis = [c for c in cands["pe"] if c["page"] not in peer_pages
                    and c["page"] not in basis_pages]
        cands["pe"] = nonbasis   # empty if only-on-basis -> no company P/E shown
    # text-regex fallback for KPI-summary rows that table detection shreds
    _TEXT_METRICS = [
        ("networth", r"net\s*worth\s*\(?\d?\)?\s+([\d][\d,]*\.\d{2})\s+([\d][\d,]*\.\d{2})\s+([\d][\d,]*\.\d{2})"),
        ("revenue",  r"revenue\s+from\s+operations\s+([\d][\d,]*\.\d{2})\s+([\d][\d,]*\.\d{2})\s+([\d][\d,]*\.\d{2})"),
        # OCF: unruled cash-flow statements are invisible to pdfplumber tables, so
        # the final "Net cash generated/(used) from operating activities (A)" line is
        # read from text. Brackets allowed - OCF is legitimately negative some years.
        ("ocf",      r"(?:net\s+)?cash\s+(?:flows?\s+)?(?:generated|used)\s*(?:/\s*\(used(?:\s+in)?\))?\s*(?:from|in)\s*"
                     r"(?:/\s*\(used(?:\s+in)?\))?\s*operat\w*\s+activit\w*\s*(?:\([A-Z]\))?\s*[:\-]?\s+"
                     r"(\(?[\d][\d,]*\.\d{2}\)?)\s+(\(?[\d][\d,]*\.\d{2}\)?)\s+(\(?[\d][\d,]*\.\d{2}\)?)"),
    ]
    _basis = {pp for pp, tt in raw_pages if tt and re.search(r"basis for (the )?(issue|offer) price", tt, re.I)}
    _rev0 = None
    for c in cands.get("revenue", []):
        try: _rev0 = abs(float(c["values"][0].replace(",", ""))); break
        except Exception: pass
    def _consistent3(vals, key=None):
        if key == "ocf":
            # OCF swings sign and magnitude year to year; require only that the
            # values parse and sit within a sane band of revenue scale.
            try: nums = [abs(float(v.strip("()").replace(",", ""))) for v in vals]
            except ValueError: return False
            if _rev0 and max(nums) > _rev0 * 2: return False
            return True
        try: nums = [abs(float(v.replace(",", ""))) for v in vals]
        except ValueError: return False
        if not (len(nums) == 3 and min(nums) > 0 and max(nums) / min(nums) <= 8): return False
        if key == "networth" and _rev0 and not (_rev0/50 <= nums[0] <= _rev0*20): return False
        return True
    for mk, mpat in _TEXT_METRICS:
        existing = cands.get(mk, [])
        only_basis = existing and all(c["page"] in _basis for c in existing)
        # a full 3-year statement line beats a partial table fragment: run the text
        # fallback not only when the table found nothing, but also when it found an
        # incomplete series (<3 values) - the fuller reading wins.
        best_len = max((len(c.get("values", [])) for c in existing), default=0)
        if existing and not only_basis and best_len >= 3:
            continue
        for p, t in raw_pages:
            if not t or p in _basis: continue
            for mm in re.finditer(mpat, re.sub(r"\s+", " ", t), re.I):
                trio = [mm.group(1), mm.group(2), mm.group(3)]
                if _consistent3(trio, mk):
                    lbl = {"networth": "Net worth", "revenue": "Revenue from operations", "ocf": "Operating cash flow"}.get(mk, mk.title())
                    cands[mk] = [{"label": lbl, "values": trio, "page": p, "_score": 99}]
                    break
            if cands.get(mk) and cands[mk][0].get("_score") == 99: break
    # concentration text-fallback: "top ten customers ... 67.47%, 72.17% and 81.14%"
    for kind, key in (("customers", "top10_customers"), ("suppliers", "top10_suppliers")):
        if key in found or key in cands: continue
        for p, t in raw_pages:
            if not t: continue
            flat = re.sub(r"\s+", " ", t)
            m = re.search(r"top (?:ten|10) " + kind +
                          r"[^%]{0,80}?(\d{1,2}\.\d{1,2})\s*%[,\s]+(?:and\s+)?(\d{1,2}\.\d{1,2})\s*%"
                          r"[,\s]+(?:and\s+)?(\d{1,2}\.\d{1,2})\s*%", flat, re.I)
            if m:
                cands[key] = [{"label": f"Top 10 {kind} share",
                               "values": [m.group(1)+"%", m.group(2)+"%", m.group(3)+"%"],
                               "page": p, "_score": 50}]
                break
    rev = max(cands.get("revenue", []), key=lambda c: c["_score"], default=None)
    rev_med = None
    if rev:
        rn = [n for n in (_num(v) for v in rev["values"]) if n]
        rev_med = sorted(rn)[len(rn)//2] if rn else None
    def _latest(key):
        c = found.get(key)
        return _num(c["values"][0]) if c and c.get("values") else None

    def _pick(key, validator=None):
        for c in sorted(cands.get(key, []), key=lambda c: -c["_score"]):
            if not _sane(key, c["values"], rev_med): continue
            if validator and not validator(_num(c["values"][0])): continue
            c.pop("_score", None)
            found[key] = c
            rows_out.append({"key": key, **c})
            return

    # magnitudes + ratios first
    for key in list(cands):
        if key not in ("networth", "ebitda"):
            _pick(key)
    # networth must be consistent with PAT/ROE (NW = PAT/ROE), within 3x
    pat, roe = _latest("pat"), _latest("roe")
    implied_nw = (pat / (roe / 100)) if (pat and roe) else None
    _pick("networth", (lambda n: n and implied_nw and implied_nw/3 <= n <= implied_nw*3)
                      if implied_nw else None)
    # EBITDA must be consistent with revenue x margin, within 3x
    marg = _latest("ebitda_margin")
    implied_eb = (rev_med * marg / 100) if (rev_med and marg and marg < 80) else None
    pat_l, fin_l = _latest("pat"), _latest("finance_cost")
    floor_eb = (pat_l or 0) + (fin_l or 0)   # EBITDA below PAT+finance is impossible
    _pick("ebitda", lambda n: n and (not implied_eb or implied_eb/3 <= n <= implied_eb*3)
                              and (floor_eb == 0 or n >= floor_eb))
    if "ocf" in found and "pbt" in found and found["ocf"]["values"] == found["pbt"]["values"]:
        found.pop("ocf", None)                    # grabbed the PBT opening line
        rows_out[:] = [r for r in rows_out if r["key"] != "ocf"]
    # borrowings below annual finance cost implies >100% interest: mislabeled row
    if fin_l:
        b = found.get("borrowings")
        if b:
            bn = _num(b["values"][0])
            if bn and bn < fin_l * 1.5:
                found.pop("borrowings", None)
                rows_out[:] = [r for r in rows_out if r["key"] != "borrowings"]
    return found, rows_out


def merge_into_fundamentals(fund_list, table_found):
    """Table-extracted values override text-regex values when they carry MORE
    fiscal years (tables are the ground truth for statements)."""
    label_map = {"revenue": "Revenue from operations", "pat": "Profit after tax (PAT)",
                 "ocf": "Operating cash flow", "eps": "EPS", "roe": "ROE / RoNW",
                 "roce": "ROCE", "current_ratio": "Current ratio",
                 "ebitda_margin": "EBITDA margin", "borrowings": "Total borrowings",
                 "networth": "Net worth", "ebitda": "EBITDA", "total_income": "Total income",
                 "top1_customer": "Top 1 customer share", "top5_customers": "Top 5 customers share",
                 "top10_customers": "Top 10 customers share", "top10_suppliers": "Top 10 suppliers share", "capacity_util": "Capacity utilisation",
                 "order_book": "Order book", "rpt_total": "Related-party transactions (total)",
                 "pe": "P/E (company)", "peer_pe_hi": "Peer P/E (highest)",
                 "peer_pe_avg": "Industry composite P/E", "total_assets": "Total assets", "finance_cost": "Finance costs",
                 "depreciation": "Depreciation & amortisation", "pbt": "Profit before tax",
                 "inventory": "Inventories", "receivables": "Trade receivables",
                 "cash": "Cash & equivalents", "rnd_pct": "R&D spend (% of revenue)",
                 "payables": "Trade payables", "ppe": "Property, plant & equipment",
                 "employee_cost": "Employee benefits expense"}
    by_key = {f["key"]: f for f in fund_list}
    for key, t in table_found.items():
        if key in ("rpt_total",): continue
        cur = by_key.get(key)
        if cur is None or len(t["values"]) > len(cur["values"]):
            by_key[key] = {"key": key, "label": label_map.get(key, t["label"][:40]),
                           "values": t["values"], "years": t.get("years"),
                           "page": t["page"], "section": "financial tables"}
    def _fv(k, i=0):
        try:
            v = by_key[k]["values"][i]
            return float(v.strip("()%").replace(",", "")) * (-1 if v.startswith("(") else 1)
        except Exception: return None
    for ck in ("top1_customer", "top5_customers", "top10_customers", "top10_suppliers"):
        if ck in by_key and "revenue" in by_key and not by_key[ck]["values"][0].endswith("%"):
            pcts = []
            for i in range(min(len(by_key[ck]["values"]), len(by_key["revenue"]["values"]))):
                a, r = _fv(ck, i), _fv("revenue", i)
                if a and r: pcts.append(f"{a/r*100:.1f}%")
            if pcts:
                by_key[ck] = {**by_key[ck], "values": pcts,
                              "label": by_key[ck]["label"] + " (computed % of revenue)"}
    order = [k for k, _ in LINE_ITEMS] + [f["key"] for f in fund_list]
    order += [k for k in table_found if k not in order]   # text-pass keys (rnd_pct, peer P/E)
    seen, out = set(), []
    for k in order:
        if k in by_key and k not in seen:
            out.append(by_key[k]); seen.add(k)
    return out


# ---------------- offer snapshot (price band, lot, sizes, dates) ----------------
_OFFER_PATTERNS = [
    ("price_band", r"price\s+band[^\n]{0,40}?(?:rs\.?|\u20b9)\s*([\d,]+)\s*(?:/-)?\s*(?:to|-|\u2013)\s*(?:rs\.?|\u20b9)?\s*([\d,]+)", 2),
    ("lot_size",   r"(?:bid\s+lot|lot\s+size|minimum\s+bid\s+lot)[^\d]{0,30}([\d,]+)\s+equity\s+shares", 1),
    ("fresh_issue",r"(?:fresh|public)\s+issue\s+of[^.]{0,160}?aggregating\s+(?:up\s+)?to\s*(?:rs\.?|\u20b9)\s*([\d,]+(?:\.\d+)?)\s*(lakhs?|million|crores?)", 2),
    ("ofs",        r"offer\s+for\s+sale[^.]{0,200}?aggregating\s+(?:up\s+)?to\s*(?:rs\.?|\u20b9)\s*([\d,]+(?:\.\d+)?)\s*(lakhs?|million|crores?)", 2),
    ("issue_size", r"(?:the\s+)?offer\W{0,4}(?:\(\d\)\W{0,3})*[^.]{0,120}?aggregating\s+(?:up\s+)?to\s*(?:rs\.?|\u20b9)\s*([\d,]+(?:\.\d+)?)\s*(lakhs?|million|crores?)", 2),
    ("opens",      r"(?:bid|issue|offer)\s*/?\s*(?:issue|offer)?\s+opens?\s+on[:\s]+([A-Za-z]+\s+\d{1,2},?\s+\d{4}|\w+day,\s+[A-Za-z]+\s+\d{1,2},?\s+\d{4})", 1),
    ("closes",     r"(?:bid|issue|offer)\s*/?\s*(?:issue|offer)?\s+closes?\s+on[:\s]+([A-Za-z]+\s+\d{1,2},?\s+\d{4}|\w+day,\s+[A-Za-z]+\s+\d{1,2},?\s+\d{4})", 1),
    ("listing_at", r"(?:proposed\s+to\s+be\s+)?listed\s+on\s+(?:the\s+)?([A-Za-z ]{2,40}?(?:platform of )?(?:nse|bse|national stock exchange|bombay stock exchange)[A-Za-z ()]{0,30})", 1),
]

def detect_units(raw_pages):
    """Majority vote over statement pages: are figures in millions, lakhs, crores?"""
    votes = {}
    for p, t in raw_pages:
        if not t: continue
        for m in re.finditer(r"(?:all amounts (?:are )?in|(?:rs\.?|\u20b9)\s*in)\s*(?:inr\s*)?"
                             r"(million|lakh|crore)s?", t, re.I):
            u = m.group(1).lower()
            votes[u] = votes.get(u, 0) + 1
    return (max(votes, key=votes.get) + "s") if votes else None


def offer_snapshot(raw_pages):
    """Broker-note top box: extracted from the cover and offer pages. [\u25cf]
    placeholders (price undecided in many RHPs) are reported honestly."""
    text = " ".join(t for p, t in raw_pages[:15] if t)
    text += " " + " ".join(t for p, t in raw_pages if t and re.search(
        r"(the offer|offer structure|terms of the (issue|offer))", (t or "")[:400], re.I))[:30000]
    out = {}
    for key, pat, groups in _OFFER_PATTERNS:
        m = re.search(pat, text, re.I)
        if not m: continue
        if groups == 2 and key == "price_band":
            out[key] = f"\u20b9{m.group(1)} - \u20b9{m.group(2)}"
        elif groups == 2:
            out[key] = f"\u20b9{m.group(1)} {m.group(2)}"
        else:
            v = re.sub(r"\s+", " ", m.group(1)).strip().rstrip("(").strip()
            if key == "listing_at":
                v = re.sub(r"^(the\s+|stock\s+exchanges?\s+being\s+)", "", v, flags=re.I).strip()
            out[key] = v
    m = re.search(r"equity\s+shares\s+of\s+face\s+value\s+of\s+(?:rs\.?|\u20b9)\s*(\d+)\s+each", text, re.I)
    if m: out["face_value"] = f"\u20b9{m.group(1)}"
    m = re.search(r"weighted\s+average\s+cost\s+of\s+acquisition[\s\S]{0,600}?\bnil\b", text, re.I)
    if m:
        out["promoter_waca"] = "Nil (\u20b90)"
    else:
        m = re.search(r"weighted\s+average\s+cost\s+of\s+acquisition[\s\S]{0,600}?"
                      r"(?:rs\.?|\u20b9)\s*(\d[\d,]*\.\d{1,2})", text, re.I)
        if m:
            out["promoter_waca"] = f"\u20b9{m.group(1)}"
    # recent-transaction WACA: secondary/primary deals in the 1-3 years pre-IPO
    m = re.search(r"(?:secondary transactions|primary transactions|cost of acquisition)[^.]{0,120}?"
                  r"(?:three years|one year|preceding)[^.]{0,40}?(?:\u20b9|rs\.?)?\s*(\d{2,4})\b",
                  text, re.I)
    if not m:
        m = re.search(r"issued\s+CCPS\s+at\s+a\s+price\s+of\s+(?:\u20b9|rs\.?)?\s*(\d{2,4})", text, re.I)
    if m:
        out["waca_recent_txn"] = f"\u20b9{int(m.group(1)):,} (recent investors)"
    def _amt(v):
        try: return float(re.sub(r"[^\d.]", "", v.split()[0].replace("\u20b9","")))
        except Exception: return None
    if "fresh_issue" not in out:
        m = re.search(r"(?:fresh|public)\s+issue\s+of\s+(?:up\s+to\s+)?([\d,]{4,})\s+equity\s+shares", text, re.I)
        if m:
            out["fresh_issue"] = f"up to {m.group(1)} equity shares (amount at [\u25cf])"
    _no_fresh = not re.search(r"(fresh|public)\s+issue\s+of", text, re.I)
    _has_ofs = bool(out.get("ofs")) or re.search(r"offer\s+for\s+sale", text[:20000], re.I)
    if ("fresh_issue" not in out) and _has_ofs and (_no_fresh or re.search(
            r"offer\s+(comprises|consists)\s+(of\s+)?(an\s+)?offer\s+for\s+sale|"
            r"entirely\s+an\s+offer\s+for\s+sale", text, re.I)):
        if "issue_size" in out:
            out["ofs"] = out["issue_size"] + " (100% OFS)"
        out["fresh_issue"] = "none - company receives no proceeds"
    elif "fresh_issue" not in out and "issue_size" in out and "ofs" in out:
        t_, o_ = _amt(out["issue_size"]), _amt(out["ofs"])
        if t_ and o_ and t_ > o_:
            unit = out["issue_size"].split()[-1]
            out["fresh_issue"] = f"\u20b9{t_-o_:,.2f} {unit} (derived: total minus OFS)"
    if "price_band" not in out and re.search(r"price\s+band", text, re.I) \
            and "\u25cf" in text[:20000]:
        out["price_band"] = "not fixed yet [\u25cf]"
    return out


def extract_segments(pdf_path, raw_pages, cap=6):
    """Segment/market-wise revenue split: rows carrying both amounts and %
    on pages that talk about segments. Returns [{label, shares[latest..], page}]."""
    import pdfplumber
    targets = [p for p, t in raw_pages if t and re.search(
        r"market segment|segment[- ]wise|revenue from contracts.{0,80}segment", t, re.I)][:8]
    best, best_n = [], 0
    with pdfplumber.open(pdf_path) as pdf:
        for pno in targets:
            if pno > len(pdf.pages): continue
            page = pdf.pages[pno - 1]
            rows = []
            try: tables = page.extract_tables() or []
            except Exception: tables = []
            for tb in tables:
                for row in tb:
                    if not row: continue
                    cells = [re.sub(r"(\d) \.(\d)", r"\1.\2",
                             re.sub(r"(\d) ,(\d{3})", r"\1,\2",
                             str(c).replace("\n", " "))).strip() for c in row if c]
                    label = " ".join(c for c in cells if not re.search(r"\d", c))[:48]
                    pcts = [c for c in cells if re.fullmatch(r"\d{1,2}\.\d{1,2}%", c)]
                    if label and 1 <= len(pcts) <= 4 and len(label) > 4 \
                            and not re.search(r"total|particular|revenue from contracts", label, re.I):
                        rows.append({"label": label, "shares": pcts[:3], "page": pno})
            if len(rows) > best_n:
                best, best_n = rows[:cap], len(rows)
            try: page.flush_cache()
            except Exception: pass
    return best


def extract_peers(pdf_path, raw_pages, cap=7):
    """Peer-comparison: named peer companies with their P/E from basis pages.
    Works both when peers are a known set and when the doc labels '(Industry Peer)'."""
    # first try the text form: "<Peer Name> (Industry Peer) ... P/E ... <num>"
    text_peers = []
    for p, t in raw_pages:
        if not t or not re.search(r"industry peer|listed peer|comparison of kpi", t, re.I): continue
        flat = re.sub(r"\s+", " ", t)
        # peer P/E often in a row: find company names followed by a P/E-range number
        for mm in re.finditer(r"([A-Z][A-Za-z&.\- ]{3,40}?(?:Limited|Ltd\.?|Industries|Rectifiers|Polyfab|Denim|Spinners))"
                              r"\s*\(industry peer\)", flat, re.I):
            nm = re.sub(r"\s+", " ", mm.group(1)).strip()[:42]
            if nm and not re.search(r"our company|particular", nm, re.I):
                text_peers.append({"name": nm, "values": ["(industry peer)"], "page": p})
        if text_peers: break
    import pdfplumber
    targets = [p for p, t in raw_pages if t and re.search(
        r"listed (industry )?peers?|peer group|industry composite|accounting ratios|comparison of kpi|"
        r"industry peer", t, re.I)][:6]
    out = []
    with pdfplumber.open(pdf_path) as pdf:
        for pno in targets:
            if pno > len(pdf.pages): continue
            page = pdf.pages[pno - 1]
            try: tables = page.extract_tables() or []
            except Exception: tables = []
            for tb in tables:
                for row in tb:
                    if not row: continue
                    cells = [str(c).replace("\n", " ").strip() for c in row if c and str(c).strip()]
                    if len(cells) < 2: continue
                    joined = re.sub(r"(?<=\d)\s+(?=\d{3}\b)", "", " ".join(cells))  # rejoin 15 287->15287
                    mname = re.search(r"(Garware[A-Za-z ]*|Arvind[A-Za-z ]*|SRF[A-Za-z ]*|"
                                      r"[A-Z][A-Za-z]+ (?:Technical|Industries|Fibres|Textiles|Mills)[A-Za-z ]*)",
                                      joined)
                    pe = re.findall(r"\b([1-9]\d(?:\.\d{1,2}))\b", joined)  # 2-digit .xx = P/E-ish
                    pe = [x for x in pe if 8 <= float(x) <= 90]
                    if mname and pe:
                        nm = re.sub(r"\s+", " ", mname.group(1)).strip().rstrip(" 0123456789")[:42]
                        if not re.search(r"our company|particular", nm, re.I):
                            out.append({"name": nm, "pe": pe[0], "page": pno})
            try: page.flush_cache()
            except Exception: pass
            if len(out) >= 3: break
    seen, ded = set(), []
    for r in out:
        if r["name"] not in seen and r["name"]:
            seen.add(r["name"]); ded.append({"name": r["name"], "values": ["P/E " + r["pe"]], "page": r["page"]})
    return ded[:cap]
