"""judge-kit: prove an LLM judge before you trust it.

The steps:
1. A blind consensus key from isolated graders (consensus.py).
2. A seeded, stratified split into a tune set and a sealed held-out set (splits.py).
3. Freeze the judge before the held-out set is opened, open it once, and replace a spent set (vault.py).
4. Agreement with the key, with confidence intervals (agreement.py).

A consensus key is not ground truth. It is the best label you have, and the kit reports how much of
the data it covers and what it dropped.
"""

from judge_kit.consensus import Key, build_key
from judge_kit.splits import Split, split
from judge_kit.vault import Vault, VaultError
from judge_kit.agreement import Report, agreement, wilson

__all__ = ["Key", "build_key", "Split", "split", "Vault", "VaultError", "Report", "agreement", "wilson"]
__version__ = "0.1.0"
