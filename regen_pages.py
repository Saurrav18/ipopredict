"""Regenerate the deep-link entry pages (page_*.html) and preview_ledger.html
from index.html, so they carry the same embedded ARCHIVE after a retrain.
build_archive.py rewrites index.html in place; this propagates that change to
the page_*.html variants. Called automatically at the end of ipo_update.main().
"""
import os
from pathlib import Path

INDEX = os.environ.get("INDEX_HTML", "index.html")
ANCHOR = "</footer>\n\n<script>"
PAGES = {
    "page_ledger.html":   'const START_VIEW="ledger";',
    "page_archive.html":  'const START_VIEW="archive";',
    "page_method.html":   'const START_VIEW="method";',
    "page_patterns.html": 'const START_VIEW="patterns";',
    "page_ask.html":      'const START_VIEW="ask";',
    "page_alerts.html":   'const START_VIEW="alerts";',
    "page_account.html":  'const START_VIEW="account";',
}


def main():
    p = Path(INDEX)
    if not p.exists():
        print(f"  regen_pages: {INDEX} not found; skipped.")
        return
    idx = p.read_text(encoding="utf-8")
    if idx.count(ANCHOR) != 1:
        print(f"  regen_pages: injection anchor not unique in {INDEX}; skipped.")
        return

    def inj(snippet):
        return idx.replace(ANCHOR, "</footer>\n\n<script>\n" + snippet + "\n</script>\n<script>", 1)

    n = 0
    for fn, snippet in PAGES.items():
        if Path(fn).exists():
            Path(fn).write_text(inj(snippet), encoding="utf-8")
            n += 1
    ql = Path("qualified_ipos.json")
    if ql.exists():
        Path("preview_ledger.html").write_text(
            inj("const PREVIEW_LIVE = " + ql.read_text(encoding="utf-8").strip() + ";"),
            encoding="utf-8")
    print(f"  regen_pages: refreshed {n} entry pages + preview from {INDEX}.")


if __name__ == "__main__":
    main()
