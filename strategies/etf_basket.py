"""HPPO against its basket: price the ETF from BNKY and BRRY, and close a round trip by conversion.

    pqt backtest strategies/etf_basket.py --data data --name s01
    pqt run strategies/etf_basket.py --mode test --server https://sandbox.princeton-quant.com

(with your algorithm key in ``--key`` or in the environment variable ``PQT_API_KEY``).

HPPO is 1 BNKY + 2 BRRY, and a team may convert between them at any time for $0.02 per HPPO share:
``self.create(n)`` delivers n BNKY and 2n BRRY and gives you n HPPO, ``self.redeem(n)`` the
reverse. So the basket is a price for HPPO built from two other books:

* when HPPO's **bid** is above what the basket **costs** at its asks, by more than the costs, sell
  HPPO and buy the basket, then create HPPO from that basket to deliver the HPPO you sold: flat,
  with the difference locked in;
* when HPPO's **ask** is below what the basket **fetches** at its bids, buy HPPO, sell the basket,
  and redeem.

The costs per HPPO share: four taker fees ($0.004 each: 1 HPPO, 1 BNKY, 2 BRRY) and the conversion
fee, $0.036 in all. What can go wrong: the three orders are IOC and any of them may come back partly
filled (other participants trade in the same batch), which leaves you holding legs. The next batch
converts every complete unit and closes what is left with IOC orders at the touch. BRRY's position
limit (600) caps a conversion at 300 HPPO; its largest order (120) caps a clip at 60. While HPPO is
halted nothing can be converted, so the file waits.

The books rarely cross by that much, and others look for it too. The interesting use of the basket
is as one input to your own fair value of HPPO (and of the basket from HPPO).
"""

from pqt_client.v2 import BatchState, Strategy

ETF = "HPPO"
BASKET = {"BNKY": 1, "BRRY": 2}  # shares per HPPO share
COST = 3.6  # cents per HPPO share: four taker fees and the conversion fee
MARGIN = 1.0  # cents per HPPO share wanted on top of the costs
CLIP = 20  # HPPO shares per attempt, at most
WAIT = 2  # batches to wait for the answers to what was sent


class EtfBasket(Strategy):
    def on_start(self) -> None:
        self.quiet_until = 0  # the batch from which to act again

    def on_batch(self, state: BatchState) -> None:
        if state.batch < self.quiet_until or state.top[ETF].halted:
            return
        if self.settle(state) or self.arbitrage(state):
            self.quiet_until = state.batch + WAIT

    def settle(self, state: BatchState) -> bool:
        """Convert complete units into nothing, then close any leftover leg. True if it sent
        anything."""
        etf, bnky, brry = self.position(ETF), self.position("BNKY"), self.position("BRRY")
        if etf < 0 and bnky > 0 and brry > 1:  # sold HPPO, bought the basket: create
            units = min(-etf, bnky, brry // 2)
            self.create(units)
            self.log.info("create %d HPPO from the basket", units)
            return True
        if etf > 0 and bnky < 0 and brry < -1:  # bought HPPO, sold the basket: redeem
            units = min(etf, -bnky, -brry // 2)
            self.redeem(units)
            self.log.info("redeem %d HPPO into the basket", units)
            return True
        sent = False
        for symbol in (ETF, *BASKET):
            position, top = self.position(symbol), state.top[symbol]
            largest = self.symbol_info[symbol].max_order_qty
            if position > 0 and top.bid_px is not None:
                self.sell(symbol, min(position, largest), top.bid_px, ioc=True)
                sent = True
            elif position < 0 and top.ask_px is not None:
                self.buy(symbol, min(-position, largest), top.ask_px, ioc=True)
                sent = True
        return sent

    def arbitrage(self, state: BatchState) -> bool:
        """Trade all three books at their touch if the basket and HPPO are far enough apart."""
        etf = state.top[ETF]
        legs = {symbol: state.top[symbol] for symbol in BASKET}
        if not all(t.two_sided and not t.halted for t in (etf, *legs.values())):
            return False
        if etf.bid_px is None or etf.ask_px is None or not etf.bid_qty or not etf.ask_qty:
            return False  # (two_sided says so already; this tells the type checker)
        basket_ask = sum(n * (legs[s].ask_px or 0) for s, n in BASKET.items())
        basket_bid = sum(n * (legs[s].bid_px or 0) for s, n in BASKET.items())
        if etf.bid_px - basket_ask > COST + MARGIN:  # HPPO rich: sell it, buy the basket
            qty = min(CLIP, etf.bid_qty, *((legs[s].ask_qty or 0) // n for s, n in BASKET.items()))
            if qty < 1:
                return False
            self.log.info("HPPO bid %d > basket ask %d: %d units", etf.bid_px, basket_ask, qty)
            self.sell(ETF, qty, etf.bid_px, ioc=True)
            for symbol, n in BASKET.items():
                self.buy(symbol, n * qty, legs[symbol].ask_px or 0, ioc=True)
            return True
        if basket_bid - etf.ask_px > COST + MARGIN:  # HPPO cheap: buy it, sell the basket
            qty = min(CLIP, etf.ask_qty, *((legs[s].bid_qty or 0) // n for s, n in BASKET.items()))
            if qty < 1:
                return False
            self.log.info("HPPO ask %d < basket bid %d: %d units", etf.ask_px, basket_bid, qty)
            self.buy(ETF, qty, etf.ask_px, ioc=True)
            for symbol, n in BASKET.items():
                self.sell(symbol, n * qty, legs[symbol].bid_px or 0, ioc=True)
            return True
        return False
