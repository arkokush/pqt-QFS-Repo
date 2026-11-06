"""News helpers for theo_mm.py: a local LLM news reader, a paper book of hypothetical news trades,
and a reader for commands typed into the terminal. No Strategy subclass lives here.

Everything slow or blocking runs in a background thread and hands its results back through a
queue, which theo_mm.py drains at the start of each batch: the trading thread never waits on the
LLM or on your keyboard, and only the trading thread changes the strategy's state.
"""

from __future__ import annotations

import csv
import json
import math
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SYMBOLS = ("HPPO", "BNKY", "CRRT", "BRRY")
LLM_MODELS = {0: None, 1: "qwen2.5:7b", 2: "qwen2.5:14b"}  # the llm_model slider: 0 = off
LLM_NAMES = {"off": 0, "7b": 1, "14b": 2}  # what you type: `llm 14b`
OLLAMA_URL = "http://localhost:11434/api/chat"
LLM_TIMEOUT = 8.0  # seconds: past this a read is given up (logged, never blocks trading)
WARMUP_TIMEOUT = 60.0  # seconds: the first call loads the model into memory (the 14B: ~6-15 s)

SYSTEM = """You read news for a simulated stock market with four symbols: HPPO, BNKY, CRRT, BRRY.
HPPO is an ETF equal to 1 BNKY + 2 BRRY, so news that moves BNKY or BRRY moves HPPO the same way.
For a news item, answer ONLY JSON: {"symbols": {"<SYM>": <-1, 0 or 1>}, "confidence": <0 to 1>, "reason": "<short>"}
Rules: always include every symbol the item names. Use 1 for up, -1 for down, 0 for no clear direction.
Add HPPO when BNKY or BRRY moves. Do not guess effects on symbols the item does not name, other than HPPO.
Scheduled events (an upcoming report or meeting) and results in line with expectations have no clear direction.
confidence: 0.9 or more only when the direction is obvious; 0.5 when unsure; lower for vague or mixed news."""


# ---- logs -----------------------------------------------------------------------------------------

def append_row(path: Path, fields: list[str], row: dict) -> None:
    """Append one row to a CSV log. If the file's header differs (an older version of the code),
    it is renamed to <name>.old-<time>.csv and a fresh file started. Never raises."""
    try:
        path.parent.mkdir(exist_ok=True)
        if path.exists():
            with path.open(newline="") as f:
                header = next(csv.reader(f), None)
            if header != list(fields):
                path.rename(path.with_name(f"{path.stem}.old-{int(time.time())}{path.suffix}"))
        new = not path.exists()
        with path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(fields), extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)
    except (OSError, csv.Error):
        pass  # never let logging stop trading


# ---- reading an answer --------------------------------------------------------------------------

def normalize(answer: dict) -> tuple[dict[str, int], float]:
    """The model's answer as {symbol: -1, 0 or 1} and a confidence. Models sometimes answer "-1"
    as text or 0.5; anything unreadable is dropped."""
    dirs: dict[str, int] = {}
    raw = answer.get("symbols", {}) if isinstance(answer, dict) else {}
    if isinstance(raw, dict):
        for sym, v in raw.items():
            sym = str(sym).upper()
            try:
                x = float(v)
            except (TypeError, ValueError):
                continue
            if sym in SYMBOLS and math.isfinite(x):
                dirs[sym] = 1 if x > 0.3 else -1 if x < -0.3 else 0
    try:
        conf = float(answer.get("confidence", 0)) if isinstance(answer, dict) else 0.0
    except (TypeError, ValueError):
        conf = 0.0
    return dirs, max(0.0, min(1.0, conf)) if math.isfinite(conf) else 0.0


def interpret(dirs: dict[str, int], named: list[str]) -> dict[str, int]:
    """Safeguards on a read: keep only symbols the item names, and set HPPO from its basket
    (1 BNKY + 2 BRRY) whenever BNKY or BRRY has a direction. HPPO keeps the model's own direction
    only if the item names HPPO and neither BNKY nor BRRY moves. Returns only non-zero directions."""
    out = {s: d for s, d in dirs.items() if s in named and s != "HPPO" and d}
    basket = out.get("BNKY", 0) + 2 * out.get("BRRY", 0)
    if basket:
        out["HPPO"] = 1 if basket > 0 else -1
    elif "HPPO" in named and dirs.get("HPPO"):
        out["HPPO"] = dirs["HPPO"]
    return out


