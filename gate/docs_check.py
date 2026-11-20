"""Docs check: every number the README states about the demo must appear in the demo's actual output."""
import re
import sys

demo = open(sys.argv[1]).read()
claims = [
    ("key covers 76%", r"\(76%\)"),
    ("hard cases 33% of the data", r"hard cases are 33% of the data"),
    ("22% of the key", r"but 22% of the key"),
    ("three unanimous labels wrong", r": 3 unanimous labels are WRONG"),
    ("baseline kappa 0.85", r"baseline: kappa 0\.85\d"),
    ("round 3 kappa 0.99", r"round 3: .*kappa 0\.98[5-9]|round 3: .*kappa 0\.99"),
    ("fourth round refused", r"a 4th round is refused"),
    ("held-out kappa 0.966 [0.915, 1.000]", r"kappa:\s+0\.966 \[0\.915, 1\.000\]"),
    ("2 false alarms of 69", r"\(2/69\)"),
    ("no errors vs planted truth", r"false alarms 0, misses 0"),
    ("second open refused", r"a second open is refused"),
    ("retune refused", r"retuning is refused"),
    ("generation 2 sealed", r"generation 2 sealed"),
    ("400 items", r"key: 400 synthetic tool calls"),
    ("verify consistent", r"verify: consistent"),
]
bad = 0
for what, pat in claims:
    ok = re.search(pat, demo) is not None
    print(("ok   " if ok else "MISS ") + what)
    bad += not ok
print(f"{len(claims) - bad}/{len(claims)} README claims found in the demo output")
sys.exit(1 if bad else 0)
