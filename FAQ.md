# Questions and problems

The things that go wrong most often, what the message means, and what to do. If you are still
stuck, bring the output of `pqt check` and the error line to the help desk. The rules themselves
are in the [strategy guide](STRATEGY_GUIDE.md).

## Installing

**Which Python?** 3.10 or newer: `python3 --version` (Windows: `python --version`). On a Mac the
Python that came with the system may be older; install a current one from python.org.

**`pip install` says the package "requires a different Python".** The virtual environment was
made with a Python older than 3.10. Delete `.venv` and make it again with a newer one, for example
`python3.12 -m venv .venv`.

**Windows: activating the environment fails with "running scripts is disabled on this system".**
PowerShell blocks scripts by default. Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
once, or use Command Prompt and `.venv\Scripts\activate.bat` instead.

**`pqt: command not found`** (or "not recognized"). The environment is not active in this
terminal. Every new terminal needs it: `. .venv/bin/activate` (Windows: `.venv\Scripts\activate`).

## Backtesting

**`error: missing data file data/s01.top.csv`.** Put the data pack's files in `data/` (so that
`data/s01.top.csv` and `data/manifest.json` exist) and check `--name`: the sessions are `s01` to
`s24`, as listed in `manifest.json`.

**`error: my_strategy.py must define exactly one Strategy subclass (found: none)`.** The file needs
a class that inherits from `Strategy`: `from pqt_client.v2 import Strategy`. If it has several,
choose one: `my_strategy.py:MyClass`.

**`error: my_strategy.py could not be loaded:` followed by a file, a line and an error.** Your file
has a mistake at that line: a typo, an import that is not installed, or code at the top level that
fails. Fix it there; nothing is wrong with the SDK.

**`KeyError: 'ALPHA'`, or a strategy that sees ticks and books.** That is code for an older version
of the SDK. Strategies for this competition import from `pqt_client.v2`, trade `HPPO`, `BNKY`,
`CRRT` and `BRRY`, and get a `BatchState` in `on_batch`.

**Why does the backtest disagree with the sandbox, or with my score?** A backtest replays recorded
market data: your orders never move it, it fills you whenever the recorded prices allow, and it
marks positions to the final microprices. The live market reacts to you, other teams trade in it
too, and the official score marks every fill against the true value 60 seconds later. Use
backtests to compare versions of an idea, not to predict your score.

## Connecting

Start with `pqt check --server URL --key KEY`. Each line says `OK` or what is wrong.

**`error: the server rejected your API key`.** Copy the key exactly as you received it (spaces and
line breaks around it do not matter). Programs use an **algorithm key**; a member's **login code** is
for the trader GUI. If a key is lost, the help desk can issue a new one.

**`error: cannot reach URL: ... Connection refused`, or a timeout.** The address is wrong, the server
is not up yet, or the network you are on blocks it. Check the address, then try another network
(a phone hotspot is a quick test).

**`FAIL protocol mismatch`.** Your SDK and the exchange speak different versions: install the SDK
from the current kit.

**`WARN your clock is ... ms from the server's`.** Turn on your computer's automatic time setting.

**`pqt run` says "running ... Ctrl-C to stop" and nothing happens.** The session has not started
yet; your program waits for it. The sandbox starts a new session shortly after the last one ends.

**The connection dropped.** You do not need to do anything: the SDK reconnects by itself, and if
the network goes silent without a word (a Wi-Fi drop) it notices within 5 seconds. While you are
away the market goes on, and **your resting orders stay in it** and can still fill. If you would
rather have them cancelled when your connection is lost, run with `--cancel-on-disconnect`.

**I stopped my program, and my orders are still in the book.** That is the rule: orders persist
until they fill, you cancel them, or the session ends. Cancel them in the trader GUI, or start your
program again (it sees them in `self.open_orders()`), or use `--cancel-on-disconnect` next time.

**`the server closed the connection with code 4001 (SUPERSEDED); not reconnecting`.** Another copy
of your program connected with the same key, and the newest one wins. Give each program its own
algorithm key.

**`... code 4005 (KICKED); not reconnecting`.** An organiser disconnected your program. Talk to the
help desk, then start it again.

**Every order is rejected with `BLOCKED`.** Your team is halted: by the organisers (talk to the help
desk), or by your own kill switch, if your realized PnL reached the floor you set (lower or clear
it: `self.set_kill_switch(None)`, or in the trader GUI).

**Orders are rejected with `NOT_RUNNING`.** The session is not running: before its start, while the
organisers have paused it, or after its end.

**The program stopped by itself.** It ends when its session ends. Start it again for the next
session (you can start it early: it waits).

## Trading

**`POSITION_LIMIT` or `EXPOSURE_LIMIT` although my position is small.** Limits count the worst case:
your position plus every open order on that side, as if they all filled. Resting quotes on both
sides use up capacity on both sides. `state.limit_usage` shows where you stand.

**`RATE_LIMIT`.** More than 20 messages from your whole team in one batch, every key and the trader
GUI together. Orders, cancels, replaces and conversions count. `self.messages_left()` tells your
program how many are left.

**`HALTED`.** The symbol is halted: its resting orders were cancelled, new orders are refused, and
(for HPPO) so is conversion. Check `state.top[symbol].halted` before trading a symbol.

**Every symbol is halted and my orders were cancelled with reason `SYSTEM`.** The exchange restarted
or failed. A broadcast says when trading resumes; send your orders again then. Your positions are
kept, and the session's clock and scoring carry on.

**My order traded at a better price than my limit.** A crossing order fills at the resting order's
price.

**A replace sent my order to the back of the queue.** A cancel-replace keeps its place only if it
reduces the size at the same price. Leave an order alone when it is where you want it.

## The trader GUI and your PnL

**Where is it?** At the exchange's address followed by `/trade/`: for the sandbox,
https://sandbox.princeton-quant.com/trade/. Log in with your own login code. It shows the same
account as your programs: positions, orders and fills from every login and key of your team.

**The PnL I see is not my official score.** The PnL shown live is estimated: your cash plus your
positions marked to the microprice. The official score marks each fill against the true value 60
seconds after it, which nobody can see during a session; it comes after the session.

**My model parameters do not appear, or cannot be changed.** Your program declares them
(`MODEL_PARAMS`) when it connects, and the panel cannot change shape while a session runs. Only the
device holding your team's control token can change values; the panel says who holds it, and the
holder can release it.

## On the day

- Use a wired network if you can, keep the laptop plugged in, and stop it from sleeping (on a Mac,
  `caffeinate -i` in another terminal; on Windows, the power settings).
- One program per algorithm key; give each program its own key if you run several.
- Start your program again for every session.
