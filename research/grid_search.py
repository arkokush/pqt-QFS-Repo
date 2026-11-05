"""Full per-symbol grid search of theo_mm.py's panel settings, with one fixed theo model.

    python research/grid_search.py                 # run (resumes if interrupted), then analyse
    python research/grid_search.py --runs 4        # a quick test: only the first 4 runs
    python research/grid_search.py --analyze       # analyse what is in the results file so far

Every combination of every symbol's settings jointly is ~2e10 runs, so instead each symbol gets
its own full grid (edge x skew x size x improve x take), and all four symbols are tested at once:
run i gives each symbol one combination from its own (shuffled) grid, and a backtest reports
markouts per symbol. The symbols interact only through the shared exposure limit; shuffling pairs
each symbol's combinations with random settings of the others, so that interaction averages out.
The largest grid (BRRY's) sets the number of runs; smaller grids repeat, giving extra samples.

Each run is backtested on all 24 sessions. Results are appended to a CSV as they finish, so you
can stop it (Ctrl-C) and start it again: finished runs are skipped.

The theo model is fixed (default: the one trained on all 24 sessions), so every backtest is
in-sample for the model. EDA.ipynb's 4-fold check found that costs only ~0.4%. Picking the best
of hundreds of settings on the same 24 sessions overfits the *settings* far more: the analysis
reports each combination on odd and even sessions separately. Trust a winner that is good on both
halves and sits among good neighbours, not a lone spike.
"""

from __future__ import annotations

import argparse
import itertools
import random
import sys
import time
from pathlib import Path

import pandas as pd
from joblib import Parallel, delayed

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
STRATEGIES = ROOT / "strategies"
SYMBOLS = ["HPPO", "BNKY", "CRRT", "BRRY"]
SETTINGS = ["edge", "skew", "size", "improve", "take"]

GRID = {
    "edge":    {"HPPO": [1, 2, 3, 4],    "BNKY": [0.5, 1, 1.5, 2], "CRRT": [0.5, 1, 1.5, 2.5], "BRRY": [1.5, 3, 4.5, 6]},
    "skew":    {s: [0, 3, 6, 10] for s in SYMBOLS},
    "size":    {"HPPO": [20, 40, 80],    "BNKY": [50, 100, 200],   "CRRT": [50, 100, 200],    "BRRY": [15, 30, 60]},
    "improve": {"HPPO": [0, 1],          "BNKY": [0, 1],           "CRRT": [0, 1, 2],         "BRRY": [0, 1, 2, 3]},
    "take":    {"HPPO": [2, 4, 50],      "BNKY": [1.5, 3, 50],     "CRRT": [1.5, 3, 50],      "BRRY": [2, 4, 50]},
}
SEED = 7


def symbol_grid(sym: str) -> list[dict]:
    combos = itertools.product(*(GRID[k][sym] for k in SETTINGS))
    grid = [dict(zip(SETTINGS, c)) for c in combos]
    random.Random(f"{SEED}-{sym}").shuffle(grid)
    return grid


def plan() -> list[dict[str, float]]:
    """Run i -> the panel defaults it changes (e.g. {'edge_BRRY': 3, 'skew_BRRY': 6, ...})."""
    grids = {s: symbol_grid(s) for s in SYMBOLS}
    n_runs = max(len(g) for g in grids.values())
    runs = []
    for i in range(n_runs):
        cfg = {}
        for s in SYMBOLS:
            for k, v in grids[s][i % len(grids[s])].items():
                cfg[f"{k}_{s}"] = v
        runs.append(cfg)
    return runs


def backtest_one(run: int, session: str, cfg: dict, model_path: str) -> dict:
    """One backtest in a worker process: markouts per symbol, in dollars."""
    import dataclasses
    import logging

    if str(STRATEGIES) not in sys.path:
        sys.path.insert(0, str(STRATEGIES))
    from pqt_client.backtest_v2 import load_session
    from pqt_client.v2 import backtest_strategy
    from theo_mm import TheoMM

    logging.getLogger("pqt").setLevel(logging.WARNING)
    names = {p.name for p in TheoMM.MODEL_PARAMS}
    unknown = set(cfg) - names
    if unknown:
        raise ValueError(f"unknown parameters {sorted(unknown)}")
    params = tuple(dataclasses.replace(p, default=cfg.get(p.name, p.default)) for p in TheoMM.MODEL_PARAMS)
    cls = type("Variant", (TheoMM,), {"MODEL_PARAMS": params, "MODEL_PATH": Path(model_path)})
    strat = cls()
    backtest_strategy(load_session(str(DATA), session), strat)
    row = {"run": run, "session": session, **cfg}
    row.update({f"mk_{s}": v / 100 for s, v in strat.markouts().items()})
    return row


