#!/bin/bash
# disco_fan.sh command for the judge-kit gate: one gate row per shard. Runs inside a deny-by-default Discobox box as user `job`.
# Shards: 0 suite+chaos(200 trials, junit) | 1 stateful (3000 examples) | 2 red_check | 3 mutmut | 4 load to the knee | 5 demo + docs numbers
set -u
export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}" PYTHONDONTWRITEBYTECODE=1
python -V > "$OUT/python.txt" 2>&1; nproc >> "$OUT/python.txt"; free -m >> "$OUT/python.txt" 2>&1
case "$SHARD_INDEX" in
  0) JK_CHAOS_TRIALS=200 python -m pytest -q -p no:cacheprovider --junitxml="$OUT/junit.xml" tests > "$OUT/suite.txt" 2>&1 ;;
  1) JK_SM_EXAMPLES=3000 python -m pytest -q -p no:cacheprovider --junitxml="$OUT/junit.xml" tests/test_stateful.py > "$OUT/stateful.txt" 2>&1 ;;
  2) python scripts/red_check.py > "$OUT/red_check.txt" 2>&1 ;;
  3) export JK_CHAOS_TRIALS=2; mutmut run --max-children "$(nproc)" > "$OUT/mutmut_run.txt" 2>&1; r=$?
     mutmut results > "$OUT/mutmut_results.txt" 2>&1
     for m in $(mutmut results 2>/dev/null | awk -F: '/survived/{print $1}'); do echo "=== $m"; mutmut show "$m"; done > "$OUT/survivors.txt" 2>&1
     tr '\r' '\n' < "$OUT/mutmut_run.txt" | grep -E '🎉|🫥|⏰|🤔|🙁|🔇' | tail -1 > "$OUT/mutmut_tally.txt"; exit $r ;;
  4) python scripts/load.py --max 10000000 --budget-s 2400 > "$OUT/load.jsonl" 2>&1 ;;
  5) python -m judge_kit.cli demo --dir /tmp/jkdemo > "$OUT/demo.txt" 2>&1 && python gate/docs_check.py "$OUT/demo.txt" > "$OUT/docs_check.txt" 2>&1 ;;
  *) exit 0 ;;
esac
