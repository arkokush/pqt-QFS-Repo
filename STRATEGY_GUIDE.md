# Writing a strategy

How to write, test and run a trading strategy for the PQT competition with the `pqt_client.v2` SDK:
the market, how a batch works, what you see and what you can do, the limits, how you are scored,
and the historical data. This page is handed to students; it uses only the public package. The
[starter strategies](README.md) are complete examples.

You write one Python file with one class. The same file runs offline on the historical data
(**backtest**), on the sandbox exchange (**test**), and in the competition (**live**):

```bash
pqt backtest my_strategy.py --data data --name s01
pqt run my_strategy.py --mode test --server https://sandbox.princeton-quant.com --key YOUR_KEY
pqt run my_strategy.py --mode live --server https://competition-url --key YOUR_KEY
```

`PQT_SERVER` and `PQT_API_KEY` can be set in the environment instead of `--server` and `--key`.

## 1. The market

Four symbols trade in one market shared by all 60 to 70 teams and other participants:

| symbol | what it is | position limit (± shares) | largest order | exposure weight |
|---|---|---|---|---|
| `HPPO` | an ETF: one share is 1 BNKY + 2 BRRY | 1,000 | 200 | 1.00 |
| `BNKY` | a stock | 2,500 | 500 | 0.15 |
| `CRRT` | a stock | 2,500 | 500 | −0.29 |
| `BRRY` | a stock | 600 | 120 | 0.425 |

- **Sessions.** The competition has two sessions, R1 and R2, of 45 minutes each. A session runs in
  **batches** of 500 ms: 5,400 batches, numbered from 0, so minute *m* starts at batch 120 *m*. A
  session is divided into five 8-minute **trading days** (minutes 0 to 40) and 5 more minutes of
  trading; the news may count time in days. Trading stops at 45:00, and every resting order
  expires then.
- **Other participants.** The market has other participants besides the teams. Who they are and
  how they trade is not published. The market's parameters are drawn afresh for every session,
  from the same distribution that generated the historical sessions: you can learn the
  distribution from the data, never the live draw.
- **The true value.** For each symbol the exchange's simulator keeps a hidden true value V, which
  your score uses (section 7). Nobody sees it during the competition; the V paths are published
  afterwards. HPPO's true value is exactly BNKY's plus twice BRRY's.
- **Prices** are whole cents (one tick is $0.01); quantities are whole shares (lot size 1).
- **Money.** Every team starts each session flat: no shares and $0 cash. There is no starting
  balance and no cash limit: cash goes below zero when you buy and above zero when you sell
  (selling short is allowed). What you can hold is limited in shares, by the table above and the
  exposure limit (section 6), never in dollars. There is no loss limit either.

## 2. How a batch works

The exchange collects every message (new order, cancel, cancel-replace, conversion) during a 500 ms
batch and processes them together at its end:

1. It draws a random order of all participants: each team is one participant (all its keys and
   logins together), and so is each of the market's other participants.
2. Participants are processed in that order; each participant's messages in the order they were
   sent.
