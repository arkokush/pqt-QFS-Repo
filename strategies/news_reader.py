"""A skeleton for reading the news: find what an item is about, step aside, decide, act.

    pqt backtest strategies/news_reader.py --data data --name s01
    pqt run strategies/news_reader.py --mode test --server https://sandbox.princeton-quant.com

(with your algorithm key in ``--key`` or in the environment variable ``PQT_API_KEY``).

News items are free text (a headline and a body) with the batch they were published in; nothing in
them says which symbol they concern or which way, so reading them is your job. ``on_news`` is
called once per item, before the ``on_batch`` it arrived with; ``state.news_stack`` holds every
item of the session so far (a program that connects late gets the whole stack).

This skeleton finds the symbols an item names, cancels your resting orders in them, and stands
aside from them for ``PAUSE_S`` seconds while ``judge`` (yours to write) decides what the item
means. It trades nothing itself: put your strategy in ``trade``, which is only offered symbols you
are not standing aside from. The historical data has no news, so a backtest never calls
``on_news``; try it on the sandbox, which publishes a test item now and then.
"""

import re

from pqt_client.v2 import BatchState, NewsItem, Strategy

PAUSE_S = 30  # seconds to stand aside from a symbol an item names
TICKERS = re.compile(r"\b(HPPO|BNKY|BRRY|CRRT)\b")


class NewsReader(Strategy):
    def on_start(self) -> None:
        self.aside_until: dict[str, int] = {}  # symbol -> the batch it may be traded again
        assert self.session is not None
        self.pause = PAUSE_S * 1000 // self.session.batch_ms  # in batches

    def on_news(self, item: NewsItem) -> None:
        named = sorted(set(TICKERS.findall(f"{item.headline} {item.body}")))
        self.log.warning("NEWS at batch %d (%s): %s", item.batch, named or "-", item.headline)
        for symbol in named:
            self.cancel_all(symbol)
            self.aside_until[symbol] = item.batch + self.pause
        self.judge(item, named)

    def judge(self, item: NewsItem, named: list[str]) -> None:
        """Your model of what the item means: which symbols, which way, how much, how sure. Keep
        what you conclude in attributes for ``trade`` to use."""

    def on_batch(self, state: BatchState) -> None:
        free = [
            s
            for s, top in state.top.items()
            if not top.halted and state.batch >= self.aside_until.get(s, 0)
        ]
        self.trade(state, free)

    def trade(self, state: BatchState, symbols: list[str]) -> None:
        """Your strategy, for the symbols you are not standing aside from."""
