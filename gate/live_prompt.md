You are running one acceptance check in this repository. Do not change any source file.

1. Run exactly this, from the repository root, and wait for it to finish (it makes 60 short `claude -p` calls and can take 10 to 20 minutes):

   PYTHONPATH=src python3 scripts/live_acceptance.py live_acceptance.json 2>&1 | tee live_acceptance.log

2. Commit the two result files, even if the script printed FAIL:

   git add -f live_acceptance.json live_acceptance.log && git commit -q -m "live acceptance result"

3. Then run: touch /tmp/job/DONE

Do not retry the script, do not edit it, and do not interpret or summarise the results. If step 1 cannot start (for example python3 or claude is missing), write the error into live_acceptance.log, commit it as in step 2, and finish with step 3.