3. Each new order matches against the book by price, then time, where time is (batch, rank in that
   batch's order). A crossing order fills at the **resting** order's price; an unfilled limit
   remainder rests.
4. Then the market data for the batch is published.

So arriving a few milliseconds earlier is worth nothing, and sending duplicate orders does not buy
an earlier slot. Queue priority across batches still exists: an order that rested in an earlier
batch is ahead of one that arrives later at the same price.

What that means for your code:

```text
 batch b ends ──► its events (acks, fills, ...), then on_news / on_batch(b) run ──► you send orders
                                                                                         │
 batch b+1 ends ◄──────────── your orders are processed here, at your team's slot ◄──────┘
   then their acks / rejects / fills / cancels arrive, and on_batch(b+1) runs
```

- **Nothing is instant.** An order sent from `on_batch(b)` is processed at the end of batch b+1;
  its answer arrives before `on_batch(b+1)`. When `on_batch` runs, `state` and `self.positions`
  already include everything that happened in that batch.
- **The book your order meets is not the book you saw.** Participants ahead of you in the batch's
  order may have traded or moved their quotes.
- **What you send after the last batch is never processed.** `state.final` is true on the last
  batch; `state.batches_left` counts down to it.

## 3. The shape of a strategy

```python
from pqt_client.v2 import BatchState, Strategy


class MyStrategy(Strategy):
    def on_batch(self, state: BatchState) -> None:
        top = state.top["HPPO"]
        if top.ask_px is not None and my_fair_value(state) > top.ask_px + 3:
            self.buy("HPPO", 50, top.ask_px, ioc=True)
```

Subclass `pqt_client.v2.Strategy`, override the hooks you need, and call the trading helpers from
inside them. A file must define exactly one `Strategy` subclass, or you choose with
`my_strategy.py:ClassName`.

| hook | called |
|---|---|
| `on_start()` | once, when the connection is ready and the session's rules are known |
| `on_batch(state)` | every batch (twice a second), after that batch's events: **most strategies trade here** |
| `on_news(item)` | a news item was published; before the `on_batch` it arrived with |
| `on_ack(ack)`, `on_fill(fill)`, `on_reject(reject)` | an order was accepted, traded, or refused (by the exchange, or by the SDK's own checks) |
| `on_cancelled(cancelled)`, `on_replaced(replaced)` | an order stopped resting (`reason` says why); a cancel-replace was applied (`kept_priority`) |
| `on_converted(converted)` | a creation or redemption of HPPO happened |
| `on_model_params(changed)` | your team changed your model parameters (section 8) |
| `on_round_state(round_state)`, `on_broadcast(broadcast)` | the session started, paused, resumed or ended; a message from the organisers |
| `on_reconnect()` | the connection dropped and came back; your resting orders are still there |
| `on_end(state)` | after the last batch |

Facts the SDK fills in before `on_start` (read-only): `self.team_id`, `self.mode` (`"backtest"`,
`"test"` or `"live"`), `self.session` (`session_id`, `n_batches`, `batch_ms`),
`self.symbols`, `self.symbol_info[symbol]` (`tick_size`, `position_limit`, `max_order_qty`,
`exposure_weight_milli`), `self.limits` (the team-wide limits and fees), and `self.log`, a logger
to use instead of `print`. `BatchState` is rebuilt every batch: keep any history in your own
attributes.

## 4. What you see

`on_batch(state)` gets a `BatchState`:

| field | meaning |
|---|---|
| `state.batch`, `state.n_batches`, `state.batches_left`, `state.minute`, `state.final` | where you are in the session |
| `state.top[symbol]` | the **top of book**: `bid_px`, `bid_qty`, `ask_px`, `ask_qty` (None when that side is empty), `status`, `last` (last trade price); and `mid`, `spread`, `microprice`, `two_sided`, `halted`. No deeper levels are published |
| `state.trades` | every trade in the market this batch (the public tape): `symbol`, `price`, `qty`, `aggressor`, `self_trade`, `aux` |
| `state.news`, `state.news_stack` | news published with this batch; every item so far, oldest first |
| `state.portfolio` | your team's `positions`, `cash_mills`, `fees_mills`, `penalties_mills`, `realized_pnl_mills`, `pnl_est_mills` |
| `state.position(symbol)`, `state.open_orders`, `state.orders(symbol)` | your position; your team's resting orders (every key's) |
| `state.limit_usage`, `state.exposure` | worst-case positions and net exposure over your open orders (section 6); the net exposure of your positions, in thousandths |
| `state.session`, `state.phase`, `state.blocked` | the session; whether it is running or paused; whether the organisers have halted your team |

**Units.** Prices are integer **cents** (`10_012` is $100.12). Cash, fees, rebates, penalties and
PnL are integer **mills**, tenths of a cent (`5_460` is $5.46), because the fees are sub-cent.

**Trading status.** Each symbol's `status` is `TRADING`, `HALTED` or `CLOSED` (before the first
and after the last batch). While a symbol is halted its resting orders are cancelled (reason
`HALT`), new orders on it are refused (`HALTED`), its bid and ask are empty and `last` keeps its
pre-halt value; for HPPO, conversion is refused too. Code that relies on a price must check
`top.halted`.

**News** items are free text: a `headline`, a `body` and the `batch` they were published in. They
say nothing machine-readable about which symbol they concern or which way: reading them is your
job. A program that connects late receives the whole stack.

**`aux`.** Every trade on the tape and every one of your fills carries `aux`, a dictionary of
extra information about the trade, often empty. Your code must not assume that any key is present
(use `trade.aux.get(...)`, never `trade.aux[...]`). In the historical data it is always `{}`.

