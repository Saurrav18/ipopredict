"""
drhp_log.py - structured logging for the scanner.

WHY: the audit's worst finding was 29 silent `except: pass` sites. Every
expensive bug in the 38-iteration history (false "no lawsuits", stale cache,
Cerebras 404s) was expensive BECAUSE it failed silently and needed a live user
to notice. This module makes failure visible without changing behaviour:
callers still degrade gracefully, but the degradation now leaves a trace.

Design constraints:
- One JSON line per event (machine-parseable on Render's log stream).
- The logger itself must NEVER raise - a logging failure inside an exception
  handler would convert graceful degradation into a crash.
- Request IDs tie every line of one scan/ask together (LLM_DEBUG never could).
"""
import json, os, sys, time, uuid

_LEVEL = {"debug": 10, "info": 20, "warn": 30, "error": 40}
_MIN = _LEVEL.get(os.environ.get("LOG_LEVEL", "info"), 20)


def _emit(level, event, kv):
    if _LEVEL.get(level, 20) < _MIN:
        return
    try:
        rec = {"ts": round(time.time(), 3), "lvl": level, "event": event}
        rec.update({k: (str(v)[:300] if not isinstance(v, (int, float, bool, type(None))) else v)
                    for k, v in kv.items()})
        sys.stdout.write(json.dumps(rec, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:
        pass                      # a logger that can crash is worse than none


def debug(event, **kv): _emit("debug", event, kv)
def info(event, **kv):  _emit("info", event, kv)
def warn(event, **kv):  _emit("warn", event, kv)
def error(event, **kv): _emit("error", event, kv)


def swallowed(where, exc, note=""):
    """The ONE call every formerly-silent except now makes: same graceful
    degradation as before, but the event is on the record with its cause."""
    warn("exception_swallowed", where=where,
         err=f"{type(exc).__name__}: {exc}", note=note)


def new_request_id():
    return uuid.uuid4().hex[:12]