def run_grid(model_path: Path, out: Path, n_runs: int | None, jobs: int, chunk: int) -> None:
    runs = plan()
    if n_runs is not None:
        runs = runs[:n_runs]
    sessions = sorted(p.name.split(".")[0] for p in DATA.glob("s*.top.csv"))
    done = set(pd.read_csv(out)["run"].unique()) if out.exists() else set()
    todo = [i for i in range(len(runs)) if i not in done]
    print(f"{len(runs)} runs x {len(sessions)} sessions; {len(done)} runs already in {out.name}, {len(todo)} to go")
    t0 = time.time()
    with Parallel(n_jobs=jobs) as parallel:
        for start in range(0, len(todo), chunk):
            batch = todo[start : start + chunk]
            rows = parallel(
                delayed(backtest_one)(i, s, runs[i], str(model_path)) for i in batch for s in sessions
            )
            pd.DataFrame(rows).to_csv(out, mode="a", header=not out.exists(), index=False)
            finished = start + len(batch)
            rate = (time.time() - t0) / finished
            print(f"  {finished}/{len(todo)} runs, {rate:.1f} s/run, ~{rate * (len(todo) - finished) / 60:.0f} min left",
                  flush=True)


def analyze(out: Path, top: int = 10) -> None:
    df = pd.read_csv(out)
    df["half"] = df["session"].str[1:].astype(int).map(lambda n: "odd" if n % 2 else "even")
    n_sessions = df["session"].nunique()
    print(f"{df['run'].nunique()} runs x {n_sessions} sessions in {out.name}\n")
    best = {}
    for s in SYMBOLS:
        keys = [f"{k}_{s}" for k in SETTINGS]
        mk = f"mk_{s}"
        # Average over repeated samples of the same combination, per session, then sum over sessions.
        per_session = df.groupby(keys + ["session", "half"])[mk].mean().reset_index()
        total = per_session.groupby(keys)[mk].sum().rename("total")
        halves = per_session.groupby(keys + ["half"])[mk].sum().unstack("half")
        worst = per_session.groupby(keys)[mk].min().rename("worst_session")
        samples = df.groupby(keys)["run"].nunique().rename("samples")
        table = pd.concat([total, halves, worst, samples], axis=1).sort_values("total", ascending=False)
        table["rank_odd"] = table["odd"].rank(ascending=False).astype(int)
        table["rank_even"] = table["even"].rank(ascending=False).astype(int)
        print(f"== {s}: top {top} of {len(table)} combinations (dollars over {n_sessions} sessions) ==")
        print(table.head(top).round(0).to_string())
        print()
        # How much each setting matters: the best total for each value, holding nothing else fixed.
        for k in keys:
            print(f"   {k:12s} best total by value: "
                  + "  ".join(f"{v}: ${table.groupby(level=k)['total'].max().loc[v]:,.0f}"
                              for v in sorted(table.index.get_level_values(k).unique())))
        print()
        best[s] = dict(zip(keys, table.index[0]))
    print("best per symbol (by total), as DEFAULTS entries for theo_mm.py:")
    for s in SYMBOLS:
        d = {k.removesuffix(f"_{s}"): (v.item() if hasattr(v, "item") else v) for k, v in best[s].items()}
        print(f'    "{s}": {d},')


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", type=Path, default=STRATEGIES / "models" / "theo_all24.json")
    ap.add_argument("--out", type=Path, default=ROOT / "research" / "grid_results.csv")
    ap.add_argument("--runs", type=int, default=None, help="only the first N runs (a quick test)")
    ap.add_argument("--jobs", type=int, default=-1, help="worker processes (-1: all cores)")
    ap.add_argument("--chunk", type=int, default=10, help="runs per save to the CSV")
    ap.add_argument("--analyze", action="store_true", help="only analyse the results file")
    args = ap.parse_args()
    if not args.analyze:
        if not args.model.exists():
            sys.exit(f"{args.model} not found: run cell CV-1 in EDA.ipynb to export it")
        run_grid(args.model.resolve(), args.out, args.runs, args.jobs, args.chunk)
    analyze(args.out)


if __name__ == "__main__":
    main()
