# PQT trading competition: starter kit

Everything you need to write, test and run a strategy for the PQT trading competition: the SDK,
four example strategies, the historical data and the guides. You need Python 3.10 or newer.

## 1. Install the SDK

The SDK is two Python packages, attached to this repository's [release](https://github.com/tom05919/pqt-student-kit/releases/tag/v0.2.0):

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install "https://github.com/tom05919/pqt-student-kit/releases/download/v0.2.0/pqt_protocol-0.2.0-py3-none-any.whl" \
            "https://github.com/tom05919/pqt-student-kit/releases/download/v0.2.0/pqt_client-0.2.0-py3-none-any.whl"
pqt --help
```

## 2. Get the data

Download the historical data and unzip it here, so that you have `data/manifest.json`,
`data/s01.top.csv` and so on (24 sessions, `s01` to `s24`):

```bash
curl -LO https://github.com/tom05919/pqt-student-kit/releases/download/v0.2.0/pqt-data-pack.zip && unzip pqt-data-pack.zip
```

(Or download `pqt-data-pack.zip` from the [release](https://github.com/tom05919/pqt-student-kit/releases/tag/v0.2.0) in your browser.)
[DATA.md](DATA.md) says what every file and column means.

## 3. Run a strategy

```bash
pqt backtest strategies/hello_world.py --data data --name s01    # offline: start here
pqt check --server https://sandbox.princeton-quant.com --key YOUR_KEY
pqt run strategies/hello_world.py --mode test --server https://sandbox.princeton-quant.com --key YOUR_KEY
```

The **sandbox**, https://sandbox.princeton-quant.com, is a test exchange with the competition's symbols, rules
and API, but placeholder bots and prices: use it to check that your program connects, trades and
copes. Your team's **algorithm keys** (for programs, `--key` or the environment variable
`PQT_API_KEY`) and each member's **login code** (for the trader GUI) are sent to each team
separately; keep them secret. While your program runs, the trader GUI at https://sandbox.princeton-quant.com/trade/
shows your team's orders, fills, positions and estimated PnL, and lets you trade by hand.

## The example strategies

They are **examples, not alpha**: each shows one pattern. Copy one, rename the class, and make it
yours.

| file | pattern it demonstrates |
|---|---|
| `strategies/hello_world.py` | the lifecycle, the session's rules and the data you get; trades nothing (run this first) |
| `strategies/market_maker.py` | keep one bid and one ask resting, move them with cancel-replace only when needed, lean against inventory; model parameters (`MODEL_PARAMS`) for your team's panel |
| `strategies/etf_basket.py` | price HPPO from its basket (1 BNKY + 2 BRRY), trade all three books, and close the round trip by conversion (`create`, `redeem`); handling partly filled legs and a halt |
| `strategies/news_reader.py` | read the free-text news, find the symbols an item names, step aside from them while you decide; a skeleton for your own strategy |

## Read next

- [STRATEGY_GUIDE.md](STRATEGY_GUIDE.md): the market, how a batch works, orders and conversion,
  fees and limits, how you are scored, the sandbox, and testing a strategy.
- [DATA.md](DATA.md): the historical data, file by file and column by column.
- [FAQ.md](FAQ.md): when something does not work: installing, connecting, trading.

SDK version 0.2.0.
