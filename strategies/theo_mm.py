"""Market making around a model theo, for all four symbols.

    pqt backtest strategies/theo_mm.py --data data --name s01
    pqt run strategies/theo_mm.py --mode test --server https://sandbox.princeton-quant.com

Needs ``strategies/theo_model.json``, written by the export cell in EDA.ipynb (the ridge models:
features, weights, clip bounds). Retrain in the notebook, export, and this file picks it up.

Each batch, for each symbol:

1. **Theo.** Compute the same features as the notebook from this batch's book and tape and the
   history kept here, clip them to the training range, and predict the mid's move over the next
   60 s: ``theo = mid + prediction + bias``. ``bias_<SYM>`` is a panel slider (cents) for a
   teammate to lean the theo by hand, for example on news.
2. **Inventory.** Quote around ``q = theo - skew_<SYM> * position / cap``: when long, both quotes
   move down, so you sell more easily and buy less.
3. **Quote.** Join the best bid and ask (or step ``improve_<SYM>`` ticks inside), but never closer to ``q``
   than ``edge_<SYM>`` cents: ``bid = min(best_bid, q - edge)``, ``ask = max(best_ask, q + edge)``.
   When the theo leans one way, the side that would be picked off steps back by itself.
   A quote is moved (cancel-replace) only when its price should change, so it keeps its queue place.
4. **Take.** When ``q`` is beyond the far side of the book by more than the taker fee plus
   ``take_<SYM>`` cents, cross with an IOC of ``tsize_<SYM>`` shares whose limit is the worst price
   ``q`` still justifies, so it sweeps several levels if they are there. ``take`` is 50 (off) by
   default; while a news lean is active and its source trades for real (below), the margin is
   ``ntake_<SYM>`` (3c) instead, so it takes aggressively toward the target until the price gets there.

**News.** On every item it pulls all resting orders at once (``cancel_all``, one message) and
stands aside in every symbol; each symbol resumes when its book has settled (spread near its usual
width, mid steady), after 5 s at least and 30 s at most. Two sources can then lean the theo:

* **Your team** (``move_<SYM>``): the total move you expect, in cents (+15 = up 15c). Counted from
  the mid when the news arrived; only the part that has not happened yet is added to the theo, so
  the lean shrinks as the market reprices, never exceeds what you entered, and expires after
  2 minutes. Setting it ends that symbol's pause and replaces any LLM lean on it; 0 clears it.
* **The LLM** (``strategies/news_desk.py``): a local Ollama model (``llm_model``: 0 off, 1
  qwen2.5:7b, 2 qwen2.5:14b) reads each item in a background thread, ~1-2 s. A read with
  confidence >= ``llm_min`` leans each symbol it names by ``llm_size`` cents in its direction,
  the same way as a team move (HPPO follows from its basket; a team move on a symbol wins).

``team_trade`` and ``llm_trade`` (0 off, 1 paper, 2 real) say what each source does with its
calls: **paper** records the aggressive take it would have made (the touch, at ``tsize_<SYM>``
for the team and half of it for the LLM) and marks it 5 s / 30 s / 60 s later; **real** also
takes for real. Paper results per source go to the terminal, the per-minute line and
``logs/paper_trades.csv``; the `news` command shows how often each source was right and how far
prices moved (its *sensitivity*). Every setting can be changed in the GUI panel or by typing into
the terminal (``help`` lists the commands); the last change wins.

Position caps (``CAP``) and a soft exposure limit (``EXPOSURE_SOFT``) stop it adding to a position.
It does nothing for the first 60 batches (30 s): the features need that much history.

At the end it prints **markouts**: each fill scored against the mid 120 batches (60 s) later, minus
fees. That is the closest offline proxy for the official score; the backtest's estimated PnL is not.
In a backtest the recorded market never reacts to you, so passive fills look better than they will
live (STRATEGY_GUIDE.md section 11): compare versions with it, don't forecast with it.
"""

from __future__ import annotations

import csv
import json
import math
import queue
import re
import sys
from collections import Counter, deque
from pathlib import Path

from pqt_client.v2 import BatchState, Fill, NewsItem, ParamKind, ParamSpec, Reject, Side, Strategy

from news_desk import HELP, LLM_MODELS, LLM_NAMES, CommandReader, LLMReader, PaperBook, append_row, interpret

MODEL_FILE = Path(__file__).with_name("theo_model.json")
SYMBOLS = ("HPPO", "BNKY", "CRRT", "BRRY")

# Feature definitions: must match EDA.ipynb exactly.
FLOW_WINDOWS = (5, 20, 60)
RET_LAGS = (2, 10, 60)
DEPTH_SPAN = 600  # EWM span of the size at the best price, used to scale flow
WARMUP = max(max(FLOW_WINDOWS), max(RET_LAGS))  # batches of history needed
HORIZON = 120  # markout horizon: 60 s

TAKER_FEE_C = 0.4  # cents per share
MAKER_REBATE_C = 0.2

# Per-symbol settings that are not on the panel.
CAP = {"HPPO": 300, "BNKY": 1000, "CRRT": 1000, "BRRY": 300}  # stop adding beyond this position
EXPOSURE_SOFT = 500  # HPPO-equivalent shares; the exchange's hard limit is 600

