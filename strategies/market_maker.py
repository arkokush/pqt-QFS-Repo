"""Quote both sides of one symbol around its microprice, and lean against your inventory.

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
"""

import math

from pqt_client.v2 import BatchState, ParamKind, ParamSpec, Side, Strategy

SYMBOL = "BNKY"
CAP = 500  # shares: stop adding to a position beyond this


class MarketMaker(Strategy):
    MODEL_PARAMS = (
        ParamSpec("half_spread", ParamKind.INT, 2, 1, 50),  # cents either side of the centre
        ParamSpec("size", ParamKind.INT, 50, 1, 500),  # shares per quote
        ParamSpec("skew", ParamKind.INT, 3, 0, 50),  # cents of lean at a CAP-sized position
    )

    def on_batch(self, state: BatchState) -> None:
        top = state.top[SYMBOL]
        fair = top.microprice
        if top.halted or fair is None or top.bid_px is None or top.ask_px is None:
            return  # nothing sensible to quote around; resting quotes stay where they are
        p = self.model_params
        position = self.position(SYMBOL)
        centre = fair - p["skew"] * position / CAP  # long: shade both quotes down, to sell more
        bid = min(math.floor(centre - p["half_spread"]), top.ask_px - 1)  # never cross the book
        ask = max(math.ceil(centre + p["half_spread"]), top.bid_px + 1)
        size = int(p["size"])
        self.keep_quote(Side.BUY, bid if position < CAP else None, size)
        self.keep_quote(Side.SELL, ask if position > -CAP else None, size)

    def keep_quote(self, side: Side, price: int | None, size: int) -> None:
        """Leave exactly one resting order on ``side`` at ``price`` (none if ``price`` is None),
        with as few messages as possible."""
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