# ---- the LLM reader ----------------------------------------------------------------------------

class LLMReader:
    """Asks a local Ollama model about news items, one at a time, in a background thread.
    ``results`` receives one dict per request: kind ("read" or "warmup"), news_id, model, secs,
    dirs, conf, reason, error."""

    def __init__(self) -> None:
        self.results: queue.Queue[dict] = queue.Queue()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm")

    def warmup(self, model: str) -> None:
        self._pool.submit(self._run, "warmup", None, model, "warm-up", "")

    def read(self, news_id: int, model: str, headline: str, body: str) -> None:
        self._pool.submit(self._run, "read", news_id, model, headline, body)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _run(self, kind: str, news_id: int | None, model: str, headline: str, body: str) -> None:
        t = time.time()
        result = {"kind": kind, "news_id": news_id, "model": model, "dirs": {}, "conf": 0.0,
                  "reason": "", "error": None}
        try:  # a warm-up may have to load the model from disk first: give it longer
            answer = self.ask(model, headline, body, timeout=WARMUP_TIMEOUT if kind == "warmup" else LLM_TIMEOUT)
            result["dirs"], result["conf"] = normalize(answer)
            result["reason"] = str(answer.get("reason", ""))[:120] if isinstance(answer, dict) else ""
        except Exception as e:  # server down, model missing, timeout, bad JSON: report, never raise
            result["error"] = f"{type(e).__name__}: {e}"[:160]
        result["secs"] = time.time() - t
        self.results.put(result)

    @staticmethod
    def ask(model: str, headline: str, body: str, timeout: float = LLM_TIMEOUT) -> dict:
        payload = {
            "model": model, "stream": False, "format": "json", "keep_alive": "60m", "think": False,
            "options": {"temperature": 0},
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": f"{headline}\n{body}"}],
        }
        try:
            reply = LLMReader._post(payload, timeout)
        except urllib.error.HTTPError as e:
            if e.code != 400:
                raise
            payload.pop("think")  # a model without a thinking setting: ask without it
            reply = LLMReader._post(payload, timeout)
        return json.loads(reply["message"]["content"])

    @staticmethod
    def _post(payload: dict, timeout: float) -> dict:
        req = urllib.request.Request(OLLAMA_URL, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())


# ---- the paper book ----------------------------------------------------------------------------

PAPER_HORIZONS = (10, 60, 120)  # batches: 5 s, 30 s, 60 s (60 s is how the official score marks a fill)
PAPER_FIELDS = ["session", "mode", "source", "news_id", "batch", "symbol", "side", "qty", "price",
                "mid0", "conf", "real", "pnl10", "pnl60", "pnl120", "move120"]


