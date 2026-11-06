"""Check local Ollama models as news readers: speed, and how often their reads are right.

    python research/ollama_test.py                         # default model
    python research/ollama_test.py qwen2.5:7b qwen2.5:3b   # compare several models

Needs the Ollama server running (the app, or `ollama serve`) and each model pulled. Run it with
as little else open as possible (no PyCharm, no browser): other programs using the GPU slow the
model down a lot.

Each headline has the answer we expect. A read passes when every expected symbol has the right
direction and no other symbol is given a direction. For "neutral" items (no clear direction),
any read that gives no direction, or only directions at confidence below MIN_CONFIDENCE, passes.
Items marked None are ambiguous: shown, not scored.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
import urllib.error
import urllib.request

MODELS = sys.argv[1:] or ["qwen2.5:7b"]
THINK = False  # no "thinking" before answering (Gemma 4 thinks by default, which takes far too long)
MIN_CONFIDENCE = 0.8  # the strategy would act only on reads at least this confident
SYMBOLS = ("HPPO", "BNKY", "CRRT", "BRRY")

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "strategies"))
from news_desk import SYSTEM  # noqa: E402  the exact instructions the strategy uses live

NEUTRAL = {}
UP_BNKY, DOWN_BNKY = {"BNKY": 1, "HPPO": 1}, {"BNKY": -1, "HPPO": -1}
UP_BRRY, DOWN_BRRY = {"BRRY": 1, "HPPO": 1}, {"BRRY": -1, "HPPO": -1}
UP_CRRT, DOWN_CRRT = {"CRRT": 1}, {"CRRT": -1}
# (headline, body, expected directions, category)
#   expected: NEUTRAL = no clear direction; None = ambiguous (shown, not scored)
#   category: basic   plain good/bad news about one company
#             trap    sounds like news but has no direction (scheduled, in line, denials, "no impact")
#             basket  tests HPPO = BNKY + 2 BRRY (and that CRRT is not in it)
#             multi   several symbols, possibly in opposite directions
#             hard    numbers, "less bad than feared", reactions that contradict the headline, insiders, rumours
TESTS = [
    # basic
    ("BRRY wins major government contract", "Shares expected to benefit.", UP_BRRY, "basic"),
    ("BNKY CEO resigns amid accounting probe", "", DOWN_BNKY, "basic"),
    ("CRRT beats earnings estimates", "Revenue up 20% year on year.", UP_CRRT, "basic"),
    ("BNKY misses revenue guidance and cuts its outlook", "Management cites weak demand.", DOWN_BNKY, "basic"),
    ("CRRT recalls 2 million units over a safety defect", "", DOWN_CRRT, "basic"),
    ("BRRY announces $500M share buyback", "", UP_BRRY, "basic"),
    ("Analyst upgrades BNKY to Buy and raises price target", "", UP_BNKY, "basic"),
    ("Analyst downgrades CRRT to Sell", "Cites rising costs.", DOWN_CRRT, "basic"),
    ("Fire at BRRY's largest warehouse", "Inventory losses expected to be significant.", DOWN_BRRY, "basic"),
    ("CRRT loses key patent case to a rival", "", DOWN_CRRT, "basic"),
    ("CRRT raises full-year guidance for the second time", "", UP_CRRT, "basic"),
    ("BNKY cuts its dividend by 50%", "", DOWN_BNKY, "basic"),
    ("BRRY doubles its dividend", "", UP_BRRY, "basic"),
    ("CRRT data breach exposes millions of customer records", "", DOWN_CRRT, "basic"),
    ("Lawsuit against CRRT dismissed", "", UP_CRRT, "basic"),
    ("Not a good day for BNKY: product recall widens", "", DOWN_BNKY, "basic"),
    # trap
    ("Regulators approve HPPO basket change", "No impact expected.", NEUTRAL, "trap"),
    ("BNKY denies reports of a merger", "", NEUTRAL, "trap"),
    ("CRRT to report earnings next week", "", NEUTRAL, "trap"),
    ("CRRT says it is not affected by sector rumours", "", NEUTRAL, "trap"),
    ("BRRY quarterly results in line with expectations", "", NEUTRAL, "trap"),
    ("Day 2: no news for BNKY today", "", NEUTRAL, "trap"),
    ("BNKY beats on revenue but misses on profit", "", NEUTRAL, "trap"),
    ("BRRY earnings: revenue +2% (expected +2%), profit $1.10 (expected $1.10)", "", NEUTRAL, "trap"),
    ("Weather delays BRRY shipments by one day", "Company says no impact on quarterly results.", NEUTRAL, "trap"),
    ("BNKY to hold annual shareholder meeting on Day 4", "", NEUTRAL, "trap"),
    # basket
    ("BRRY halts production at main plant", "Output expected to fall for several weeks.", DOWN_BRRY, "basket"),
    ("Day 3: BRRY delays new product launch to next quarter", "", DOWN_BRRY, "basket"),
    ("Supplier outage hits CRRT; BNKY unaffected", "BNKY uses a different supplier.", DOWN_CRRT, "basket"),
    ("BRRY plant reopens ahead of schedule after fire", "", UP_BRRY, "basket"),
    ("CRRT's largest customer switches to a rival", "", DOWN_CRRT, "basket"),
    ("BNKY's main competitor goes bankrupt", "", UP_BNKY, "basket"),
    # multi
    ("BNKY and BRRY both report strong quarters", "", {"BNKY": 1, "BRRY": 1, "HPPO": 1}, "multi"),
    ("Regulator approves BNKY's treatment and rejects CRRT's competing application", "",
     {"BNKY": 1, "HPPO": 1, "CRRT": -1}, "multi"),
    ("Bank moves BNKY to Buy and CRRT to Sell", "Analysts swap their preferences.", {"BNKY": 1, "HPPO": 1, "CRRT": -1}, "multi"),
    ("CRRT wins the contract BRRY was expected to get", "", {"CRRT": 1, "BRRY": -1, "HPPO": -1}, "multi"),
    ("BNKY and CRRT both hit by the same cyberattack", "Systems down for two days.", {"BNKY": -1, "HPPO": -1, "CRRT": -1}, "multi"),
    # hard
    ("BNKY shares fall despite record profit", "Investors focus on weaker guidance for next year.", DOWN_BNKY, "hard"),
    ("CRRT losses narrower than expected", "Loss of $0.10 per share vs $0.40 forecast.", UP_CRRT, "hard"),
    ("CRRT earnings: profit $1.50 vs $1.10 expected", "Guidance raised.", UP_CRRT, "hard"),
    ("BNKY earnings: profit $0.80 vs $1.20 expected", "", DOWN_BNKY, "hard"),
    ("BNKY settles lawsuit for far less than feared", "Investors relieved.", UP_BNKY, "hard"),
    ("BRRY CFO buys $2M of shares", "The largest insider purchase in five years.", UP_BRRY, "hard"),
    ("BNKY CEO sells half of his stake", "", DOWN_BNKY, "hard"),
    ("Report: BRRY in talks to be acquired at a 30% premium", "Sources say a deal could come within days.", UP_BRRY, "hard"),
    ("No takeover talks, BRRY says", "Shares had jumped on yesterday's report of a bid.", DOWN_BRRY, "hard"),
    # ambiguous: shown, not scored
    ("Central bank raises rates unexpectedly", "Markets brace for volatility.", None, "ambiguous"),
    ("HPPO sees record inflows", "Investors pile into the fund.", None, "ambiguous"),
    ("CRRT to acquire BNKY's logistics unit at a premium", "BNKY gets cash above book value; CRRT takes on debt.", None, "ambiguous"),
    ("BRRY and CRRT announce a joint venture", "Terms not disclosed.", None, "ambiguous"),
    ("Industry-wide chip shortage eases", "All four companies rely on chips.", None, "ambiguous"),
    ("Lawsuit filed against BNKY over patents", "Damages sought are small.", None, "ambiguous"),
]


def post(payload: dict) -> dict:
    req = urllib.request.Request(
        "http://localhost:11434/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def ask(model: str, headline: str, body: str) -> tuple[dict, float]:
    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "keep_alive": "60m",
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"{headline}\n{body}"},
        ],
    }
    if THINK is not None:
        payload["think"] = THINK
    t = time.time()
    try:
        reply = post(payload)
    except urllib.error.HTTPError as e:
        if e.code != 400 or "think" not in payload:
            raise
        payload.pop("think")  # this model has no thinking setting: ask without it
        reply = post(payload)
    return json.loads(reply["message"]["content"]), time.time() - t


def normalize(answer: dict) -> tuple[dict[str, int], float]:
    """What the strategy would use: each known symbol rounded to -1, 0 or 1 (models sometimes
    answer "-1" as text or 0.5), and the confidence as a number."""
    dirs = {}
    raw = answer.get("symbols", {})
    if isinstance(raw, dict):
        for sym, v in raw.items():
            sym = str(sym).upper()
            try:
                x = float(v)
            except (TypeError, ValueError):
                continue
            if sym in SYMBOLS:
                dirs[sym] = 1 if x > 0.3 else -1 if x < -0.3 else 0
    try:
        conf = float(answer.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    return dirs, conf


def score(expected: dict | None, dirs: dict[str, int], conf: float) -> str:
    if expected is None:
        return "  ?"
    moved = {s: d for s, d in dirs.items() if d}
    if expected == NEUTRAL:
        return " ok" if not moved or conf < MIN_CONFIDENCE else "BAD"
    right = all(dirs.get(s) == d for s, d in expected.items())
    extra = any(s not in expected for s in moved)
    return " ok" if right and not extra else "BAD"


def test_model(model: str) -> dict:
    print(f"\n=== {model} ===")
    try:
        _, secs = ask(model, "warm-up", "")
    except Exception as e:  # not pulled, server down, ...
        print(f"  cannot use {model}: {e}")
        return {"model": model, "error": str(e)}
    print(f"  warm-up {secs:.1f}s")
    times, results = [], []  # results: (category, verdict)
    for headline, body, expected, category in TESTS:
        try:
            answer, secs = ask(model, headline, body)
        except Exception as e:
            print(f"  ERROR {headline[:50]}: {e}")
            results.append((category, "BAD"))
            continue
        dirs, conf = normalize(answer)
        verdict = score(expected, dirs, conf)
        times.append(secs)
        results.append((category, verdict))
        shown = " ".join(f"{s}{d:+d}" for s, d in dirs.items() if d) or "none"
        print(f"  {verdict} {secs:4.1f}s  {category:6s} {headline[:52]:52s} -> {shown:22s} conf {conf:.2f}")
    scored = [(c, r) for c, r in results if r != "  ?"]
    by_cat = {}
    for c, r in scored:
        right, n = by_cat.get(c, (0, 0))
        by_cat[c] = (right + (r == " ok"), n + 1)
    summary = {
        "model": model,
        "right": sum(r == " ok" for _, r in scored),
        "scored": len(scored),
        "by_cat": by_cat,
        "median_s": statistics.median(times) if times else float("nan"),
        "max_s": max(times) if times else float("nan"),
    }
    cats = "  ".join(f"{c} {r}/{n}" for c, (r, n) in by_cat.items())
    print(f"  -> {summary['right']}/{summary['scored']} right ({cats}), median {summary['median_s']:.1f}s, "
          f"slowest {summary['max_s']:.1f}s")
    return summary


if __name__ == "__main__":
    summaries = [test_model(m) for m in MODELS]
    if len(summaries) > 1:
        print("\n=== comparison ===")
        for s in summaries:
            if "error" in s:
                print(f"  {s['model']:16s} unavailable")
            else:
                cats = "  ".join(f"{c} {r}/{n}" for c, (r, n) in s["by_cat"].items())
                print(f"  {s['model']:16s} {s['right']:2d}/{s['scored']} right   median {s['median_s']:4.1f}s   "
                      f"slowest {s['max_s']:4.1f}s   | {cats}")
