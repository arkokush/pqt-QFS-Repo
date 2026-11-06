#!/usr/bin/env bash
# Start theo_mm.py on the sandbox or in the competition, with the checks done for you.
#
#   export PQT_API_KEY=pqt_...                 # your team's algorithm key (never a pqm_ login code)
#   ./run.sh sandbox                           # https://sandbox.princeton-quant.com, --mode test
#   ./run.sh live https://competition-url      # the competition, --mode live
#
# It checks the key and the model file, warns if Ollama is not running (trading works without it),
# runs `pqt check`, then starts the strategy with --cancel-on-disconnect under caffeinate (the Mac
# stays awake). Type `help` + Enter in this window for the strategy's commands. Ctrl-C stops it.
set -euo pipefail
cd "$(dirname "$0")"

case "${1:-}" in
  sandbox) SERVER="https://sandbox.princeton-quant.com"; MODE="test" ;;
  live)    SERVER="${2:-}"; MODE="live"
           if [[ -z "$SERVER" ]]; then echo "usage: ./run.sh live https://competition-url"; exit 1; fi ;;
  *)       echo "usage: ./run.sh sandbox | ./run.sh live https://competition-url"; exit 1 ;;
esac

if [[ -z "${PQT_API_KEY:-}" ]]; then
  echo "PQT_API_KEY is not set: export PQT_API_KEY=pqt_...  (the algorithm key, not a pqm_ login code)"; exit 1
fi
if [[ "$PQT_API_KEY" == pqm_* ]]; then
  echo "PQT_API_KEY is a pqm_ login code: programs need the team's pqt_ algorithm key"; exit 1
fi
if [[ ! -f strategies/theo_model.json ]]; then
  echo "strategies/theo_model.json is missing: export it from EDA.ipynb first"; exit 1
fi
if ! curl -s --max-time 2 http://localhost:11434 >/dev/null; then
  echo "WARNING: Ollama is not running: the LLM news reader is off (open the Ollama app, or run 'ollama serve')."
  echo "         Trading works without it. Press Enter to go on, Ctrl-C to stop."
  read -r
fi
if pgrep -xq pycharm || pgrep -fq "PyCharm.app"; then
  echo "NOTE: PyCharm is running; it slowed the LLM more than 10x in testing. Quit it if you can."
fi

source .venv/bin/activate
pqt check --server "$SERVER"
echo
echo "starting theo_mm.py on $SERVER ($MODE). Type 'help' + Enter for commands."
exec caffeinate -i pqt run strategies/theo_mm.py --mode "$MODE" --server "$SERVER" --cancel-on-disconnect
