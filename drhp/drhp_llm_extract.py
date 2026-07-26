"""
drhp_llm_extract.py - schema-driven LLM extraction with verification (optional).

For sections where regex hits its ceiling (litigation case tables, objects
breakdowns), ask Gemini to fill a strict JSON schema FROM the section text,
then VERIFY: every number in the output must literally appear in the source
text, else that item is dropped. Grounded by construction; needs GEMINI_API_KEY.
Live-test on the laptop: the sandbox cannot reach the API.
"""
import os, re, json, urllib.request

KEY   = os.environ.get("GEMINI_API_KEY", "").strip()
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

SCHEMAS = {
    "litigation": ("List every distinct legal matter as JSON: "
                   '{"cases":[{"who":"company|promoter|director|subsidiary",'
                   '"type":"criminal|civil|tax|regulatory","count":int,'
                   '"amount":"as stated or null","page":int}]}'),
    "objects":    ('{"uses":[{"purpose":str,"amount":"as stated","page":int}],'
                   '"fresh_issue":"amount or null","offer_for_sale":"amount or null"}'),
}

def _gemini(prompt):
    import drhp_llm
    return drhp_llm.complete(prompt, json_mode=True)

def _verified(obj, source):
    """Drop any dict item containing a number not present in the source text."""
    src = re.sub(r"[,\u20b9]", "", source.lower())
    def ok(x):
        if isinstance(x, dict):  return all(ok(v) for v in x.values())
        if isinstance(x, list):  return all(ok(v) for v in x)
        for n in re.findall(r"\d[\d,]*\.?\d*", str(x)):
            if re.sub(",", "", n) not in src: return False
        return True
    if isinstance(obj, dict):
        return {k: ([i for i in v if ok(i)] if isinstance(v, list) else (v if ok(v) else None))
                for k, v in obj.items()}
    return obj

def extract_section(index, section_key):
    """Schema-extract one section; returns verified JSON or None."""
    import drhp_llm
    if not drhp_llm.available() or section_key not in SCHEMAS: return None
    chunks = index.chunks
    chunks = list(chunks.values()) if isinstance(chunks, dict) else list(chunks)
    text = "\n".join(f"[p.{c['page_start']}] {c['text']}"
                     for c in chunks if c["section"] == section_key)[:24000]
    if len(text) < 200: return None
    prompt = ("Extract from this Indian IPO prospectus section. Use ONLY this text; "
              "cite the [p.N] page for each item; null when not stated.\n"
              f"Schema: {SCHEMAS[section_key]}\n\nTEXT:\n{text}")
    try:
        out = json.loads(_gemini(prompt))
    except Exception as e:
        return {"error": f"{type(e).__name__}"}
    return _verified(out, text)
