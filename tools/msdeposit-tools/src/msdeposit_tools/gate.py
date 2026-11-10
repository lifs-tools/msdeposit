"""gate: combine the check results of one study into `gate.json` (CONTRACTS.md §9).

Reads every `check_*.json` (`msdeposit/check/1`; `*.private.json` are ignored) of the study from the given files and
directories. Per-run checks (`scope: run`, e.g. C2, C4) are aggregated to the worst status (fail > skipped > warn >
pass); several study-scope files of one check likewise.

The study is `blocked` when
  - a blocking check failed (any run of a per-run check),
  - an expected check (--expected, default C1-C9) is missing,
  - in sensitive mode, any of S1-S5 and S7 is missing, or any S check is skipped or failed (S6 runs after
    publishing and is gated by the pipeline itself; when an S6 result is present it counts like the others),
  - no check result was found at all.
  - a blocking check was skipped (e.g. validator not available): fail closed, a blocking check that did not run
    cannot pass the study; such checks are also listed in `skipped_blocking`.
The gate never passes silently: every blocking reason is listed in `reasons`.

`gate.json`: schema, study, mode, status (passed|blocked), checks {id: status}, blocking_failures, missing_checks,
skipped_blocking, runs {id: {status: n}} for per-run checks, reasons, notes, summary (public; no paths).
"""

import re
from collections import Counter, defaultdict
from pathlib import Path

from .io import InputError, read_json, write_json

RANK = {"pass": 0, "warn": 1, "skipped": 2, "fail": 3}
DEFAULT_EXPECTED = ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9")
SENSITIVE_REQUIRED = ("S1", "S2", "S3", "S4", "S5", "S7")
CHECK_ID = re.compile(r"^[CS]\d+$")


def add_arguments(p):
    p.add_argument("--study", required=True)
    p.add_argument("--checks", required=True, nargs="+", type=Path, help="check_*.json files or directories")
    p.add_argument("--expected", default=",".join(DEFAULT_EXPECTED), help="comma-separated check ids")
    p.add_argument("--mode", choices=("standard", "sensitive"), default="standard")
    p.add_argument("--out", type=Path, default=Path("gate.json"))


def check_files(paths: list[Path]) -> list[Path]:
    out = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            out += sorted(p.rglob("check_*.json"))
        elif p.is_file():
            out.append(p)
        else:
            raise InputError(f"check result not found: {p.name}")
    return [f for f in dict.fromkeys(out) if f.name.startswith("check_") and not f.name.endswith(".private.json")]


def load_results(paths: list[Path], study: str) -> list[dict]:
    results = []
    for f in check_files(paths):
        r = read_json(f, "msdeposit/check/1")
        if r.get("study") != study:
            continue
        if not CHECK_ID.match(str(r.get("check", ""))) or r.get("status") not in RANK:
            raise InputError(f"{f.name}: invalid check id or status")
        results.append(r)
    return results


def gate(study: str, results: list[dict], expected: list[str], mode: str) -> dict:
    by_check = defaultdict(list)
    for r in results:
        by_check[r["check"]].append(r)
    statuses, blocking_failures, skipped_blocking, runs = {}, [], [], {}
    for check, rs in sorted(by_check.items(), key=lambda kv: (kv[0][0], int(kv[0][1:]))):
        statuses[check] = max((r["status"] for r in rs), key=RANK.get)
        if any(r.get("scope") == "run" for r in rs):
            runs[check] = dict(sorted(Counter(r["status"] for r in rs).items()))
        if any(r["status"] == "fail" and r.get("blocking", True) for r in rs):
            blocking_failures.append(check)
        if any(r["status"] == "skipped" and r.get("blocking", True) for r in rs):
            skipped_blocking.append(check)
    required = list(expected) + (list(SENSITIVE_REQUIRED) if mode == "sensitive" else [])
    missing = [c for c in dict.fromkeys(required) if c not in statuses]
    reasons = [f"blocking check failed: {c}" for c in blocking_failures]
    reasons += [f"expected check missing: {c}" for c in missing]
    reasons += [f"blocking check skipped: {c}" for c in skipped_blocking if c not in blocking_failures]
    if mode == "sensitive":
        unsafe = [c for c in statuses if c.startswith("S") and statuses[c] in ("skipped", "fail")
                  and c not in blocking_failures]
        reasons += [f"sensitive check {statuses[c]}: {c}" for c in unsafe]
    if not results:
        reasons.append("no check results found")
    status = "blocked" if reasons else "passed"
    notes = []
    summary = f"{study}: {status}" + "".join(f"; {x}" for x in reasons + notes)
    return dict(schema="msdeposit/gate/1", study=study, mode=mode, status=status, checks=statuses,
                blocking_failures=blocking_failures, missing_checks=missing, skipped_blocking=skipped_blocking,
                runs=runs, reasons=reasons, notes=notes, summary=summary)


def run(args) -> int:
    expected = [c.strip() for c in args.expected.split(",") if c.strip()]
    bad = [c for c in expected if not CHECK_ID.match(c)]
    if bad:
        raise InputError(f"invalid check ids: {', '.join(bad)}")
    result = gate(args.study, load_results(args.checks, args.study), expected, args.mode)
    write_json(args.out, result)
    print(result["summary"])
    return 0