class PaperBook:
    """Hypothetical news takes: what an aggressive take would have paid (the touch, plus the taker
    fee) and what it would have been worth 5 s, 30 s and 60 s later against the mid. Optimistic:
    it assumes the full size fills at the touch, while live other teams react to the same item."""

    def __init__(self, path: Path, taker_fee_c: float) -> None:
        self.path, self.fee = path, taker_fee_c
        self.open: list[dict] = []
        self.done: list[dict] = []

    def add(self, *, source: str, news_id, batch: int, symbol: str, side: int, qty: int, price: int,
            mid0: float, conf: float | None, real: bool, session: str, mode: str) -> dict:
        t = {"session": session, "mode": mode, "source": source, "news_id": news_id, "batch": batch,
             "symbol": symbol, "side": side, "qty": qty, "price": price, "mid0": mid0,
             "conf": conf, "real": real}
        self.open.append(t)
        return t

    def update(self, batch: int, mids: dict[str, float | None]) -> list[dict]:
        """Mark open trades whose horizons have passed; return the ones finished this batch."""
        finished = []
        for t in self.open:
            m = mids.get(t["symbol"])
            if m is None:
                continue
            for h in PAPER_HORIZONS:
                if f"pnl{h}" not in t and batch - t["batch"] >= h:
                    t[f"pnl{h}"] = round(self.pnl(t, m), 2)
            if batch - t["batch"] >= max(PAPER_HORIZONS):
                t["move120"] = round(t["side"] * (m - t["mid0"]), 1)  # cents moved our way
                finished.append(t)
        for t in finished:
            self.open.remove(t)
            self.done.append(t)
            self.write(t)
        return finished

    def flush(self, mids: dict[str, float | None]) -> None:
        """At the end: what is still open, marked to the last mids."""
        for t in list(self.open):
            m = mids.get(t["symbol"])
            if m is not None:
                for h in PAPER_HORIZONS:
                    t.setdefault(f"pnl{h}", round(self.pnl(t, m), 2))
                t["move120"] = round(t["side"] * (m - t["mid0"]), 1)
            self.open.remove(t)
            self.done.append(t)
            self.write(t)

    def pnl(self, t: dict, mid: float) -> float:
        """Dollars: side x qty x (mid - price), less the taker fee."""
        return (t["side"] * t["qty"] * (mid - t["price"]) - t["qty"] * self.fee) / 100

    def write(self, t: dict) -> None:
        append_row(self.path, PAPER_FIELDS, t)

    def tally(self, source: str) -> tuple[int, int, float]:
        """(trades, right at 60 s, dollars at 60 s) for one source."""
        ts = [t for t in self.done if t["source"] == source and "pnl120" in t]
        return len(ts), sum(t["pnl120"] > 0 for t in ts), sum(t["pnl120"] for t in ts)

    def short(self) -> str:
        parts = []
        for src in ("llm", "team"):
            n, right, usd = self.tally(src)
            if n:
                parts.append(f"{src} {right}/{n} ${usd:+.0f}")
        pending = len(self.open)
        return ("paper " + ", ".join(parts) if parts else "paper -") + (f" ({pending} open)" if pending else "")

    def sensitivity(self) -> list[str]:
        """Per source and symbol: how often the read was right at 60 s, and how far the mid really
        moved in the read's direction (cents, average): the number to size llm_size / move_ from."""
        lines = []
        for src in ("llm", "team"):
            for s in SYMBOLS:
                ts = [t for t in self.done if t["source"] == src and t["symbol"] == s and "move120" in t]
                if not ts:
                    continue
                right = sum(t["pnl120"] > 0 for t in ts)
                avg_move = sum(t["move120"] for t in ts) / len(ts)
                usd = sum(t["pnl120"] for t in ts)
                lines.append(f"  {src:4s} {s}: {len(ts)} reads, {right} right at 60 s, mid moved "
                             f"{avg_move:+.1f}c their way on average, paper ${usd:+.0f}")
        return lines


# ---- terminal commands -------------------------------------------------------------------------

class CommandReader:
    """Reads lines typed into the terminal in a daemon thread; ``lines`` receives each one."""

    def __init__(self) -> None:
        self.lines: queue.Queue[str] = queue.Queue()
        self.thread = threading.Thread(target=self._loop, name="commands", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        try:
            for line in sys.stdin:
                line = line.strip()
                if line:
                    self.lines.put(line)
        except (OSError, ValueError):
            pass  # stdin closed


HELP = """commands (type and press Enter; the GUI sliders do the same, and the last change wins):
  status                     positions, leans, pauses, news and LLM settings
  news                       paper results and sensitivity per source and symbol, recent LLM reads
  llm off | 7b | 14b         which model reads the news (llm_model)
  llm size 4                 LLM lean in cents per read (llm_size; 0 = no lean)
  llm min 0.9                confidence a read needs before it is used (llm_min)
  llm trade off|paper|real   LLM news takes: none / paper only / real takes (llm_trade)
  team trade off|paper|real  your team's move_ takes: none / paper only / real takes (team_trade)
  move BRRY -10              your team's call: total move expected, cents (move_BRRY; 0 clears it)
  set NAME VALUE             any panel slider, e.g. `set size_BNKY 70`, `set on_CRRT 0`
  help                       this list"""
