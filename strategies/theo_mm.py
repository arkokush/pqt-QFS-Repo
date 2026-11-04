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
2. **Inventory.** Quote around ``q = theo - skew * position / cap``: when long, both quotes move
   down, so you sell more easily and buy less.
3. **Quote.** Join the best bid and ask (or step ``IMPROVE`` ticks inside), but never closer to ``q``
   than ``edge_<SYM>`` cents: ``bid = min(best_bid, q - edge)``, ``ask = max(best_ask, q + edge)``.
   When the theo leans one way, the side that would be picked off steps back by itself.
   A quote is moved (cancel-replace) only when its price should change, so it keeps its queue place.
4. **Take.** When ``q`` is beyond the far side of the book by more than the taker fee plus
   ``take_<SYM>`` cents, cross with an IOC.

Position caps (``CAP``) and a soft exposure limit (``EXPOSURE_SOFT``) stop it adding to a position.
It does nothing for the first 60 batches (30 s): the features need that much history.

At the end it prints **markouts**: each fill scored against the mid 120 batches (60 s) later, minus
fees. That is the closest offline proxy for the official score; the backtest's estimated PnL is not.
In a backtest the recorded market never reacts to you, so passive fills look better than they will
live (STRATEGY_GUIDE.md section 11): compare versions with it, don't forecast with it.
"""

from __future__ import annotations

import json
import math
from collections import Counter, deque
from pathlib import Path

from pqt_client.v2 import BatchState, Fill, ParamKind, ParamSpec, Reject, Side, Strategy

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
IMPROVE = {"HPPO": 0, "BNKY": 0, "CRRT": 0, "BRRY": 2}  # ticks inside the best price
EXPOSURE_SOFT = 500  # HPPO-equivalent shares; the exchange's hard limit is 600

# Panel defaults per symbol: (edge cents, size shares, take margin cents)
DEFAULTS = {
    "HPPO": (2.0, 40, 2.0),
    "BNKY": (1.0, 100, 1.5),
    "CRRT": (1.5, 100, 1.5),
    "BRRY": (3.0, 30, 2.0),
}
MAX_ORDER = {"HPPO": 200, "BNKY": 500, "CRRT": 500, "BRRY": 120}

KILL_FLOOR_MILLS = -5_000_000  # -$5,000 realized: the exchange blocks the team below this
REPORT_EVERY = 120  # batches between status lines (one minute)


def _params() -> tuple[ParamSpec, ...]:
    specs = [ParamSpec("skew", ParamKind.FLOAT, 3.0, 0.0, 20.0)]  # cents of lean at a CAP position
    for s in SYMBOLS:
        edge, size, take = DEFAULTS[s]
        specs += [
            ParamSpec(f"on_{s}", ParamKind.BOOL, 1),
            ParamSpec(f"edge_{s}", ParamKind.FLOAT, edge, 0.0, 20.0),
            ParamSpec(f"size_{s}", ParamKind.INT, size, 1, MAX_ORDER[s]),
            ParamSpec(f"take_{s}", ParamKind.FLOAT, take, 0.0, 50.0),
            ParamSpec(f"bias_{s}", ParamKind.FLOAT, 0.0, -50.0, 50.0),
        ]
    return tuple(specs)


class TheoMM(Strategy):
    MODEL_PARAMS = _params()

    def on_start(self) -> None:
        if not MODEL_FILE.exists():
            raise FileNotFoundError(f"{MODEL_FILE} not found: run the export cell in EDA.ipynb first")
        model = json.loads(MODEL_FILE.read_text())
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
        self.set_kill_switch(KILL_FLOOR_MILLS)
        self.log.info("loaded theo model for %s from %s", ", ".join(self.model), MODEL_FILE.name)

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
        features = self.raw_features(state)
        if features is None or state.blocked:
            return
        p = self.model_params
        for s in SYMBOLS:
            top = state.top[s]
            if top.halted or not top.two_sided or self.last_mid[s] is None:
                continue
            self.theo[s] = self.last_mid[s] + self.predict(s, features[s]) + p[f"bias_{s}"]
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
        q = self.theo[s] - p["skew"] * pos / cap
        edge, size, take = p[f"edge_{s}"], int(p[f"size_{s}"]), p[f"take_{s}"]
        buy_room, sell_room = self.room(state, s, Side.BUY, size), self.room(state, s, Side.SELL, size)

        # Quote: join (or improve) the touch, never closer to q than edge.
        mine = {side: [o for o in self.open_orders(s) if o.side is side] for side in Side}
        my_bid = mine[Side.BUY][0].price if mine[Side.BUY] else None
        my_ask = mine[Side.SELL][0].price if mine[Side.SELL] else None
        base_bid = top.bid_px if my_bid == top.bid_px else top.bid_px + IMPROVE[s]
        base_ask = top.ask_px if my_ask == top.ask_px else top.ask_px - IMPROVE[s]
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
            parts.append(f"{s} {self.position(s):+d} ({lean})")
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
