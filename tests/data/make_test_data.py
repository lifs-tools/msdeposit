"""Regenerate the synthetic pipeline test data (no real study data; identifiers are invented and planted on purpose).

    tools/msdeposit-tools/.venv/bin/python tests/data/make_test_data.py
"""

import csv
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "tools" / "msdeposit-tools" / "tests"))
from synthetic import PLANTED, write_example_inputs  # noqa: E402

def add_second_study(root: Path, sheets: dict) -> None:
    """Study S2 (same mzML files, other study key): multi-study runs must keep every study (per-study channels)."""
    for key in ("input", "assays", "samples", "features", "values", "study_variables", "study_variable_members"):
        path = sheets[key]
        rows = list(csv.DictReader(open(path)))
        extra = [dict(r, study="S2") for r in rows if r.get("study") == "S1"]
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
            w.writeheader()
            w.writerows(rows + extra)
    studies = sheets["studies"]
    studies.write_text(studies.read_text() + "  S2:\n    title: Synthetic lipidomics test study, second study\n"
                                              "    description: The same runs as S1 under another study key.\n")


for mode in ("standard", "sensitive"):
    root = HERE / mode
    sheets = write_example_inputs(root, mode=mode, srm=True)
    if mode == "standard":
        add_second_study(root, sheets)
    if mode == "sensitive":
        # user denylist: the planted identifiers the lab would name (the pipeline adds the harvested candidates)
        (root / "denylist.txt").write_text("\n".join(PLANTED.values()) + "\n")
    print(mode, sorted(p.name for p in sheets.values()))
