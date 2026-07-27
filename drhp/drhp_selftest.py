"""
drhp_selftest.py - one-command acceptance test for the DRHP scanner.

Run:  py -3.12 drhp_selftest.py <path-to-any-RHP.pdf>
      py -3.12 drhp_selftest.py            (uses DRHP_TEST_PDF env or asks)

What it does: checks deps and .env, boots the app on a scratch port, performs a
REAL scan of the PDF through the HTTP API, then validates the response the way
this project was built - by asserting qualities, not just status codes:
snapshot present, fundamentals typed and multi-year, flags tri-state with
severities, statements captured, analyst memo with pillars+contradictions,
ask contract, refresh-restore, and llm/self diagnostics. Prints a scorecard.
"""
import sys, os, json, time, subprocess, urllib.request, urllib.error

PORT = int(os.environ.get("DRHP_TEST_PORT", "8019"))
BASE = f"http://127.0.0.1:{PORT}"
HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = []

def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  - {detail}" if detail else ""))

def req(path, method="GET", data=None, files=None, timeout=900):
    url = BASE + path
    if files:
        import uuid
        boundary = uuid.uuid4().hex
        fname, blob = files
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"{fname}\"\r\nContent-Type: application/pdf\r\n\r\n").encode() \
               + blob + f"\r\n--{boundary}--\r\n".encode()
        r = urllib.request.Request(url, data=body, method="POST",
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        r = urllib.request.Request(url, method=method,
                data=json.dumps(data).encode() if data is not None else None,
                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode())

def main():
    pdf = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DRHP_TEST_PDF", "")
    if not pdf or not os.path.exists(pdf):
        print("Usage: py -3.12 drhp_selftest.py <path-to-RHP.pdf>"); sys.exit(2)

    print("== 1. environment ==")
    for mod in ("fastapi", "uvicorn", "pdfplumber", "sklearn"):
        try: __import__(mod); check(f"dependency {mod}", True)
        except ImportError: check(f"dependency {mod}", False, "pip install " + mod)
    for m in ("drhp_ingest","drhp_tables","drhp_digest","drhp_analyst",
              "drhp_contradictions","drhp_llm","drhp_research","drhp_corpus"):
        try:
            sys.path.insert(0, HERE); __import__(m); check(f"module {m}", True)
        except Exception as e:
            check(f"module {m}", False, f"{type(e).__name__}: {e}")

    print("== 2. boot ==")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "drhp_app:app",
                             "--port", str(PORT)], cwd=HERE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    err = None
    try:
        up = False
        for _ in range(30):
            time.sleep(1)
            try:
                s, _d = req("/llmtest"); up = True; break
            except Exception: pass
        check("server boots", up)
        if not up: return finish(proc)
        s, d = req("/llmtest")
        r0 = (d.get("results") or [{}])[0]
        check("llm selftest endpoint", s == 200,
              f"provider ok={r0.get('ok')} " + (r0.get("error","")[:60] if not r0.get("ok") else ""))

        print(f"== 3. real scan ({os.path.basename(pdf)}) ==")
        t0 = time.time()
        blob = open(pdf, "rb").read()
        s, d = req("/scan", files=(os.path.basename(pdf), blob))
        check("scan returns 200", s == 200, f"{time.time()-t0:.0f}s")
        dig = d.get("digest", {})
        check("pages read > 100", d.get("meta", {}).get("pages", 0) > 100,
              str(d.get("meta", {}).get("pages")))
        F = dig.get("fundamentals", [])
        check("fundamentals >= 8 metrics", len(F) >= 8, f"{len(F)} metrics")
        multi = sum(1 for f in F if len(f.get("values", [])) >= 3)
        check("multi-year values on most metrics", multi >= max(4, len(F)//2), f"{multi} with 3 FYs")
        check("every metric page-cited", all(f.get("page") for f in F))
        keys = [f["key"] for f in F]
        check("no duplicate metrics", len(keys) == len(set(keys)))
        fl = dig.get("red_flags", [])
        check("flags tri-state with severity", fl and all("severity" in f and "triggered" in f for f in fl),
              f"{sum(f['triggered'] for f in fl)}/{len(fl)} triggered")
        check("triggered flags carry evidence+page",
              all(f.get("evidence", {}).get("page") for f in fl if f["triggered"]))
        check("offer snapshot extracted", bool(dig.get("offer")), str(dig.get("offer", {}))[:70])
        check("units detected", bool(dig.get("units")), str(dig.get("units")))
        check("statement rows captured", len(dig.get("statements", [])) >= 5,
              f"{len(dig.get('statements', []))} rows")
        blob_txt = json.dumps(dig.get("summary", {}))
        check("no placeholder bullets [\u25cf]", "\u25cf" not in blob_txt)
        check("no LLM preamble leak", "Here are" not in blob_txt)

        print("== 4. analyst ==")
        s, m = req("/analyst", method="POST")
        check("analyst memo 200", s == 200)
        check("pillars scored with reasons", len(m.get("pillars", [])) >= 4 and
              all(p.get("reasons") for p in m.get("pillars", [])))
        check("contradiction engine ran", "contradictions" in m,
              f"{len(m.get('contradictions', []))} tensions found")
        check("coverage gaps self-reported", "coverage_gaps" in m, str(m.get("coverage_gaps"))[:60])

        print("== 5. ask + restore ==")
        s, a = req("/ask", method="POST", data={"question": "should i apply?"})
        check("general question answered honestly", s == 200 and a.get("answer") and
              "advice" in json.dumps(a).lower())
        s, a2 = req("/ask", method="POST", data={"question": "who are the promoters?"})
        check("factual question answered with pages", s == 200 and a2.get("answer") and
              all(it.get("page") for it in a2["answer"][:3]))
        s, l = req("/last")
        check("refresh-restore (/last)", s == 200 and l.get("digest"))
    except Exception as e:
        import traceback
        err = f"{type(e).__name__}: {e}"
        traceback.print_exc()
        check("selftest completed without harness errors", False, err)
    return finish(proc)

def finish(proc):
    proc.terminate()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{'='*46}\nSCORE: {passed}/{len(RESULTS)} checks passed")
    if passed == len(RESULTS):
        print("ALL CHECKS PASSED - the scanner is production-healthy.")
    else:
        print("Failures above - paste this output to debug.")
    sys.exit(0 if passed == len(RESULTS) else 1)

if __name__ == "__main__":
    main()
