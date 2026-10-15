"""Regenerate the synthetic pipeline test data (no real study data; identifiers are invented and planted on purpose).

    tools/msdeposit-tools/.venv/bin/python tests/data/make_test_data.py
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "tools" / "msdeposit-tools" / "tests"))
from synthetic import PLANTED, write_example_inputs  # noqa: E402

for mode in ("standard", "sensitive"):
    root = HERE / mode
    sheets = write_example_inputs(root, mode=mode, srm=True)
    if mode == "sensitive":
        # user denylist: the planted identifiers the lab would name (the pipeline adds the harvested candidates)
        (root / "denylist.txt").write_text("\n".join(PLANTED.values()) + "\n")
    print(mode, sorted(p.name for p in sheets.values()))
