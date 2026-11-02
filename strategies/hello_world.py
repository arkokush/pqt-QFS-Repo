"""Connect, look at the market, trade nothing. Run it first, in every mode.

    pqt backtest strategies/hello_world.py --data data --name s01
    pqt run strategies/hello_world.py --mode test --server https://sandbox.princeton-quant.com

(with your algorithm key in ``--key`` or in the environment variable ``PQT_API_KEY``).

It places no orders, so it cannot lose money and cannot be rejected. Every 10 seconds it prints the
top of book of each symbol, your position in it and your estimated PnL; it prints every news item as
it arrives.
"""

from pqt_client.v2 import BatchState, NewsItem, Strategy

EVERY = 20  # batches between reports: 20 x 500 ms = 10 seconds


class HelloWorld(Strategy):
    def on_start(self) -> None:
        # Everything about the session and its rules is known before the first batch.
        assert self.session is not None
        assert self.limits is not None
        self.log.info(
            "team %s, session %s: %d batches of %d ms",
            self.team_id,
            self.session.session_id,
            self.session.n_batches,
            self.session.batch_ms,
        )
        for symbol, info in self.symbol_info.items():
            self.log.info(
                "  %s: position limit +/-%d, largest order %d, exposure weight %+.3f",
                symbol,
                info.position_limit,
                info.max_order_qty,
                info.exposure_weight_milli / 1000,
            )
        limits = self.limits
        self.log.info(
            "  net exposure limit +/-%d, %d messages per batch, %d resting orders per symbol",
            limits.exposure_limit,
            limits.max_msgs_per_batch,
            limits.max_open_orders_per_symbol,
        )
        self.log.info(
            "  per share: taker fee $%.3f, maker rebate $%.3f, conversion fee $%.3f per %s",
            limits.taker_fee_mills / 1000,
            limits.maker_rebate_mills / 1000,
            limits.conversion_fee_mills / 1000,
            limits.etf,
        )

    def on_batch(self, state: BatchState) -> None:
        if state.batch % EVERY:
            return
        self.log.info(
            "minute %.1f: estimated PnL $%.2f, net exposure %+.1f",
            state.minute,
            state.pnl_est_mills / 1000,
            state.exposure / 1000,
        )
        for symbol, top in state.top.items():
            self.log.info(
                "  %s %-7s bid %s x %s  ask %s x %s  last %s  position %d",
                symbol,
                top.status.value,
                top.bid_px,
                top.bid_qty,
                top.ask_px,
                top.ask_qty,
                top.last,
                state.position(symbol),
            )

    def on_news(self, item: NewsItem) -> None:
        self.log.warning("NEWS at batch %d: %s | %s", item.batch, item.headline, item.body)

    def on_end(self, state: BatchState) -> None:
        self.log.info("session over: estimated PnL $%.2f", state.pnl_est_mills / 1000)
