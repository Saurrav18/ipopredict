"""
add_drhp_nav.py - put a "DRHP Scanner" button in the IPOPredict top nav.

Run it INSIDE the repo folder:      py -3.12 add_drhp_nav.py

Why a script instead of hand-editing HTML: the nav markup is repeated across
index.html and every page_*.html, and those files are large. A script edits
them all identically, is idempotent (safe to run twice - it skips files that
already have the button), and can be re-run after any page regeneration.

The existing nav buttons switch views client-side via data-nav. The scanner is
a separate mounted app, so its button navigates instead - which is why it sets
location.href rather than carrying a data-nav attribute.
"""
import re
import sys
from pathlib import Path

BUTTON = ('<button class="navbtn" onclick="location.href=\'/drhp/\'" '
          'title="Read an IPO prospectus (DRHP/RHP) with page-cited analysis">'
          'DRHP Scanner</button>')

# The dashboard picks its opening view from START_VIEW and then REWRITES the
# hash, so arriving at "/#archive" from another page (e.g. the scanner's nav)
# would silently land on the default view. This patch makes a valid incoming
# hash win on first load - which is what makes cross-app navigation coherent.
VIEWS = ("ledger", "archive", "patterns", "alerts", "ask", "method", "account")
# The dashboard toggled dark mode without persisting it, so moving between the
# dashboard and the scanner would flip the theme back. Both apps now read/write
# the same localStorage key, which is what makes them feel like one product.
THEME_ANCHOR = ('$("#themeBtn").addEventListener("click", () => {\n'
                '  const dark = document.documentElement.classList.toggle("dark");\n'
                '  $("#themeBtn").textContent = dark ? "Light" : "Dark";\n'
                '});')
THEME_PATCH = (
    '(function(){                      /* shared theme, persists across /drhp */\n'
    '  var KEY = "ipopredict_theme";\n'
    '  function apply(dark){\n'
    '    document.documentElement.classList.toggle("dark", dark);\n'
    '    var b = document.getElementById("themeBtn");\n'
    '    if (b) b.textContent = dark ? "Light" : "Dark";\n'
    '  }\n'
    '  try { apply(localStorage.getItem(KEY) === "dark"); } catch(e){}\n'
    '  $("#themeBtn").addEventListener("click", function(){\n'
    '    var dark = !document.documentElement.classList.contains("dark");\n'
    '    apply(dark);\n'
    '    try { localStorage.setItem(KEY, dark ? "dark" : "light"); } catch(e){}\n'
    '  });\n'
    '})();')

HASH_ANCHOR = 'var sv = typeof START_VIEW !== "undefined" ? START_VIEW : "ledger";'
HASH_PATCH = HASH_ANCHOR + """
  try {                                  // honour an incoming #view deep link
    var _h = (location.hash || "").replace(/^#/, "").split("/")[0];
    if (_h && %s.indexOf(_h) >= 0) sv = _h;
  } catch(e){}""" % (list(VIEWS),)

def patch(path: Path) -> str:
    html = path.read_text(encoding="utf-8", errors="ignore")

    changed = []

    # (b) deep-link support - independent of the button, so a page that already
    #     has the button can still gain it
    if HASH_ANCHOR in html and "honour an incoming #view deep link" not in html:
        html = html.replace(HASH_ANCHOR, HASH_PATCH, 1)
        changed.append("deep-links")

    # (c) shared persisted theme
    if THEME_ANCHOR in html:
        html = html.replace(THEME_ANCHOR, THEME_PATCH, 1)
        changed.append("shared-theme")

    if "/drhp/" in html:
        if changed:
            path.write_text(html, encoding="utf-8")
            return "patched (" + ", ".join(changed) + "); button already present"
        return "skipped (already patched)"

    # anchor on the LAST button inside the caps nav, then close the nav
    m = re.search(r'(<nav class="caps">.*?)(\s*</nav>)', html, re.S)
    if m:
        html = html[:m.end(1)] + "\n    " + BUTTON + html[m.end(1):]
        changed.append("nav button")
    elif not changed:
        return "skipped (no <nav class=\"caps\"> found)"

    path.write_text(html, encoding="utf-8")
    return "patched (" + ", ".join(changed) + ")"

def main():
    here = Path(".").resolve()
    targets = sorted(list(here.glob("index.html")) + list(here.glob("page_*.html")))

    if not targets:
        print("No index.html / page_*.html found. Run this inside the repo folder.")
        return 1

    print(f"Scanning {len(targets)} page(s) in {here}\n")
    for p in targets:
        print(f"  {p.name:28} {patch(p)}")

    print("\nDone. Restart the server and hard-refresh the browser (Ctrl+F5).")
    print("The button appears in the top nav and opens /drhp/.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
