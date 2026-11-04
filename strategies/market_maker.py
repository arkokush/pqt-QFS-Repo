"""Quote both sides of one symbol around a slow average of its mid, and lean against your inventory.

    pqt backtest strategies/market_maker.py --data data --name s01
    pqt run strategies/market_maker.py --mode test --server https://sandbox.princeton-quant.com

(with your algorithm key in ``--key`` or in the environment variable ``PQT_API_KEY``).

The classic: keep one bid and one ask resting, earn the spread (and the maker rebate) when others
trade with you, and shade both quotes so that a position you do not want drifts back to zero. It
shows how to keep quotes with few messages: an order is placed once, then **moved** with a
cancel-replace only when its price should change, and left alone otherwise (a replaced order goes
to the back of the queue; one left alone keeps its place).

It also declares **model parameters** (``MODEL_PARAMS``): the SDK sends them to the exchange when
it connects, the trader GUI shows them in your team's panel, and ``self.model_params`` has their
current values. A backtest runs with the defaults.

It is the naive version on purpose: it quotes around the market's own price. Whoever trades with
you may know better where the price is going, and markout scoring charges you for that within 60
seconds of every fill. A better fair value than the book's own is your job.

Why a *slow* average, and not the current mid or microprice: your quotes are part of the book. If
you lean around the book's own price, the book moves to where you leaned, the next batch you lean
from there, and the price walks away by your lean every half-second, the faster the more teams run
the same code. Averaging the mid over about 30 seconds (``FAIR_BATCHES``) keeps your lean from
feeding back into your own fair value.

Once a minute it prints its position, its quotes and its estimated PnL, so that a run in a terminal
shows what it is doing (the trader GUI shows more).
"""

import math

from pqt_client.v2 import BatchState, ParamKind, ParamSpec, Side, Strategy

SYMBOL = "BNKY"
CAP = 500  # shares: stop adding to a position beyond this
FAIR_BATCHES = 60  # the fair value averages the mid over about this many batches (30 s)
REPORT_EVERY = 120  # batches between status lines: 120 x 500 ms = one minute


class MarketMaker(Strategy):
    MODEL_PARAMS = (
        ParamSpec("half_spread", ParamKind.INT, 2, 1, 50),  # cents either side of the centre
        ParamSpec("size", ParamKind.INT, 50, 1, 500),  # shares per quote
        ParamSpec("skew", ParamKind.INT, 3, 0, 50),  # cents of lean at a CAP-sized position
    )

    fair: float | None = None  # a slow average of the mid, cents

    def on_batch(self, state: BatchState) -> None:
        if (state.batch + 1) % REPORT_EVERY == 0:
            self.report(state)
        top = state.top[SYMBOL]
        if top.halted or top.mid is None or top.bid_px is None or top.ask_px is None:
            return  # nothing sensible to quote around; resting quotes stay where they are
        if self.fair is None:
            self.fair = top.mid
        else:  # move a little toward the mid each batch (an exponential moving average)
            self.fair += (top.mid - self.fair) / FAIR_BATCHES
        p = self.model_params
        position = self.position(SYMBOL)
        centre = self.fair - p["skew"] * position / CAP  # long: shade both quotes down, sell more
        bid = min(math.floor(centre - p["half_spread"]), top.ask_px - 1)  # never cross the book
        ask = max(math.ceil(centre + p["half_spread"]), top.bid_px + 1)
        size = int(p["size"])
        self.keep_quote(Side.BUY, bid if position < CAP else None, size)
        self.keep_quote(Side.SELL, ask if position > -CAP else None, size)

    def report(self, state: BatchState) -> None:
        """One line a minute: position, resting quotes (in dollars) and estimated PnL."""
        orders = self.open_orders(SYMBOL)
        quotes = {o.side: f"{o.price / 100:.2f}" for o in orders}
        self.log.info(
            "minute %d: %s position %+d, %d open orders (bid %s, ask %s), estimated PnL $%.2f",
            (state.batch + 1) // REPORT_EVERY,
            SYMBOL,
            self.position(SYMBOL),
            len(orders),
            quotes.get(Side.BUY, "-"),
            quotes.get(Side.SELL, "-"),
            state.pnl_est_mills / 1000,
        )

    def keep_quote(self, side: Side, price: int | None, size: int) -> None:
        """Leave exactly one resting order on ``side`` at ``price`` (none if ``price`` is None),
        with as few messages as possible.

        Expect a few ``UNKNOWN_ORDER`` rejects: when a quote fills in the same batch as this
        moves or cancels it, the exchange has no such order left to move. That is harmless: the
        next batch sees the fill and quotes again."""
        mine = [o for o in self.open_orders(SYMBOL) if o.side is side]
        for extra in mine[1:]:
            self.cancel(order_id=extra.order_id)
        if price is None:
            if mine:
                self.cancel(order_id=mine[0].order_id)
        elif not mine:
            if side is Side.BUY:
                self.buy(SYMBOL, size, price)
            else:
                self.sell(SYMBOL, size, price)
        elif mine[0].price != price:
            self.replace(order_id=mine[0].order_id, price=price, qty=size)