# Panel defaults per symbol (every one is a slider named <setting>_<SYMBOL>):
#   edge     cents: the closest a quote may sit to q
#   size     shares per quote
#   take     cents beyond the fee before crossing (50 = never)
#   skew     cents of lean at a CAP-sized position
#   improve  ticks inside the best price (0 = join it)
#   tsize    shares per take (IOC); a take sweeps every level up to the price q justifies
#   ntake    the take margin while a news move (move_<SYM>) is active: setting move_ makes it take
# From research/grid_search.py: skew and take as found (interior optimum / no difference);
# edge and size one step back from the grid's boundary (the backtest's fill model rewards ever
# tighter, ever bigger quotes); improve 1 (2 and 3 were worse even in the backtest).
DEFAULTS = {
    "HPPO": {"edge": 2.0, "size": 40, "take": 50.0, "skew": 10.0, "improve": 1, "tsize": 100, "ntake": 3.0},
    "BNKY": {"edge": 1.0, "size": 100, "take": 50.0, "skew": 3.0, "improve": 1, "tsize": 250, "ntake": 3.0},
    "CRRT": {"edge": 1.0, "size": 100, "take": 50.0, "skew": 3.0, "improve": 1, "tsize": 250, "ntake": 3.0},
    "BRRY": {"edge": 3.0, "size": 30, "take": 50.0, "skew": 6.0, "improve": 1, "tsize": 60, "ntake": 3.0},
}
MAX_ORDER = {"HPPO": 200, "BNKY": 500, "CRRT": 500, "BRRY": 120}

KILL_FLOOR_MILLS = -5_000_000  # -$5,000 realized: the exchange blocks the team below this
REPORT_EVERY = 120  # batches between status lines (one minute)

# News: pull every resting order, then resume each symbol once its book has settled.
NEWS_MIN_PAUSE = 10  # batches (5 s) at least
NEWS_MAX_PAUSE = 60  # batches (30 s) at most
SETTLE_SPREAD_X = 1.5  # settled: spread at most this times its usual width (+1 tick) ...
SETTLE_LOOKBACK = 4  # ... and the mid moved at most a usual spread over this many batches
SPREAD_SPAN = 600  # EWM span of the usual spread, updated outside news pauses
# The move_<SYM> slider: the total move you expect from a news item, in cents.
MOVE_ANCHOR_WINDOW = 240  # batches: a slider set within 2 min of news counts from the mid at the news
MOVE_TTL = 240  # batches: the lean expires 2 min after the slider was set
# The news log: every item, the symbols it names, and how far each mid moved after it.
NEWS_LOG = Path(__file__).resolve().parents[1] / "logs" / "news_log.csv"
NEWS_HORIZONS = (10, 60, 120, 240)  # batches: 5 s, 30 s, 60 s, 2 min
TICKERS = re.compile(r"\b(HPPO|BNKY|CRRT|BRRY)\b")
NEWS_FIELDS = (
    ["session", "mode", "news_id", "batch", "minute", "named", "headline", "body",
     "llm_model", "llm_secs", "llm_conf", "llm_used", "llm_reason", "llm_error"]
    + [f"{k}_{s}" for s in SYMBOLS for k in ("mid0", "pred0", *(f"move{h}" for h in NEWS_HORIZONS), "slider", "llm")]
)
# The LLM news reader (strategies/news_desk.py) and the paper book of news trades.
PAPER_LOG = NEWS_LOG.with_name("paper_trades.csv")
LLM_MAX_AGE = 20  # batches (10 s): a read arriving later than this after its news is logged, not used
LLM_TAKE_FRACTION = 0.5  # an LLM take (real or paper) is this share of tsize_<SYM>
TRADE_MODES = {0: "off", 1: "paper", 2: "real"}  # llm_trade / team_trade
GLOBAL_DEFAULTS = {"llm_model": 2, "llm_size": 3.0, "llm_min": 0.9, "llm_trade": 1, "team_trade": 1}


def _params() -> tuple[ParamSpec, ...]:
    g = GLOBAL_DEFAULTS
    specs = [
        ParamSpec("llm_model", ParamKind.INT, g["llm_model"], 0, 2),  # 0 off, 1 qwen2.5:7b, 2 qwen2.5:14b
        ParamSpec("llm_size", ParamKind.FLOAT, g["llm_size"], 0.0, 30.0),  # cents of lean per read
        ParamSpec("llm_min", ParamKind.FLOAT, g["llm_min"], 0.0, 1.0),  # confidence a read needs
        ParamSpec("llm_trade", ParamKind.INT, g["llm_trade"], 0, 2),  # 0 off, 1 paper, 2 real takes
        ParamSpec("team_trade", ParamKind.INT, g["team_trade"], 0, 2),  # 0 off, 1 paper, 2 real takes
    ]
    for s in SYMBOLS:
        d = DEFAULTS[s]
        specs += [
            ParamSpec(f"on_{s}", ParamKind.BOOL, 1),
            ParamSpec(f"edge_{s}", ParamKind.FLOAT, d["edge"], 0.0, 20.0),
            ParamSpec(f"size_{s}", ParamKind.INT, d["size"], 1, MAX_ORDER[s]),
            ParamSpec(f"take_{s}", ParamKind.FLOAT, d["take"], 0.0, 50.0),
            ParamSpec(f"skew_{s}", ParamKind.FLOAT, d["skew"], 0.0, 20.0),
            ParamSpec(f"improve_{s}", ParamKind.INT, d["improve"], 0, 5),
            ParamSpec(f"bias_{s}", ParamKind.FLOAT, 0.0, -50.0, 50.0),
            ParamSpec(f"move_{s}", ParamKind.FLOAT, 0.0, -100.0, 100.0),
            ParamSpec(f"tsize_{s}", ParamKind.INT, d["tsize"], 1, MAX_ORDER[s]),
            ParamSpec(f"ntake_{s}", ParamKind.FLOAT, d["ntake"], 0.0, 50.0),
        ]
    return tuple(specs)


