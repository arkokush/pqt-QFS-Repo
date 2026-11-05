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
   ``take_<SYM>`` cents, cross with an IOC.

**News.** On every item it pulls all resting orders at once (``cancel_all``, one message) and
stands aside in every symbol; each symbol resumes when its book has settled (spread near its usual
width, mid steady), after 5 s at least and 30 s at most. The ``move_<SYM>`` slider is your read of
an item: the total move you expect, in cents (+15 = up 15c). It is counted from the mid when the
news arrived, and only the part that has not happened yet is added to the theo, so the lean shrinks
as the market reprices and stops once it has moved that far; it expires after 2 minutes. Setting
it also ends that symbol's pause. Set it back to 0 to clear it early.

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
import re
from collections import Counter, deque
from pathlib import Path

from pqt_client.v2 import BatchState, Fill, NewsItem, ParamKind, ParamSpec, Reject, Side, Strategy

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
# From research/grid_search.py: skew and take as found (interior optimum / no difference);
# edge and size one step back from the grid's boundary (the backtest's fill model rewards ever
# tighter, ever bigger quotes); improve 1 (2 and 3 were worse even in the backtest).
DEFAULTS = {
    "HPPO": {"edge": 2.0, "size": 40, "take": 50.0, "skew": 10.0, "improve": 1},
    "BNKY": {"edge": 1.0, "size": 100, "take": 50.0, "skew": 3.0, "improve": 1},
    "CRRT": {"edge": 1.0, "size": 100, "take": 50.0, "skew": 3.0, "improve": 1},
    "BRRY": {"edge": 3.0, "size": 30, "take": 50.0, "skew": 6.0, "improve": 1},
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
    ["session", "mode", "news_id", "batch", "minute", "named", "headline", "body"]
    + [f"{k}_{s}" for s in SYMBOLS for k in ("mid0", "pred0", *(f"move{h}" for h in NEWS_HORIZONS), "slider")]
)


def _params() -> tuple[ParamSpec, ...]:
    specs = []
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
        ]
    return tuple(specs)


class TheoMM(Strategy):
    MODEL_PARAMS = _params()
    MODEL_PATH: Path = MODEL_FILE  # override (e.g. in a backtest subclass) to load another model

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
        self.set_kill_switch(KILL_FLOOR_MILLS)
        self.print_news_history()
        self.log.info("loaded theo model for %s from %s", ", ".join(self.model), self.MODEL_PATH.name)

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
        self.check_resume(state)
        features = self.raw_features(state)
        if features is None or state.blocked:
            return
        p = self.model_params
        for s in SYMBOLS:
            top = state.top[s]
            if top.halted or not top.two_sided or self.last_mid[s] is None:
                continue
            self.theo[s] = (self.last_mid[s] + self.predict(s, features[s]) + p[f"bias_{s}"]
                            + self.news_lean(s, state.batch))
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
        self.keep_quote(s, Side.BUY, bid if buy_room > 0 else None, min(size, buy_room), mine)
        self.keep_quote(s, Side.SELL, ask if sell_room > 0 else None, min(size, sell_room), mine)

        # Take: cross only when q is beyond the far side by the fee plus a margin.
        if self.messages_left() < 1:
            return
        if q - top.ask_px > TAKER_FEE_C + take and buy_room > 0:
            self.buy(s, min(size, top.ask_qty, buy_room), top.ask_px, ioc=True)
        elif top.bid_px - q > TAKER_FEE_C + take and sell_room > 0:
            self.sell(s, min(size, top.bid_qty, sell_room), top.bid_px, ioc=True)

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
        try:
            NEWS_LOG.parent.mkdir(exist_ok=True)
            new = not NEWS_LOG.exists()
            with NEWS_LOG.open("a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=NEWS_FIELDS, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerow(r)
        except OSError as e:  # never let logging stop trading
            self.log.error("could not write %s: %s", NEWS_LOG, e)

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

    def on_model_params(self, changed: dict) -> None:
        """A move_<SYM> slider changed: the total move you expect, counted from the mid at the
        latest news (if it was within MOVE_ANCHOR_WINDOW) or from the mid now. Setting one also
        ends that symbol's news pause: you have decided."""
        batch = self.state.batch if self.state is not None else 0
        for name, value in changed.items():
            if not name.startswith("move_"):
                continue
            s = name.removeprefix("move_")
            if s not in SYMBOLS:
                continue
            recent = self.news_batch is not None and batch - self.news_batch <= MOVE_ANCHOR_WINDOW
            anchor = self.news_mid[s] if recent and self.news_mid[s] is not None else self.last_mid[s]
            self.move_target[s] = float(value)
            self.move_anchor[s] = anchor if value else None
            self.move_set_at[s] = batch
            if recent and self.news_records and value:
                self.news_records[-1][f"slider_{s}"] = float(value)  # your read, kept beside the outcome
            if value:
                self.paused.discard(s)
                self.log.warning("news move %s %+.1fc, counted from mid %.1f", s, value, anchor or 0)
            else:
                self.log.info("news move %s cleared", s)

    def news_lean(self, s: str, batch: int) -> float:
        """The part of the expected news move that has not happened yet (0 once it has, or expired)."""
        target, anchor = self.move_target[s], self.move_anchor[s]
        if not target or anchor is None or self.last_mid[s] is None:
            return 0.0
        if batch - self.move_set_at[s] > MOVE_TTL:
            self.log.info("news move %s expired (set it again to renew)", s)
            self.move_target[s], self.move_anchor[s] = 0.0, None
            return 0.0
        remaining = target - (self.last_mid[s] - anchor)
        if remaining * target <= 0:
            return 0.0  # the market has moved that far already
        # Never lean more than you entered: if the price goes the other way, your read may be wrong.
        return max(remaining, target) if target < 0 else min(remaining, target)

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
            news = self.news_lean(s, state.batch)
            flags = (" PAUSED" if s in self.paused else "") + (f" news{news:+.1f}c" if news else "")
            parts.append(f"{s} {self.position(s):+d} ({lean}{flags})")
        self.log.info(
            "minute %d: %s | exposure %+.0f | est PnL $%.2f | markouts so far $%.2f",
            (state.batch + 1) // REPORT_EVERY,
            "  ".join(parts),
            state.exposure / 1000,
            state.pnl_est_mills / 1000,
            sum(self.markouts().values()) / 100,
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
            out[f.symbol] += sign * f.qty * (ref - f.price) - f.fee_mills / 10
        return out

    def on_end(self, state: BatchState) -> None:
        self.track_news(state.batch)
        for r in self.news_records:
            if not r["_done"]:
                self.write_news(r)  # items too close to the end for every horizon: what there is
        self.summarize_news(self.news_records, f"news this session (saved to {NEWS_LOG.name})")
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