**Estimated PnL** (`pnl_est_mills`, and the GUI's PnL) is cash plus your positions marked to each
symbol's **microprice**, (bid × ask size + ask × bid size) / (bid size + ask size), from the public
top of book; while a symbol is halted, its last microprice before the halt. It is for display: it
is **not your score**, which needs the true value (section 7).

**Never shown during a session:** the true value V, anything derived from the simulator's pricing
model, and the official score, to you or anyone.

**The objects and their fields.** Everything the SDK hands you is read-only and has exactly these
fields. `side`, `aggressor`, `status`, `liquidity`, `order_type`, `code`, `reason`, `kind` and
`phase` are string enums, so `fill.side == "BUY"` works.

| object | where you get it | fields |
|---|---|---|
| `TopView` | `state.top[symbol]` | `symbol`, `bid_px`, `bid_qty`, `ask_px`, `ask_qty` (None when that side is empty), `status`, `last`; and `mid`, `spread`, `microprice`, `two_sided`, `halted` |
| `OpenOrder` | `state.open_orders`, `state.orders(symbol)`, `self.open_orders(symbol)` | `order_id`, `client_id`, `symbol`, `side`, `price`, `remaining` (the shares still resting; there is no `qty`) |
| `Fill` | `on_fill(fill)` | `batch`, `order_id`, `client_id`, `symbol`, `side`, `price`, `qty` (the shares in this fill), `liquidity` (`MAKER` or `TAKER`), `fee_mills` (positive a charge, negative a rebate), `remaining` (what is left of the order), `self_trade`, `penalty_mills`, `aux` |
| `Trade` | `state.trades` | `batch`, `symbol`, `price`, `qty`, `aggressor` (the side of the arriving order), `self_trade`, `aux` |
| `Ack` | `on_ack(ack)` | `batch`, `order_id`, `client_id`, `symbol`, `side`, `order_type` (`LIMIT`, `IOC` or `MARKET`), `price` (for a market order, the cap the exchange applied), `qty` |
| `Reject` | `on_reject(reject)` | `batch`, `code` (section 6), `client_id`, `order_id`, `detail`; any but `code` and `detail` may be None |
| `Cancelled` | `on_cancelled(cancelled)` | `batch`, `order_id`, `client_id`, `symbol`, `side`, `reason` (`USER`, `SYSTEM`, `KILL_SWITCH`, `IOC_REMAINDER`, `HALT` or `SESSION_END`), `remaining` (the shares cancelled) |
| `Replaced` | `on_replaced(replaced)` | `batch`, `order_id`, `client_id`, `symbol`, `side`, `price`, `qty` (the new remaining size), `kept_priority` |
| `Converted` | `on_converted(converted)` | `batch`, `client_id`, `kind` (`CREATE` or `REDEEM`), `qty` (HPPO shares), `fee_mills` |
| `NewsItem` | `on_news(item)`, `state.news`, `state.news_stack` | `news_id`, `batch`, `headline`, `body` |
| `RoundState` | `on_round_state(round_state)` | `phase` (`IDLE` or `ARMED` before the start, then `RUNNING`, `PAUSED`, `ENDED`), `n_batches`, `batch`, `text` |
| `Broadcast` | `on_broadcast(broadcast)` | `text`, `level` |
| `LimitUsage` | `state.limit_usage` | `exposure_low`, `exposure_high` (worst-case net exposure, in thousandths), `symbols[symbol]` with `long_worst`, `short_worst`, `open_orders` |
| `LimitsInfo` | `self.limits`, `state.limits` | `exposure_limit`, `max_open_orders_per_symbol`, `max_msgs_per_batch`, `market_cap_bps`, `taker_fee_mills`, `maker_rebate_mills`, `conversion_fee_mills`, `self_trade_penalty_mills`, `etf`, `basket` |
| `KillSwitch` | `self.kill_switch` | `realized_pnl_floor_mills` (None: off), `engaged` |

## 5. What you can do

| call | what it does |
|---|---|
| `self.buy(symbol, qty, price)`, `self.sell(...)` | a **limit** order: trades what crosses, rests the rest until filled, cancelled or the session ends; returns its `client_id` |
| `self.buy(symbol, qty, price, ioc=True)` | an **IOC** limit order: fills what it can at once, cancels the rest (`IOC_REMAINDER`) |
| `self.buy_market(symbol, qty)`, `self.sell_market(...)` | a **market** order: an IOC priced by the exchange at the best opposite price ±1% (a protective cap); refused with `NO_LIQUIDITY` if that side is empty |
| `self.replace(order_id=..., price=..., qty=...)` | **cancel-replace** a resting order (or by `client_id=`); `qty` is the new remaining size. It keeps its queue place only if it **reduces size at the same price** |
| `self.cancel(order_id=...)`, `self.cancel(client_id=...)`, `self.cancel_all(symbol=None)` | cancel one resting order, or all of them (of one symbol) |
| `self.create(qty)`, `self.redeem(qty)` | **convert**: create delivers `qty` BNKY and 2 × `qty` BRRY and gives you `qty` HPPO; redeem is the reverse |
| `self.set_kill_switch(floor_mills)` | your team's own kill switch (section 8) |
| `self.positions`, `self.position(symbol)`, `self.open_orders(symbol=None)`, `self.messages_left()` | your state, and how many messages this batch's budget has left |

- **Conversion** is for teams only, in any whole quantity, for **$0.02 per HPPO share** either way,
  and it is refused while HPPO is halted. The resulting positions must fit the limits: each HPPO
  converted moves 2 BRRY, so a team flat in BRRY can convert at most 300 HPPO either way.
  Conversion never changes your net exposure (section 6).
- **`client_id`**: the SDK makes a unique one if you do not. Your own is 1 to 32 characters from
  `A-Z a-z 0-9 _ . : -`, unique for your team (all its keys) for the whole session. Use it to cancel
  or replace an order before you know its `order_id`.

## 6. Fees, self-trades and limits

| | per share |
|---|---|
| taker (the arriving order) | $0.004 charge |
| maker (the resting order) | $0.002 rebate |
| conversion | $0.02 per HPPO share |
| self-trade penalty | $0.10, on top of the fees |

**Self-trades** (both sides your team) are allowed, but each self-traded share is flagged
`self_trade` on the public tape and on your fills, and costs the $0.10 penalty. Self-trades add
nothing to your score except the penalty and the fees, and other teams can see and ignore them.

**Limits** are per team: every login and key of a team shares one set of positions, limits and
message budget, held on the exchange. An order that would breach a limit is rejected.

| limit | value | reject code |
|---|---|---|
| position, per symbol | the table in section 1, **worst case**: your position plus all your open orders on that side plus the new order | `POSITION_LIMIT` |
| net exposure | \|E\| ≤ 600, **worst case** over your open orders (below) | `EXPOSURE_LIMIT` |
| largest order | the table in section 1 | `ORDER_SIZE` |
| resting orders | 40 per symbol | `OPEN_ORDERS` |
| messages | 20 per batch for the whole team, all keys and logins together: orders, cancels, replaces and conversions count; heartbeats and the kill switch do not | `RATE_LIMIT` |
| order-to-trade ratio | above 50:1 in a session is flagged for review, not rejected | |

**Net exposure** is measured in HPPO-equivalent shares:
E = pos(HPPO) + 0.15 · pos(BNKY) + 0.425 · pos(BRRY) − 0.29 · pos(CRRT). The weights are fixed,
published numbers for each symbol's sensitivity to HPPO; in any session the real relation differs
somewhat. CRRT's weight is negative, so long BRRY with long CRRT partly offsets, while long BRRY
with short CRRT adds up. One HPPO weighs exactly what 1 BNKY + 2 BRRY weigh, so converting never
moves E. The exchange computes the highest and lowest E you could reach if every open order filled,
and rejects an order that would take either past ±600. Fills therefore never breach a limit; the
price is that resting quotes on both sides use up limit capacity.

**Cash** starts every session at $0 and has no limit. Each share you buy lowers it by the price and
each share you sell raises it, so a long position shows as negative cash and a short one as
positive. Negative cash is not a debt: it is what you paid for the shares you hold. Estimated PnL
(cash plus your positions marked to the microprice, section 4) says how you are doing, and your
score is your markouts (section 7). The share limits above are what cap your cash: about half a
million dollars either way at typical prices.

There is **no loss limit**. Your team may set its own kill switch (section 8), and the organisers
can halt any team (cancel its orders and refuse new ones: `state.blocked`, rejects `BLOCKED`).

**The SDK's local checks.** The SDK refuses an order *locally* only when the exchange is certain to
refuse it: you get `on_reject` with the right code and a `detail` starting `client-side`, nothing
is sent, and no message of your budget is used. It never refuses what the exchange might accept,
so an order that passes can still be rejected by the exchange, which is always the authority.

| reject code | it means |
|---|---|
| `RATE_LIMIT` | more than 20 messages from your team in this batch |
| `BLOCKED` | your team is halted (by the organisers, or by your own kill switch) |
| `NOT_RUNNING` | the session is not running (before the start, paused, or over) |
| `MALFORMED`, `BAD_REQUEST` | a message the exchange cannot read; a request that makes no sense (a market order with a price, a limit order without one, a cancel with no identifier or two) |
| `UNKNOWN_SYMBOL`, `UNKNOWN_ORDER` | no such symbol; no such resting order (already filled or cancelled) |
| `BAD_CLIENT_ID`, `DUPLICATE_CLIENT_ID` | a malformed `client_id`; one your team already used this session |
| `BAD_QTY`, `BAD_PRICE` | not a positive whole number of shares; not a positive whole number of cents |
| `ORDER_SIZE`, `OPEN_ORDERS` | above the symbol's largest order; already 40 resting orders on that symbol |
| `POSITION_LIMIT`, `EXPOSURE_LIMIT` | the worst-case position, or net exposure, would pass its limit |
| `HALTED` | the symbol is halted (or, for a conversion, HPPO is) |
| `NO_LIQUIDITY` | a market order against an empty side |

## 7. Scoring

Each of your fills is scored against the true value 60 seconds later. For your fills *i* (signed
quantity q<sub>i</sub>, positive for a buy; price p<sub>i</sub>; time t<sub>i</sub>; fees
f<sub>i</sub>, positive for a cost, negative for a rebate), with **h = 60 s** and T the session's
end (45:00):

```text
score = sum over fills i of [ q_i * (R_i - p_i) - f_i ]  -  penalties

R_i = V(min(t_i + h, T))  -  ΔN(t_i, t_i + h]
```

- R<sub>i</sub> is the fill's reference value: the true value h after the fill, or at the end of
  the session if that comes first. ΔN removes the jump of any news item announced after the fill
  and within its 60 seconds, so nobody is credited or charged for news they could not have known.
- **Conversions** score nothing apart from their fee, because HPPO's true value is exactly the
  basket's.
- **Penalties:** $0.10 per self-traded share.
- **Open positions are never penalised**, and positions left at the end are not valued: every fill
  is already marked h later, so the score does not depend on your end-of-session position at all.
- **Final score = R1 score + 2 × R2 score**, in dollars. **Tie-break:** realized PnL, with open
  positions valued at the true value at the end.

### Why markouts, and not plain profit

Realized PnL (with open positions valued at the true value at the end, V(T)) splits exactly into
your score plus a luck term L:

```text
realized PnL = score + L,     L = sum over fills i of q_i * (V(T) - V(t_i + h))
```

L is the gain or loss from the true value moving while you hold a position for longer than 60
seconds after a fill. Its expected value is zero **for every strategy**: the true value has no
predictable drift, and no information in the market runs more than 60 seconds ahead of it, so
what V does after t + h cannot be predicted from anything known at t. Removing L keeps your
expected result exactly the same and takes away most of its randomness. That is why h is 60
seconds: shorter would stop charging you for trading against someone who knew better, longer would
add luck back.

What it means for you:

- **Credited:** anything that predicts the value or the price within 60 seconds of a fill. Buying
  below the value and selling above it (which is how providing liquidity pays), avoiding fills that
  the value then moves against within a minute, reacting fast and correctly to news, the legs of an
  arbitrage, and round trips: a round trip's markouts equal its realized price difference plus only
  the value's moves inside the two 60-second windows.
- **Removed:** profit or loss from the value moving while you hold a position for longer than 60
  seconds. A large directional position held for minutes has an expected score of zero and almost
  no score variance: it costs spreads, fees and limit capacity, and gains nothing.
- **Punting does not pay.** The only luck left comes from position changes within the last
  minute. Flipping a large position back and forth every minute keeps paying the spread and the
  fees each time, so its expected score is negative, and the luck it adds is small compared with
  that cost.

**Not live.** The official score needs the true value, so it cannot be shown during a session (even
your own would reveal V with a minute's delay). During a session you see estimated PnL marked to
the microprice, which can differ a lot from the score. Official scores come after each session,
with a report for every team: score by symbol, markout curves at 1, 10, 30 and 60 seconds, and
realized PnL against score. The true-value paths and every team's breakdown are published after the
competition.

## 8. Your team on the exchange

- **Logins and keys.** Each member of your team has a **login code** for the trader GUI, and your
  programs connect with the team's **algorithm keys**; both are sent to each team separately. They
  all trade one account: one set of positions, limits and message budget, held on the exchange, so
  there is nothing to keep in sync. Every connection receives every event of the team, whichever
  login or key caused it. A second copy of your program with the **same key** replaces the first
  (closed with code 4001, and it does not reconnect): give each program its own key.
- **The trader GUI** is at the exchange's address followed by `/trade/` (for the sandbox,
  https://sandbox.princeton-quant.com/trade/), where members log in with their login codes: order
  entry, top of book, the trade tape, your
  fills and open orders (with cancel and replace), positions, limit usage, estimated PnL, the news,
  conversion, your kill switch and your model parameters. It never shows the true value.
- **Model parameters.** Declare the parameters of your model as `MODEL_PARAMS` (name, type, range;
  `market_maker.py` shows how). The SDK sends them to the exchange when it connects, the GUI shows
  them as your team's model-control panel, and `self.model_params` holds their current values (the
  defaults until changed; a backtest uses the defaults). Your team has one **control token**: the
  member whose GUI holds it can change the values (each is checked against its type and range), and
  every other device shows the panel read-only until it is released (or its holder disconnects). A
  change reaches every program of the team through `on_model_params`; programs never change values
  themselves. The panel can change shape only between sessions: during a session, a program
  declaring different parameters is refused (`BAD_REQUEST`) and runs with its own defaults.
- **Your own kill switch** (off by default): `self.set_kill_switch(-2_000_000)` sets a floor of
  −$2,000 on your team's **realized** PnL (`portfolio.realized_pnl_mills`); `None` turns it off.
  When realized PnL falls to the floor, the exchange cancels the team's resting orders and refuses
  new ones (`BLOCKED`) until the floor is lowered or cleared; `self.kill_switch.engaged` says so. It
  applies to the whole team for the rest of the session it was set in (an exchange restart keeps
  it). R1 and R2 each start with it off, so set it in each session, for example in `on_start`; on
  the sandbox it carries from one session to the next. It is not a trading message; backtests
  apply it too.
- **Disconnects.** Your resting orders **stay** in the market when your program disconnects or
  stops (Ctrl-C included); the market goes on without you. To have a key's orders cancelled when
  its connection is lost, opt in: `pqt run ... --cancel-on-disconnect` (in the trader GUI, tick the
  box under the login code). The SDK sends a heartbeat every second; the exchange drops a
  connection it has not heard from for 15 seconds. The SDK
  reconnects by itself (after 0.5 s, then longer, up to 5 s), catches up from a snapshot, and calls
  `on_reconnect`.
- **If the exchange restarts or fails** during a session, every symbol is halted and every resting
  order in the market is cancelled (reason `SYSTEM`); a broadcast says so, and another says when
  trading resumes. Nothing rests from before: send your orders again after the resume. The session's
  clock, the true value and the scoring carry on through the gap.
- **A slow strategy** cannot get you disconnected: heartbeats run apart from your code. If you
  fall behind, the SDK keeps your state current and calls `on_batch` only for the newest batch,
  never an old one.
- **An exception in your code** is logged and the next batch carries on (live), or stops the run
  so you see it (backtest).

## 9. The sandbox

https://sandbox.princeton-quant.com is a test exchange that opens before the competition, so you
can check your connection, your order entry and the API. Sessions of 45 minutes run back to back
while it is up. It has the competition's symbols, rules, limits, fees and API, but its other
participants and its prices are **placeholders that differ from the real ones**: nothing learned
there about them transfers to the competition. It publishes a test news item now and then, and the
organisers may halt a symbol there so you can test that your code copes.

```bash
pqt check --server https://sandbox.princeton-quant.com --key YOUR_KEY
pqt run strategies/hello_world.py --mode test --server https://sandbox.princeton-quant.com --key YOUR_KEY
```

## 10. The historical data

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

## 11. Testing a strategy

| | speed | what it tells you | what it cannot |
|---|---|---|---|
| **backtest** | seconds | how your logic fares on the historical sessions | impact: your orders never move the recorded market, and nobody reacts to you |
| **test mode** (the sandbox) | real time | that your program connects, trades and survives the rules and the API | anything about the real market's other participants or prices |

**Backtest.** `pqt backtest` replays one session through the same SDK core as a live connection,
with the exchange's checks (limits, fees, the message budget, halts).

**What a backtest cannot show.** The recorded market never reacts to your orders: nobody trades
with your quotes because they are there, nobody takes a price before you, nobody moves away. So:

- **Resting quotes look far better than they will live.** The recorded trades happened without
  your quotes in the book. Live, the participants who trade with a quote are often the ones who
  expect the price to move against it, and stale quotes are picked off first. A quoting strategy
  that makes money in a backtest can lose it live.
- **IOC and market orders are never beaten to the price.** An order you send after batch b meets
  the touch recorded at the end of batch b, the one you saw, whatever happened in batch b+1. It
  fills at that price, in full whenever that touch was big enough. Live, participants ahead of you
  in the batch's order may take or move that touch first, so an IOC can fill partly or not at all.
  A backtest therefore shows no leg risk: every leg of an arbitrage fills there, while live one
  leg can fill and another not.

The fill model in full, approximate because the data has only the top of book and your orders are
not in it:

- a marketable order (IOC, market, or the crossing part of a limit order) fills against the touch
  recorded at the end of the previous batch (the one you saw), at its price, up to its size; each
  touch's size is used once per batch by all your orders on that side together. Nothing deeper
  than the touch is in the data, so a market order never fills beyond it, and the rest of an IOC
  or market order is cancelled (`IOC_REMAINDER`);
- a resting order fills only when a recorded trade in a later batch prints **strictly through** its
  price (a trade at exactly your price does not fill you: your queue position is unknown), at your
  price, each trade filling at most its own size;
- your own resting orders are in the book too: an order of yours that crosses them trades with
  them as a self-trade, penalty included;
- what you send after batch b is processed in batch b+1, as live; fees, rebates, penalties,
  realized PnL and your kill switch follow the exchange's rules, and estimated PnL is marked to the
  last microprices.

```console
$ pqt backtest strategies/market_maker.py --data data --name s01
MarketMaker on s01: 5400 batches (45 min)
  estimated PnL $470.05 (marked to the final microprices), realized $466.43, fees $-52.01
  fills 732 (26,007 shares), rejects: none
  final positions {'BNKY': 145}
  (approximate: top of book only, and your orders never move the recorded market; this is not the official score)
```

Treat it as a sanity check and a way to compare versions of an idea, not a forecast: the backtest
fills you whenever the recorded market allows, and it cannot charge you for the adverse selection
that the live market, and markout scoring, will. To run every session from Python:

```python
from pqt_client.backtest_v2 import load_manifest, load_session
from pqt_client.v2 import backtest_strategy

for name in load_manifest("data").sessions:
    result = backtest_strategy(load_session("data", name), MyStrategy())
    print(name, result.pnl_est_mills / 1000, result.fills, dict(result.positions))
```

**Test mode** runs your program on the sandbox (section 9); the trader GUI shows what it did.

## 12. What we do not tell you

Deliberately not published: who the market's other participants are and how they trade, the
market's parameters, and the distributions those are drawn from (the historical data is generated
from the same distributions, so learning them from it is part of the competition); the parameters
of any live session; and the true value, until after the competition. Everything a session holds
beyond its rules is for you to observe, in the market data and in the news, as it happens.

## 13. Running in the competition

- **Check first:** `pqt check --server URL --key KEY` finds your exchange, authenticates, compares
  protocol versions, and measures the round trip and your clock offset. Fix every line it flags.
- **Start your program for every session**; it ends when the session does. You can start it early:
  it waits for the start.
- **Use a wired network** if you can, keep the laptop plugged in and awake. Latency of tens of
  milliseconds does not matter with 500 ms batches; dropped connections do.
- **One key is one connection.** A second program started with the same key, or a second copy of
  yours, closes the first one's connection (code 4001, `SUPERSEDED`; it does not reconnect).
  Give each program its own algorithm key if you run several (`pqt check` does not count).
- Remember that your resting orders stay when your program stops, unless you chose
  `--cancel-on-disconnect`.

The error messages you may meet, and what to do about each, are in [the FAQ](FAQ.md).
