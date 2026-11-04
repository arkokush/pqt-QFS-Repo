# The historical data

What each file and column of the data pack means. The full rules are in [STRATEGY_GUIDE.md](STRATEGY_GUIDE.md).

24 sessions of 45 minutes of the competition's market, generated before the competition, with
parameters drawn afresh for each session from the same distribution as the live sessions. Your
orders and other teams' are not in them. They hold no news and no halts, and nothing about the
true value or the market's parameters.

The pack is a directory with two CSV files per session and one `manifest.json`. The CSV files are
text with a header row, comma separated; **a blank cell means absent, never zero**. The column names
are the API's.

**`<session>.top.csv`**: the top of book at the end of every batch, one row per batch per symbol,
symbols in the order `HPPO, BNKY, CRRT, BRRY`.

| column | type | meaning |
|---|---|---|
| `batch` | integer | the 500 ms batch, from 0 (minute *m* starts at batch 120 *m*) |
| `symbol` | text | `HPPO`, `BNKY`, `CRRT` or `BRRY` |
| `bid_px`, `ask_px` | integer cents, or blank | best bid and ask; blank when that side is empty |
| `bid_qty`, `ask_qty` | integer shares, or blank | everything resting at the best price, all participants together; blank with its price |
| `status` | text | the symbol's trading status (`TRADING` throughout the historical data) |

**`<session>.trades.csv`**: one row per execution, ordered by batch, then symbol, then execution
order.

| column | type | meaning |
|---|---|---|
| `batch`, `symbol` | | as above |
| `price` | integer cents | the execution price: the resting order's |
| `qty` | integer shares | |
| `aggressor` | `BUY` or `SELL` | the side of the arriving order |
| `self_trade` | `true` or `false` | both sides were the same participant |
| `aux` | a JSON object | extra information about the trade, as in the API's `aux`; always `{}` in the historical data |

**`manifest.json`**: `format` (`pqt.history/2`), `batch_ms` (500), `n_batches` (5,400), `symbols`
(each with `tick_size`, `position_limit`, `max_order_qty` and `exposure_weight_milli`, the weight
in thousandths) and `sessions`, the session names (`s01` to `s24`).

```text
batch,symbol,bid_px,bid_qty,ask_px,ask_qty,status
0,HPPO,23577,140,23580,120,TRADING
0,BNKY,11152,250,11157,990,TRADING
```

The last trade price is not a column: it is the latest row of the trades file for that symbol. Any
CSV reader works (`pandas.read_csv`, say; pandas does not come with the SDK: `pip install pandas`),
or let the SDK read and check the files:

```python
from pqt_client.backtest_v2 import load_manifest, load_session

manifest = load_manifest("data")
session = load_session("data", "s01")
first = session.tops[0]["HPPO"]  # Top(bid_px, bid_qty, ask_px, ask_qty, status)
trades = session.trades[0]  # the trades of batch 0
```
