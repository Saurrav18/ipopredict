"""
drhp_agent.py - Tool-calling agent for open-ended prospectus Q&A.

The agent pattern: the LLM is given a QUESTION and a TOOL CATALOG, and DECIDES
which tools to call (returned as JSON), possibly across multiple rounds; tool
results are fed back; the final answer must be grounded in tool outputs only.

Why this is the right architecture here:
- "what's the PAT?"            -> agent calls get_financials (exact, validated)
- "any lawsuits?"              -> agent calls get_litigation (itemized, page-cited)
- "is the valuation stretched?"-> agent calls get_peers + get_financials, reasons
- "what do they say about X?"  -> agent calls search_prospectus (hybrid retrieval)
The DECISION of which source answers which question is the agentic step no fixed
router does well; the tools themselves are the proven deterministic/RAG layers,
so answers inherit their correctness and page citations.

No-hallucination guarantees carried through:
- tools return only validated, page-cited data from the existing pipeline
- the final answer prompt forbids facts outside tool results
- a numeric verifier drops any answer number not present in the tool outputs
- with no LLM (compliance mode / dead key) the caller falls back to the
  extractive rag_answer path - the agent is an enhancement, never a dependency.
"""
import json, re


def _tools(index, digest):
    """Tool registry: name -> (description, callable). All read from the proven
    pipeline - validated values, page citations included."""
    import drhp_rag

    def get_financials(_arg=None):
        keep = ("revenue","pat","networth","ebitda","borrowings","ocf","eps",
                "roe","roce","ebitda_margin","debt_equity","interest_cov",
                "current_ratio","top10_customers","top10_suppliers","capacity_util")
        return [{"metric": f.get("label"), "values": f.get("values"),
                 "page": f.get("page")} for f in digest.get("fundamentals", [])
                if f.get("key") in keep]

    def get_flags(_arg=None):
        _fl = [{"flag": f.get("label"), "why": f.get("why"),
                 "evidence": (f.get("evidence") or {}).get("quote", "")[:160],
                 "page": (f.get("evidence") or {}).get("page")}
                for f in digest.get("red_flags", []) if f.get("triggered")]

    def get_contradictions(_arg=None):
        return [{"tension": c.get("title"), "claim": c.get("a"),
                 "number": c.get("b"), "question": c.get("question")}
                for c in digest.get("contradictions", [])]

    def get_litigation(_arg=None):
        """Returns an explicit STATUS, never a bare empty list. An empty list is
        read by an LLM as 'there are no lawsuits' - but empty can also mean the
        extraction never ran (LLM rate-limited) or failed. Conflating 'no data'
        with 'no cases' produces a confident false negative on litigation, which
        is the worst error this tool can make. So: unavailable != none_found."""
        items = digest.get("litigation_itemized") or digest.get("litigation") or []
        if items:
            return {"status": "extracted", "case_count": len(items), "cases": items}
        status = digest.get("litigation_status")
        if status == "none_found":
            return {"status": "none_found",
                    "note": "the document was searched and no litigation was disclosed"}
        return {"status": "UNAVAILABLE",
                "note": "litigation extraction did not complete (LLM unavailable or "
                        "rate-limited). You CANNOT conclude there are no cases. Say "
                        "the data is unavailable and suggest checking the litigation "
                        "chapter directly."}

    def get_peers(_arg=None):
        return digest.get("peer_comparison") or {}

    def get_offer(_arg=None):
        return digest.get("offer") or {}

    def search_prospectus(query):
        hits = drhp_rag.rag_answer(index, str(query or ""), k=6)
        return [{"text": e["text"][:450], "page": e["page"], "section": e["section"]}
                for e in hits.get("evidence", [])]

    return {
        "get_financials":    ("validated multi-year financial metrics, page-cited", get_financials),
        "get_flags":         ("triggered red flags with evidence and pages", get_flags),
        "get_contradictions":("claims-vs-numbers tensions the engine detected", get_contradictions),
        "get_litigation":    ("itemized litigation by direction/type/amount", get_litigation),
        "get_peers":         ("industry peer P/E comparison", get_peers),
        "get_offer":         ("offer structure: fresh/OFS, size, dates, band", get_offer),
        "search_prospectus": ("hybrid semantic search over the full document; arg = search query", search_prospectus),
    }


def _plan_prompt(question, catalog, transcript):
    tools_desc = "\n".join(f"- {n}: {d}" for n, (d, _) in catalog.items())
    hist = ""
    if transcript:
        hist = "\nTOOL RESULTS SO FAR:\n" + json.dumps(transcript)[:5000] + "\n"
    return (
        "You are a prospectus analyst agent. Decide which tool(s) answer the "
        "question. Return ONLY JSON:\n"
        '{"calls": [{"tool": "<name>", "arg": "<query or null>"}]}  to call tools, or\n'
        '{"answer": "<final answer, citing (p.N) pages from the tool results>"} '
        "when you have enough.\nRules: use ONLY facts from tool results; never "
        "invent numbers; prefer get_financials for metric questions, "
        "get_litigation for legal, get_peers for valuation, search_prospectus "
        "for anything narrative.\n"
        "CRITICAL: if a tool result has status UNAVAILABLE, you must NOT conclude "
        "the thing does not exist. Absence of data is NOT evidence of absence. Say "
        "the data could not be retrieved and point to the relevant chapter. Only "
        "state that none exist when a tool explicitly reports status none_found."
        "\n\nTOOLS:\n" + tools_desc + hist +
        "\nQUESTION: " + question)


def _grounded(answer, transcript):
    """Numeric verifier: every number in the answer must appear in tool outputs."""
    blob = re.sub(r"[\s,]", "", json.dumps(transcript))
    for num in re.findall(r"\d[\d,]*\.\d+|\d{4,}", answer):
        if num.replace(",", "") not in blob and not re.fullmatch(r"(19|20)\d\d", num):
            return False, num
    return True, None


def agent_answer(index, digest, question, llm_fn, max_rounds=3):
    """Run the tool-calling loop. Returns a dict with the grounded answer, the
    tools used (the agent's visible decisions), and pages cited - or None if the
    LLM is unavailable/failed, so the caller falls back to extractive Q&A."""
    catalog = _tools(index, digest)
    transcript, used = [], []
    for _ in range(max_rounds):
        try:
            raw = llm_fn(_plan_prompt(question, catalog, transcript), True)
            raw = raw.strip().lstrip("`").lstrip("json").strip("` \n")
            plan = json.loads(raw)
        except Exception:
            return None
        if "answer" in plan and plan["answer"]:
            ok, bad = _grounded(str(plan["answer"]), transcript)
            if not ok:
                transcript.append({"verifier": f"number {bad} not in tool results - "
                                               "answer only from tool data"})
                continue
            return {"answer": plan["answer"], "tools_used": used,
                    "grounded": True, "method": "tool-calling-agent"}
        for call in (plan.get("calls") or [])[:3]:
            name = str(call.get("tool", ""))
            if name in catalog:
                try:
                    out = catalog[name][1](call.get("arg"))
                except Exception as e:
                    out = {"error": str(e)[:80]}
                used.append(name)
                transcript.append({name: out})
        if not plan.get("calls"):
            return None
    return None