class TheoMM(Strategy):
    MODEL_PARAMS = _params()
    MODEL_PATH: Path = MODEL_FILE  # override (e.g. in a backtest subclass) to load another model
    LLM_IN_BACKTEST = False  # backtests never call the LLM (there is no news in the data anyway)
    SPECS = {p.name: p for p in MODEL_PARAMS}

    def on_start(self) -> None:
        if not self.MODEL_PATH.exists():
            raise FileNotFoundError(f"{self.MODEL_PATH} not found: run the export cell in EDA.ipynb first")
        model = json.loads(self.MODEL_PATH.read_text())
        self.model = model["symbols"]
        alpha = 2 / (DEPTH_SPAN + 1)
        self.decay = 1 - alpha
        self.last_mid: dict[str, float | None] = {s: None for s in SYMBOLS}
        self.mids = {s: deque(maxlen=WARMUP + 1) for s in SYMBOLS}  # ffilled mid, newest last
        self.flows = {s: deque(maxlen=max(FLOW_WINDOWS)) for s in SYMBOLS}  # signed qty per batch
        self.depth_x: dict[str, float | None] = {s: None for s in SYMBOLS}  # ffilled touch size
        self.depth_num = {s: 0.0 for s in SYMBOLS}  # pandas ewm(adjust=True), kept incrementally
        self.depth_den = {s: 0.0 for s in SYMBOLS}
        self.mid_hist: dict[str, dict[int, float]] = {s: {} for s in SYMBOLS}  # for markouts
        self.theo: dict[str, float] = {}
        self.fills: list[Fill] = []
        self.rejects: Counter[str] = Counter()
        # news
        self.spread_avg: dict[str, float | None] = {s: None for s in SYMBOLS}
        self.news_batch: int | None = None  # batch of the latest news item
        self.news_mid: dict[str, float | None] = {s: None for s in SYMBOLS}  # mids when it arrived
        self.paused: set[str] = set()  # symbols standing aside after news
        self.move_target: dict[str, float] = dict.fromkeys(SYMBOLS, 0.0)  # cents, from the slider
        self.move_anchor: dict[str, float | None] = {s: None for s in SYMBOLS}  # mid the move counts from
        self.move_set_at: dict[str, int] = dict.fromkeys(SYMBOLS, 0)
        self.news_records: list[dict] = []  # this session's items, written when complete
        # settings typed into the terminal, on top of the panel's values (a GUI change clears them)
        self.overrides: dict[str, float] = {}
        # LLM leans: like move_, but from the LLM's read (a team move_ on a symbol replaces it)
        self.llm_target: dict[str, float] = dict.fromkeys(SYMBOLS, 0.0)
        self.llm_anchor: dict[str, float | None] = {s: None for s in SYMBOLS}
        self.llm_set_at: dict[str, int] = dict.fromkeys(SYMBOLS, 0)
        self.llm_reads: list[dict] = []  # every read this session, for `news`
        self.llm_status = "off"
        self.paper = PaperBook(PAPER_LOG, TAKER_FEE_C)
        self.use_llm = self.mode != "backtest" or self.LLM_IN_BACKTEST
        self.reader = LLMReader() if self.use_llm else None
        self.commands = None
        if self.mode in ("test", "live") and sys.stdin is not None and sys.stdin.isatty():
            self.commands = CommandReader()
        self.set_kill_switch(KILL_FLOOR_MILLS)
        self.print_news_history()
        self.log.info("loaded theo model for %s from %s", ", ".join(self.model), self.MODEL_PATH.name)
        self.warm_up_llm()
        if self.commands is not None:
            self.log.info("type `help` and Enter for terminal commands")

    def P(self) -> dict:
        """The settings in force: the panel's values, with anything typed into the terminal on top."""
        p = self.model_params
        p.update(self.overrides)
        return p

    def model_name(self) -> str | None:
        return LLM_MODELS.get(int(self.P()["llm_model"])) if self.use_llm else None

    def warm_up_llm(self) -> None:
        name = self.model_name()
        if self.reader is not None and name:
            self.llm_status = f"{name}: warming up"
            self.reader.warmup(name)
        else:
            self.llm_status = "off"

    # ---- the theo ---------------------------------------------------------------------------

    def update_history(self, state: BatchState) -> None:
        flow_now = dict.fromkeys(SYMBOLS, 0)
        for t in state.trades:
            if t.symbol in flow_now:
                flow_now[t.symbol] += t.qty if t.aggressor == Side.BUY else -t.qty
        for s in SYMBOLS:
            top = state.top[s]
            if top.mid is not None:
                self.last_mid[s] = top.mid
            self.mids[s].append(self.last_mid[s])
            self.flows[s].append(flow_now[s])
            if self.last_mid[s] is not None:
                self.mid_hist[s][state.batch] = self.last_mid[s]
            if top.bid_qty is not None and top.ask_qty is not None:
                self.depth_x[s] = (top.bid_qty + top.ask_qty) / 2
            if self.depth_x[s] is not None:
                self.depth_num[s] = self.depth_x[s] + self.decay * self.depth_num[s]
                self.depth_den[s] = 1 + self.decay * self.depth_den[s]
            if top.spread is not None and s not in self.paused:  # the usual spread, outside news
                a = 2 / (SPREAD_SPAN + 1)
                prev = self.spread_avg[s]
                self.spread_avg[s] = top.spread if prev is None else prev + a * (top.spread - prev)

    def raw_features(self, state: BatchState) -> dict[str, dict[str, float]] | None:
        """Every symbol's features, named as in the notebook; None until there is enough history."""
        if any(len(self.mids[s]) <= WARMUP or self.mids[s][0] is None for s in SYMBOLS):
            return None
        mid = {s: self.mids[s][-1] for s in SYMBOLS}
        depth = {s: self.depth_num[s] / self.depth_den[s] for s in SYMBOLS}
        flow = {
            (s, k): sum(list(self.flows[s])[-k:]) / depth[s] for s in SYMBOLS for k in FLOW_WINDOWS
        }
        ret = {(s, k): mid[s] - self.mids[s][-1 - k] for s in SYMBOLS for k in RET_LAGS}
        gap = mid["HPPO"] - mid["BNKY"] - 2 * mid["BRRY"]
        out = {}
        for s in SYMBOLS:
            top = state.top[s]
            micro, raw_mid = top.microprice, top.mid
            f = {"imbalance": micro - raw_mid if micro is not None and raw_mid is not None else 0.0}
            for k in FLOW_WINDOWS:
                f[f"flow_{k}"] = flow[(s, k)]
            for k in RET_LAGS:
                f[f"ret_{k}"] = ret[(s, k)]
            f["etf_gap"] = gap
            f["hppo_ret_10"] = ret[("HPPO", 10)]
            f["hppo_flow_20"] = flow[("HPPO", 20)]
            out[s] = f
        return out

    def predict(self, s: str, f: dict[str, float]) -> float:
        m = self.model[s]
        pred = m["intercept"]
        for name, w, lo, hi in zip(m["features"], m["weights"], m["clip_lo"], m["clip_hi"]):
            pred += w * min(max(f[name], lo), hi)
        return pred

    # ---- trading ----------------------------------------------------------------------------

    def on_batch(self, state: BatchState) -> None:
        self.update_history(state)
        self.track_news(state.batch)
        self.drain_commands(state)
        self.drain_llm(state)
        self.mark_paper(state)
        self.check_resume(state)
        features = self.raw_features(state)
        if features is None or state.blocked:
            return
        p = self.P()
        for s in SYMBOLS:
            top = state.top[s]
            if top.halted or not top.two_sided or self.last_mid[s] is None:
                continue
            self.theo[s] = (self.last_mid[s] + self.predict(s, features[s]) + p[f"bias_{s}"]
                            + self.lean(s, state.batch)[0])
            if s in self.paused:
                continue  # standing aside after news: its orders were pulled in on_news
            if not p[f"on_{s}"]:
                if self.open_orders(s):
                    self.cancel_all(s)
                continue
            self.trade_symbol(state, s, p)
        if (state.batch + 1) % REPORT_EVERY == 0:
            self.report(state)

    def trade_symbol(self, state: BatchState, s: str, p: dict) -> None:
        top = state.top[s]
        assert top.bid_px is not None and top.ask_px is not None
        assert top.bid_qty is not None and top.ask_qty is not None
        pos, cap = self.position(s), CAP[s]
        q = self.theo[s] - p[f"skew_{s}"] * pos / cap
        improve = int(p[f"improve_{s}"])
        edge, size, take = p[f"edge_{s}"], int(p[f"size_{s}"]), p[f"take_{s}"]
        buy_room, sell_room = self.room(state, s, Side.BUY, size), self.room(state, s, Side.SELL, size)

        # Quote: join (or improve) the touch, never closer to q than edge.
        mine = {side: [o for o in self.open_orders(s) if o.side is side] for side in Side}
        my_bid = mine[Side.BUY][0].price if mine[Side.BUY] else None
        my_ask = mine[Side.SELL][0].price if mine[Side.SELL] else None
        base_bid = top.bid_px if my_bid == top.bid_px else top.bid_px + improve
        base_ask = top.ask_px if my_ask == top.ask_px else top.ask_px - improve
        bid = min(base_bid, math.floor(q - edge), top.ask_px - 1)
        ask = max(base_ask, math.ceil(q + edge), top.bid_px + 1)
        if ask <= bid:
            ask = bid + 1
        # Move the quote that gets out of the way first: a bid moved up to (or past) our resting
        # ask, before that ask moves, would trade with it (a self-trade: $0.10 a share).
        quotes = [(Side.BUY, bid if buy_room > 0 else None, min(size, buy_room)),
                  (Side.SELL, ask if sell_room > 0 else None, min(size, sell_room))]
        if my_ask is not None and bid >= my_ask:
            quotes.reverse()
        for side, price, qty in quotes:
            self.keep_quote(s, side, price, qty, mine)

        # Take: cross when q is beyond the far side by the fee plus a margin, and sweep: the IOC's
        # limit is the worst price q still justifies, so it can take several levels at once.
        # While a news lean is active and its source trades for real (team_trade / llm_trade = 2),
        # the margin is the lower of take and ntake; an LLM take is LLM_TAKE_FRACTION of tsize.
        if self.messages_left() < 1:
            return
        margin = take
        tsize = int(p[f"tsize_{s}"])
        lean, source = self.lean(s, state.batch)
        if lean and source and int(p[f"{source}_trade"]) == 2:
            margin = min(margin, p[f"ntake_{s}"])
            if source == "llm":
                tsize = max(1, round(tsize * LLM_TAKE_FRACTION))
        buy_limit = math.floor(q - TAKER_FEE_C - margin)
        sell_limit = math.ceil(q + TAKER_FEE_C + margin)
        if buy_limit >= top.ask_px and buy_limit < ask:  # (< our own ask: never trade with ourselves)
            qty = self.room(state, s, Side.BUY, tsize)
            if qty > 0:
                self.buy(s, qty, buy_limit, ioc=True)
        elif sell_limit <= top.bid_px and sell_limit > bid:
            qty = self.room(state, s, Side.SELL, tsize)
            if qty > 0:
                self.sell(s, qty, sell_limit, ioc=True)

    def room(self, state: BatchState, s: str, side: Side, size: int) -> int:
        """Shares we may still add on ``side`` within CAP and the soft exposure limit."""
        pos = self.position(s)
        left = CAP[s] - pos if side is Side.BUY else CAP[s] + pos
        w = self.symbol_info[s].exposure_weight_milli / 1000
        sign = 1 if side is Side.BUY else -1
        exposure = state.exposure / 1000
        if w and abs(exposure + sign * w * size) > EXPOSURE_SOFT and abs(exposure + sign * w * size) > abs(exposure):
            return 0
        return max(0, min(left, size))

    def keep_quote(self, s: str, side: Side, price: int | None, size: int, mine: dict) -> None:
        """One resting order on ``side`` at ``price`` (none if None), moved only when needed."""
        orders = mine[side]
        for extra in orders[1:]:
            if self.messages_left() > 0:
                self.cancel(order_id=extra.order_id)
        if self.messages_left() < 1:
            return
        if price is None or size <= 0:
            if orders:
                self.cancel(order_id=orders[0].order_id)
        elif not orders:
            (self.buy if side is Side.BUY else self.sell)(s, size, price)
        elif orders[0].price != price:
            self.replace(order_id=orders[0].order_id, price=price, qty=size)

    # ---- news ---------------------------------------------------------------------------------

    def on_news(self, item: NewsItem) -> None:
        """Pull every resting order at once (one message), and stand aside until the books settle."""
        named = sorted(set(TICKERS.findall(f"{item.headline} {item.body}")))
        self.log.warning("NEWS at batch %d (names %s): %s | %s", item.batch, ", ".join(named) or "none",
                         item.headline, item.body)
        self.cancel_all()
        batch = self.state.batch if self.state is not None else item.batch
        self.news_batch = batch
        self.news_mid = dict(self.last_mid)  # last batch's mids: the book before the news
        self.paused = set(SYMBOLS)
        record = {
            "session": self.session.session_id if self.session else "", "mode": self.mode,
            "news_id": item.news_id, "batch": batch, "minute": round(batch / 120, 2),
            "named": " ".join(named), "headline": item.headline, "body": item.body,
            "_done": False,
        }
        for s in SYMBOLS:
            mid, theo = self.last_mid[s], self.theo.get(s)
            record[f"mid0_{s}"] = mid
            record[f"pred0_{s}"] = round(theo - mid, 2) if theo is not None and mid is not None else None
        self.news_records.append(record)
        name = self.model_name()
        if self.reader is not None and name:
            record["llm_model"] = name
            self.reader.read(item.news_id, name, item.headline, item.body)  # answers in drain_llm

    def track_news(self, batch: int) -> None:
        """Fill in each item's mid moves as their horizons pass; write an item once all are in."""
        for r in self.news_records:
            if r["_done"]:
                continue
            for h in NEWS_HORIZONS:
                if batch - r["batch"] >= h and f"move{h}_HPPO" not in r:
                    for s in SYMBOLS:
                        m0, m = r[f"mid0_{s}"], self.last_mid[s]
                        r[f"move{h}_{s}"] = round(m - m0, 1) if m0 is not None and m is not None else None
            if batch - r["batch"] >= max(NEWS_HORIZONS):
                self.write_news(r)

    def write_news(self, r: dict) -> None:
        r["_done"] = True
        append_row(NEWS_LOG, NEWS_FIELDS, r)  # never raises: logging must not stop trading

    def summarize_news(self, rows: list[dict], title: str) -> None:
        """One line per item: what it named, and each symbol's mid move at 5 s / 30 s / 60 s / 2 min."""
        if not rows:
            return
        self.log.info("%s (%d items): mid moves in cents at %s batches; [slider you set]", title, len(rows),
                      "/".join(str(h) for h in NEWS_HORIZONS))
        for r in rows:
            moves = []
            for s in SYMBOLS:
                vals = "/".join("-" if r.get(f"move{h}_{s}") in (None, "") else f"{float(r[f'move{h}_{s}']):+.0f}"
                                for h in NEWS_HORIZONS)
                slider = r.get(f"slider_{s}")
                moves.append(f"{s} {vals}" + (f" [{float(slider):+g}]" if slider not in (None, "") else ""))
            self.log.info("  %s min %5s names %-9s %s | %s", r.get("session", ""), r.get("minute", ""),
                          r.get("named") or "none", "  ".join(moves), str(r.get("headline", ""))[:70])

    def print_news_history(self) -> None:
        """At the start: earlier sessions' items in the same mode, so R2 starts with R1's lessons."""
        if not NEWS_LOG.exists():
            return
        try:
            with NEWS_LOG.open(newline="") as f:
                rows = [r for r in csv.DictReader(f) if r.get("mode") == self.mode]
        except (OSError, csv.Error) as e:
            self.log.error("could not read %s: %s", NEWS_LOG, e)
            return
        self.summarize_news(rows[-30:], f"news from earlier {self.mode} sessions")

    def check_resume(self, state: BatchState) -> None:
        """Resume a paused symbol once its book looks normal again (or after NEWS_MAX_PAUSE)."""
        if not self.paused or self.news_batch is None:
            return
        elapsed = state.batch - self.news_batch
        if elapsed < NEWS_MIN_PAUSE:
            return
        for s in sorted(self.paused):
            if elapsed >= NEWS_MAX_PAUSE or self.settled(state, s):
                self.paused.discard(s)
                self.log.info("resume %s %.1f s after news", s, elapsed * 0.5)

    def settled(self, state: BatchState, s: str) -> bool:
        top, usual = state.top[s], self.spread_avg[s]
        if not top.two_sided or top.spread is None or usual is None:
            return False
        mids = self.mids[s]
        if len(mids) <= SETTLE_LOOKBACK or mids[-1] is None or mids[-1 - SETTLE_LOOKBACK] is None:
            return False
        moved = abs(mids[-1] - mids[-1 - SETTLE_LOOKBACK])
        return top.spread <= SETTLE_SPREAD_X * usual + 1 and moved <= max(1.0, usual)

    # ---- settings: GUI sliders and terminal commands ------------------------------------------

    def on_model_params(self, changed: dict) -> None:
        """Sliders changed in the GUI. A GUI change replaces anything typed for that setting."""
        for name, value in changed.items():
            self.overrides.pop(name, None)
            self.param_changed(name, value, via="GUI")

    def param_changed(self, name: str, value, via: str) -> None:
        if name.startswith("move_") and name.removeprefix("move_") in SYMBOLS:
            self.set_move(name.removeprefix("move_"), float(value), via)
        elif name == "llm_model":
            self.log.warning("LLM model -> %s (%s)", LLM_MODELS.get(int(value)) or "off", via)
            self.warm_up_llm()
        elif name in ("llm_trade", "team_trade"):
            self.log.warning("%s -> %s (%s)", name, TRADE_MODES.get(int(value), value), via)
        else:
            self.log.info("%s -> %s (%s)", name, value, via)

    def set_param(self, name: str, raw: str) -> None:
        """A setting typed into the terminal: checked against the panel's kind and range."""
        spec = self.SPECS.get(name)
        if spec is None:
            self.log.error("no setting %r (try `help`)", name)
            return
        try:
            value = float(raw)
        except ValueError:
            self.log.error("%s needs a number, got %r", name, raw)
            return
        if spec.kind in (ParamKind.INT, ParamKind.BOOL):
            if value != int(value):
                self.log.error("%s takes whole numbers", name)
                return
            value = int(value)
        lo, hi = (0, 1) if spec.kind is ParamKind.BOOL else (spec.min, spec.max)
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            self.log.error("%s must be between %s and %s", name, lo, hi)
            return
        self.overrides[name] = value
        self.param_changed(name, value, via="terminal")

    def set_move(self, s: str, value: float, via: str) -> None:
        """Your team's call on news: the total move expected, counted from the mid at the latest
        news (if it was within MOVE_ANCHOR_WINDOW) or from the mid now. It replaces any LLM lean on
        the symbol, ends its news pause (you have decided), and is paper-traded if team_trade >= 1."""
        batch = self.state.batch if self.state is not None else 0
        recent = self.news_batch is not None and batch - self.news_batch <= MOVE_ANCHOR_WINDOW
        anchor = self.news_mid[s] if recent and self.news_mid[s] is not None else self.last_mid[s]
        self.move_target[s] = value
        self.move_anchor[s] = anchor if value else None
        self.move_set_at[s] = batch
        if self.llm_target[s]:
            self.log.info("team move on %s replaces the LLM lean", s)
        self.llm_target[s], self.llm_anchor[s] = 0.0, None
        if recent and self.news_records and value:
            self.news_records[-1][f"slider_{s}"] = value  # your read, kept beside the outcome
        if not value:
            self.log.info("news move %s cleared (%s)", s, via)
            return
        self.paused.discard(s)
        mode = int(self.P()["team_trade"])
        self.log.warning("news move %s %+.1fc, counted from mid %.1f (%s; team_trade %s)", s, value,
                         anchor or 0, via, TRADE_MODES[mode])
        if mode >= 1:
            tsize = int(self.P()[f"tsize_{s}"])
            self.add_paper("team", s, 1 if value > 0 else -1, tsize, None, real=mode == 2)

    # ---- leans ------------------------------------------------------------------------------

    def lean(self, s: str, batch: int) -> tuple[float, str | None]:
        """The news lean in force on ``s`` and its source: your team's move_ if set, else the LLM's."""
        team = self._remaining(s, batch, self.move_target, self.move_anchor, self.move_set_at, "news move")
        if self.move_target[s]:
            return team, "team" if team else None
        llm = self._remaining(s, batch, self.llm_target, self.llm_anchor, self.llm_set_at, "LLM lean")
        return llm, "llm" if llm else None

    def news_lean(self, s: str, batch: int) -> float:
        return self.lean(s, batch)[0]

    def _remaining(self, s, batch, targets, anchors, set_at, what) -> float:
        """The part of an expected move that has not happened yet (0 once it has, or expired)."""
        target, anchor = targets[s], anchors[s]
        if not target or anchor is None or self.last_mid[s] is None:
            return 0.0
        if batch - set_at[s] > MOVE_TTL:
            self.log.info("%s %s expired", what, s)
            targets[s], anchors[s] = 0.0, None
            return 0.0
        remaining = target - (self.last_mid[s] - anchor)
        if remaining * target <= 0:
            return 0.0  # the market has moved that far already
        # Never lean more than was entered: if the price goes the other way, the read may be wrong.
        return max(remaining, target) if target < 0 else min(remaining, target)

    # ---- the LLM ----------------------------------------------------------------------------

    def drain_llm(self, state: BatchState) -> None:
        if self.reader is None:
            return
        while True:
            try:
                r = self.reader.results.get_nowait()
            except queue.Empty:
                return
            if r["kind"] == "warmup":
                if r["error"]:
                    self.llm_status = f"{r['model']}: UNAVAILABLE"
                    self.log.error("LLM %s unavailable (%s): is Ollama running and the model pulled? "
                                   "Trading goes on without it.", r["model"], r["error"])
                else:
                    self.llm_status = f"{r['model']}: ready"
                    self.log.info("LLM %s ready (warm-up %.1fs)", r["model"], r["secs"])
            else:
                self.apply_read(state, r)

    def apply_read(self, state: BatchState, r: dict) -> None:
        """An LLM read arrived: log it, keep it beside the news record, and if it is confident
        enough, lean (llm_size) and paper- or real-trade (llm_trade) the symbols it names."""
        record = next((x for x in self.news_records if x["news_id"] == r["news_id"]), None)
        if record is None:
            return
        named = record["named"].split()
        dirs = interpret(r["dirs"], named)
        p = self.P()
        age = state.batch - record["batch"]
        shown = " ".join(f"{s}{d:+d}" for s, d in dirs.items()) or "no direction"
        use = (not r["error"] and dirs and r["conf"] >= p["llm_min"] and age <= LLM_MAX_AGE)
        record.update({"llm_secs": round(r["secs"], 2), "llm_conf": r["conf"], "llm_reason": r["reason"],
                       "llm_error": r["error"] or "", "llm_used": int(bool(use))})
        for s in SYMBOLS:
            record[f"llm_{s}"] = dirs.get(s, 0)
        self.llm_reads.append({"minute": record["minute"], "headline": record["headline"], "dirs": dirs,
                               "conf": r["conf"], "secs": r["secs"], "used": bool(use), "error": r["error"]})
        if r["error"]:
            self.log.error("LLM read failed after %.1fs (%s): %s", r["secs"], r["model"], r["error"])
            return
        why = ("" if use else " -> not used: " + ("stale" if age > LLM_MAX_AGE else
               "no direction" if not dirs else f"confidence below llm_min {p['llm_min']:g}"))
        self.log.warning("LLM (%s, %.1fs): %s conf %.2f - %s%s", r["model"], r["secs"], shown, r["conf"],
                         r["reason"], why)
        if not use:
            return
        size, mode = float(p["llm_size"]), int(p["llm_trade"])
        for s, d in dirs.items():
            if self.move_target[s]:
                continue  # your team has already called this symbol: their call stands
            if size > 0:
                self.llm_target[s], self.llm_anchor[s] = d * size, record[f"mid0_{s}"]
                self.llm_set_at[s] = state.batch
            if mode >= 1:
                tsize = max(1, round(int(p[f"tsize_{s}"]) * LLM_TAKE_FRACTION))
                self.add_paper("llm", s, d, tsize, r["conf"], real=mode == 2)
            if mode == 2:
                self.paused.discard(s)  # real LLM takes go now, not after the news pause
        leans = " ".join(f"{s}{self.llm_target[s]:+.0f}c" for s in dirs if self.llm_target[s])
        if leans:
            self.log.warning("LLM lean %s (llm_trade %s)", leans, TRADE_MODES[mode])

    # ---- the paper book ---------------------------------------------------------------------

    def add_paper(self, source: str, s: str, side: int, qty: int, conf: float | None, real: bool) -> None:
        """The aggressive take a call implies: the touch on the far side. Always recorded in the
        paper book; with ``real`` the same take is also sent (an IOC at the touch, within the caps),
        so paper results are a faithful preview of real ones."""
        state = self.state
        if state is None:
            return
        top = state.top[s]
        price = top.ask_px if side > 0 else top.bid_px
        if top.halted or price is None or self.last_mid[s] is None:
            return
        news_id = self.news_records[-1]["news_id"] if self.news_records else None
        self.paper.add(source=source, news_id=news_id, batch=state.batch, symbol=s, side=side, qty=qty,
                       price=price, mid0=self.last_mid[s], conf=conf, real=real,
                       session=self.session.session_id if self.session else "", mode=self.mode)
        sent = self.news_take(state, s, side, qty, price) if real else 0
        self.log.info("PAPER %s %s %s %d @ %d%s", source, "buy" if side > 0 else "sell", s, qty, price,
                      f" (REAL: sent {sent})" if real else "")

    def news_take(self, state: BatchState, s: str, side: int, qty: int, price: int) -> int:
        """Send one IOC at ``price`` for up to ``qty`` within the position cap and exposure limit.
        Our own resting orders on the other side at or through that price are cancelled first (the
        exchange processes our messages in order), so the take never trades with ourselves."""
        if not self.P()[f"on_{s}"] or state.blocked:
            return 0
        sd = Side.BUY if side > 0 else Side.SELL
        qty = self.room(state, s, sd, qty)
        if qty <= 0:
            return 0
        for o in self.open_orders(s):
            crosses = o.price <= price if sd is Side.BUY else o.price >= price
            if o.side is not sd and crosses and self.messages_left() > 1:
                self.cancel(order_id=o.order_id)
        if self.messages_left() < 1:
            return 0
        (self.buy if sd is Side.BUY else self.sell)(s, qty, price, ioc=True)
        return qty

    def mark_paper(self, state: BatchState) -> None:
        for t in self.paper.update(state.batch, self.last_mid):
            self.log.warning("PAPER %s %s %s %d @ %d: $%+.0f at 5 s, $%+.0f at 30 s, $%+.0f at 60 s "
                             "(mid moved %+.1fc its way) | %s", t["source"], "buy" if t["side"] > 0 else "sell",
                             t["symbol"], t["qty"], t["price"], t.get("pnl10", 0), t.get("pnl60", 0),
                             t.get("pnl120", 0), t["move120"], self.paper.short())

    # ---- terminal commands ------------------------------------------------------------------

    def drain_commands(self, state: BatchState) -> None:
        if self.commands is None:
            return
        while True:
            try:
                line = self.commands.lines.get_nowait()
            except queue.Empty:
                return
            try:
                self.run_command(state, line)
            except Exception as e:  # a typo must never stop trading
                self.log.error("command %r failed: %s", line, e)

    def run_command(self, state: BatchState, line: str) -> None:
        words = line.lower().split()
        cmd, args = words[0], words[1:]
        modes = {"off": 0, "paper": 1, "real": 2}
        if cmd == "help":
            for row in HELP.splitlines():
                self.log.info(row)
        elif cmd == "status":
            self.print_status(state)
        elif cmd == "news":
            self.print_news()
        elif cmd == "llm" and len(args) == 1 and args[0] in LLM_NAMES:
            self.set_param("llm_model", str(LLM_NAMES[args[0]]))
        elif cmd == "llm" and len(args) == 2 and args[0] in ("size", "min"):
            self.set_param(f"llm_{args[0]}", args[1])
        elif cmd in ("llm", "team") and len(args) == 2 and args[0] == "trade" and args[1] in modes:
            self.set_param(f"{cmd}_trade", str(modes[args[1]]))
        elif cmd == "move" and len(args) == 2 and args[0].upper() in SYMBOLS:
            self.set_param(f"move_{args[0].upper()}", args[1])
        elif cmd == "set" and len(args) == 2:
            name = args[0]
            for s in SYMBOLS:  # settings are typed in lower case: put the symbol back in capitals
                if name.endswith("_" + s.lower()):
                    name = name[: -len(s)] + s
            self.set_param(name, args[1])
        else:
            self.log.error("unknown command %r: type `help`", line)

    def print_status(self, state: BatchState) -> None:
        p = self.P()
        self.log.info("STATUS batch %d (minute %.1f) | exposure %+.0f | markouts $%.2f | LLM %s",
                      state.batch, state.minute, state.exposure / 1000, sum(self.markouts().values()) / 100,
                      self.llm_status)
        self.log.info("  llm_model %s, llm_size %gc, llm_min %g, llm_trade %s, team_trade %s",
                      LLM_MODELS.get(int(p["llm_model"])) or "off", p["llm_size"], p["llm_min"],
                      TRADE_MODES[int(p["llm_trade"])], TRADE_MODES[int(p["team_trade"])])
        for s in SYMBOLS:
            lean, src = self.lean(s, state.batch)
            self.log.info("  %s pos %+5d  theo-mid %+5.1fc  %s%s  on %d edge %g size %d tsize %d ntake %g",
                          s, self.position(s),
                          (self.theo.get(s, 0) - (self.last_mid[s] or 0)) if s in self.theo else 0,
                          "PAUSED " if s in self.paused else "", f"lean {lean:+.1f}c ({src})" if lean else "no lean",
                          int(p[f"on_{s}"]), p[f"edge_{s}"], int(p[f"size_{s}"]), int(p[f"tsize_{s}"]),
                          p[f"ntake_{s}"])
        if self.overrides:
            self.log.info("  typed in the terminal (the GUI may show other values): %s", self.overrides)

    def print_news(self) -> None:
        self.log.info("NEWS DESK | %s", self.paper.short())
        lines = self.paper.sensitivity()
        if lines:
            self.log.info("sensitivity (finished paper trades): how often each source was right, and how far "
                          "the mid really moved its way at 60 s, the number to size llm_size and move_ from")
            for row in lines:
                self.log.info(row)
        for r in self.llm_reads[-8:]:
            shown = " ".join(f"{s}{d:+d}" for s, d in r["dirs"].items()) or "none"
            self.log.info("  LLM min %5s %-22s conf %.2f %.1fs %s | %s", r["minute"], shown, r["conf"], r["secs"],
                          "used" if r["used"] else ("ERROR" if r["error"] else "not used"), r["headline"][:60])

    # ---- bookkeeping ------------------------------------------------------------------------

    def on_fill(self, fill: Fill) -> None:
        self.fills.append(fill)

    def on_reject(self, reject: Reject) -> None:
        self.rejects[str(reject.code.value if hasattr(reject.code, "value") else reject.code)] += 1

    def report(self, state: BatchState) -> None:
        parts = []
        for s in SYMBOLS:
            mid, theo = self.last_mid[s], self.theo.get(s)
            lean = f"{theo - mid:+.1f}c" if theo is not None and mid is not None else "-"
            news, src = self.lean(s, state.batch)
            flags = (" PAUSED" if s in self.paused else "") + (f" {src}{news:+.1f}c" if news else "")
            parts.append(f"{s} {self.position(s):+d} ({lean}{flags})")
        news_desk = f" | {self.paper.short()}" if self.paper.done or self.paper.open else ""
        self.log.info(
            "minute %d: %s | exposure %+.0f | est PnL $%.2f | markouts so far $%.2f%s",
            (state.batch + 1) // REPORT_EVERY,
            "  ".join(parts),
            state.exposure / 1000,
            state.pnl_est_mills / 1000,
            sum(self.markouts().values()) / 100,
            news_desk,
        )

    def markouts(self) -> dict[str, float]:
        """Cents per symbol: each fill against the mid HORIZON batches later (or the last mid),
        minus fees. The official score uses the true value instead of the mid."""
        out = dict.fromkeys(SYMBOLS, 0.0)
        for f in self.fills:
            hist = self.mid_hist.get(f.symbol)
            if not hist:
                continue
            last = max(hist)
            b = min(f.batch + HORIZON, last)
            while b not in hist and b > 0:  # a skipped batch (live, if on_batch fell behind)
                b -= 1
            ref = hist.get(b)
            if ref is None:
                continue
            sign = 1 if f.side == Side.BUY else -1
            out[f.symbol] += sign * f.qty * (ref - f.price) - f.fee_mills / 10 - f.penalty_mills / 10
        return out

    def on_end(self, state: BatchState) -> None:
        self.track_news(state.batch)
        for r in self.news_records:
            if not r["_done"]:
                self.write_news(r)  # items too close to the end for every horizon: what there is
        self.summarize_news(self.news_records, f"news this session (saved to {NEWS_LOG.name})")
        self.drain_llm(state)
        self.paper.flush(self.last_mid)
        if self.paper.done:
            self.print_news()
            self.log.info("  (paper trades saved to %s)", PAPER_LOG.name)
        if self.reader is not None:
            self.reader.close()
        marks = self.markouts()
        self.log.info("markouts (fills vs mid 60 s later, after fees; a proxy for the score):")
        for s in SYMBOLS:
            fs = [f for f in self.fills if f.symbol == s]
            shares = sum(f.qty for f in fs)
            maker = sum(f.qty for f in fs if f.liquidity == "MAKER")
            self.log.info(
                "  %s: $%9.2f  over %6d shares (%5.2fc/share), %d fills, %.0f%% maker, final position %+d",
                s,
                marks[s] / 100,
                shares,
                marks[s] / shares if shares else 0.0,
                len(fs),
                100 * maker / shares if shares else 0.0,
                self.position(s),
            )
        self.log.info("  total: $%.2f", sum(marks.values()) / 100)
        if self.rejects:
            self.log.info("  rejects: %s", dict(self.rejects))
