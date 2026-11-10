"""check: checks C6-C9 on an assembled upload set (design §8.1; results as in CONTRACTS.md §9).

  C6  MetaboLights validation (`validate_mtbls.validate`, pinned bundle + OPA, offline)
  C7  mzTab-M validation of every mzTab file, with the version read from its `mzTab-version` line; every version of
      --mztab-versions needs a file (`validate_mztab.validate(path, version)`)
  C8  mzQC validation of every mzQC file (`validate_mzqc.validate`); at least one file is expected
  C9  completeness: `[[TODO-…]]` placeholders in all text files (not mzML or raw), empty cells in required ISA
      columns (sample and assay templates of the pinned bundle, `data/mtbls_templates.json`, else a built-in minimum),
      missing MAF m/z (all assays) and retention times (chromatographic assays, needs --plan)

Status: validator findings with errors → `fail`, warnings only → `warn`; a validator that is not available →
`skipped` (never `pass`); C9 findings → `fail` with --fail-on-todo-placeholders, else `warn`. `blocking` follows the
parameters. Public check files carry counts and a summary without paths; messages go to `check_<ID>.private.json`.
"""

import csv
import json
import re
from functools import lru_cache
from pathlib import Path

from .assemble import kind_of, walk
from .io import InputError, as_bool, read_json, write_check

CHECKS = ("C6", "C7", "C8", "C9")
MZTAB_VERSIONS = ("2.0.0-M", "2.1.0-M")
TODO = re.compile(r"\[\[TODO-[^\]\n]*\]\]")
MZTAB_VERSION_LINE = re.compile(r"^MTD\tmzTab-version\t([^\t\r\n]+)", re.M)
CHROMATOGRAPHIC = {"LC-MS", "GC-MS", "GCxGC-MS", "CE-MS", "SFC-MS"}
TEMPLATE_FILES = ("mtbls_templates.json", "mtbls_isa_column_structures.json")
# used only when no template file is shipped: required columns of template version 1.0 (without Protocol REF)
BUILTIN_SAMPLE = ["Source Name", "Characteristics[Organism]", "Characteristics[Organism part]", "Sample Name"]
BUILTIN_ASSAY = ["Sample Name", "Parameter Value[Scan polarity]", "Parameter Value[Scan m/z range]",
                 "Parameter Value[Instrument]", "Metabolite Assignment File"]
BUILTIN_CHROMATOGRAPHY = ["Parameter Value[Chromatography Instrument]", "Parameter Value[Column model]",
                          "Parameter Value[Column type]"]
SKIP_TEXT_KINDS = {"mzml", "raw"}


def add_arguments(p):
    p.add_argument("--upload-set", required=True, type=Path)
    p.add_argument("--study", required=True)
    p.add_argument("--plan", type=Path, help="plan.json (assay techniques for C9)")
    p.add_argument("--checks", default=",".join(CHECKS), help="comma-separated subset of C6,C7,C8,C9")
    p.add_argument("--mztab-versions", default="2.0.0-M", help="comma-separated: 2.0.0-M, 2.1.0-M; 'none' for none")
    p.add_argument("--fail-on-validation-errors", default="false")
    p.add_argument("--fail-on-todo-placeholders", default="false")
    p.add_argument("--outdir", type=Path, default=Path("."))


def parse_list(value: str, allowed: tuple, what: str) -> list[str]:
    items = [x.strip() for x in str(value or "").split(",") if x.strip() and x.strip().lower() != "none"]
    bad = [x for x in items if x not in allowed]
    if bad:
        raise InputError(f"unknown {what}: {', '.join(bad)} (allowed: {', '.join(allowed)})")
    return list(dict.fromkeys(items))


# --- validators (C6-C8) -------------------------------------------------------------------------------------------

def _validator(name: str):
    """The validator module, or None when it cannot be imported (the check is then skipped)."""
    import importlib
    try:
        return importlib.import_module(f"msdeposit_tools.{name}")
    except ImportError:
        return None


UNAVAILABLE = {"available": False, "tool": "", "errors": 0, "warnings": 0, "messages": []}


def combine(results: list[tuple[str, dict]], missing: list[str] | None = None) -> tuple[str, dict, list, str]:
    """(status, counts, details, summary) from validator results [(label, result)] and missing expected files."""
    missing = missing or []
    available = [(label, r) for label, r in results if r.get("available")]
    unavailable = [label for label, r in results if not r.get("available")]
    errors = sum(int(r.get("errors", 0)) for _, r in available)
    warnings = sum(int(r.get("warnings", 0)) for _, r in available)
    details = [{**m, "checked": label, "validator": r.get("tool", "")} for label, r in available
               for m in r.get("messages", [])]
    details += [dict(checked=label, level="info", rule="", message="validator not available") for label in unavailable]
    details += [dict(checked=label, level="error", rule="missing", message="expected file not found")
                for label in missing]
    if errors or missing:
        status = "fail"
    elif unavailable or not results:
        status = "skipped"
    else:
        status = "warn" if warnings else "pass"
    counts = dict(files=len(results), validated=len(available), unavailable=len(unavailable), missing=len(missing),
                  errors=errors, warnings=warnings)
    tools = sorted({r.get("tool", "") for _, r in available} - {""})
    summary = (f"{len(available)}/{len(results)} files validated; {errors} errors, {warnings} warnings"
               + (f"; {len(missing)} expected files missing" if missing else "")
               + (f"; validator not available for {len(unavailable)} files" if unavailable else "")
               + (f" ({'; '.join(tools)})" if tools else ""))
    return status, counts, details, summary


def run_validator(module: str, *call) -> dict:
    mod = _validator(module)
    if mod is None:
        return dict(UNAVAILABLE)
    return mod.validate(*call)


def check_c6(upload: Path) -> tuple:
    return combine([("upload set", run_validator("validate_mtbls", upload))])


def mztab_version(path: Path) -> str:
    with open(path, encoding="utf-8", errors="replace") as fh:
        head = fh.read(1 << 16)
    m = MZTAB_VERSION_LINE.search(head)
    if m:
        return m.group(1).strip()
    return next((v for v in MZTAB_VERSIONS if v in path.name), "")


def check_c7(upload: Path, versions: list[str]) -> tuple:
    files = [upload / f for f in walk(upload)[0] if kind_of(f) == "mztab"]
    found = {f: mztab_version(f) for f in files}
    results = []
    for f, v in found.items():
        rel = f.relative_to(upload).as_posix()
        if v in MZTAB_VERSIONS:
            results.append((f"{rel} ({v})", run_validator("validate_mztab", f, v)))
        else:
            results.append((rel, dict(available=True, tool="msdeposit-tools", errors=1, warnings=0, messages=[
                dict(level="error", rule="mztab_version", message=f"unsupported mzTab-version {v or 'none'}")])))
    missing = [f"mzTab-M {v}" for v in versions if v not in found.values()]
    if not versions and not files:
        return "skipped", dict(files=0), [], "no mzTab-M version selected"
    return combine(results, missing)


def check_c8(upload: Path) -> tuple:
    files = [upload / f for f in walk(upload)[0] if kind_of(f) == "mzqc"]
    results = [(f.relative_to(upload).as_posix(), run_validator("validate_mzqc", f)) for f in files]
    return combine(results, [] if files else ["mzQC file"])


# --- completeness (C9) --------------------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def templates() -> dict | None:
    """Template column structures {"assay": {technique: [[header, structure, required, default]]}, "sample": {...}}."""
    data = Path(__file__).parent / "data"
    for name in TEMPLATE_FILES:
        if (data / name).is_file():
            t = json.loads((data / name).read_text(encoding="utf-8"))
            if isinstance(t.get("assay"), dict) and isinstance(t.get("sample"), dict):
                return t
    return None


def required_columns(kind: str, technique: str = "") -> list[str]:
    t = templates()
    if t is None:
        if kind == "sample":
            return list(BUILTIN_SAMPLE)
        return BUILTIN_ASSAY + (BUILTIN_CHROMATOGRAPHY if technique in CHROMATOGRAPHIC else [])
    if kind == "sample":
        cols = t["sample"].get("minimum") or next(iter(t["sample"].values()), [])
    else:
        cols = t["assay"].get(technique) or t["assay"].get("MS") or next(iter(t["assay"].values()), [])
    return list(dict.fromkeys(c[0] for c in cols if c[2] and c[0] != "Protocol REF"))


def read_tsv_rows(path: Path) -> list[list[str]]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.reader(fh, delimiter="\t"))


def empty_required(path: Path, required: list[str]) -> tuple[int, list[str], list[dict]]:
    """(empty cells, missing columns, details) for the required columns of an ISA table."""
    rows = read_tsv_rows(path)
    if not rows:
        return 0, list(required), []
    header = [h.strip() for h in rows[0]]
    empty, details = 0, []
    missing = [c for c in required if c not in header]
    for col in required:
        if col not in header:
            continue
        i = header.index(col)
        n = sum(1 for r in rows[1:] if any(x.strip() for x in r) and (i >= len(r) or not r[i].strip()))
        if n:
            empty += n
            details.append(dict(file=path.name, item="empty_required", column=col, rows=n))
    details += [dict(file=path.name, item="missing_required_column", column=c) for c in missing]
    return empty, missing, details


def maf_gaps(path: Path, need_rt: bool) -> tuple[int, int, list[dict]]:
    rows = read_tsv_rows(path)
    if not rows:
        return 0, 0, []
    header = [h.strip() for h in rows[0]]
    body = [r for r in rows[1:] if any(x.strip() for x in r)]

    def empty(col):
        if col not in header:
            return len(body)
        i = header.index(col)
        return sum(1 for r in body if i >= len(r) or not r[i].strip())

    mz, rt = empty("mass_to_charge"), (empty("retention_time") if need_rt else 0)
    details = [dict(file=path.name, item=f"maf_missing_{k}", rows=n) for k, n in (("mz", mz), ("rt", rt)) if n]
    return mz, rt, details


def check_c9(upload: Path, plan: dict | None, blocking: bool) -> tuple:
    files = walk(upload)[0]
    todo, todo_files, details = 0, 0, []
    for f in files:
        if kind_of(f) in SKIP_TEXT_KINDS:
            continue
        text = (upload / f).read_text(encoding="utf-8", errors="replace")
        hits = TODO.findall(text)
        if hits:
            todo += len(hits)
            todo_files += 1
            details.append(dict(file=f, item="todo", count=len(hits), placeholders=sorted(set(hits))))
    techniques = {a["file"]: a.get("technique", "") for a in (plan or {}).get("assays", [])}
    maf_tech = {a["maf"]: a.get("technique", "") for a in (plan or {}).get("assays", [])}
    empty = missing_cols = mz = rt = 0
    for f in files:
        if "/" in f:
            continue
        if f.startswith("s_") and f.endswith(".txt"):
            n, miss, d = empty_required(upload / f, required_columns("sample"))
        elif f.startswith("a_") and f.endswith(".txt"):
            n, miss, d = empty_required(upload / f, required_columns("assay", techniques.get(f, "")))
        elif f.startswith("m_") and f.endswith(".tsv"):
            a, b, d = maf_gaps(upload / f, maf_tech.get(f, "") in CHROMATOGRAPHIC)
            mz, rt = mz + a, rt + b
            details += d
            continue
        else:
            continue
        empty, missing_cols = empty + n, missing_cols + len(miss)
        details += d
    counts = dict(todo_placeholders=todo, files_with_todo=todo_files, empty_required_cells=empty,
                  missing_required_columns=missing_cols, maf_missing_mz=mz, maf_missing_rt=rt)
    findings = sum(counts.values()) - todo_files
    status = ("fail" if blocking else "warn") if findings else "pass"
    summary = (f"{todo} TODO placeholders in {todo_files} files; {empty} empty required ISA cells, "
               f"{missing_cols} missing required columns; MAF rows without m/z: {mz}, without RT: {rt}"
               + ("" if templates() else " (built-in minimal ISA column list)"))
    return status, counts, details, summary


def run(args) -> int:
    upload = args.upload_set
    if not upload.is_dir():
        raise InputError("upload set is not a directory")
    checks = parse_list(args.checks, CHECKS, "check")
    versions = parse_list(args.mztab_versions, MZTAB_VERSIONS, "mzTab-M version")
    fail_validation = as_bool(args.fail_on_validation_errors)
    fail_todo = as_bool(args.fail_on_todo_placeholders)
    plan = read_json(args.plan, "msdeposit/plan/1") if args.plan else None
    if plan is not None and plan["study"] != args.study:
        raise InputError(f"plan is for study {plan['study']}, not {args.study}")
    for check in checks:
        if check == "C6":
            status, counts, details, summary = check_c6(upload)
        elif check == "C7":
            status, counts, details, summary = check_c7(upload, versions)
        elif check == "C8":
            status, counts, details, summary = check_c8(upload)
        else:
            status, counts, details, summary = check_c9(upload, plan, fail_todo)
        blocking = fail_todo if check == "C9" else fail_validation
        if status == "skipped" and blocking:
            summary = f"NOT RUN although blocking: {summary}"
        write_check(args.outdir, args.study, check, status, blocking=blocking, counts=counts, summary=summary,
                    details=details)
        print(f"{args.study}: {check} {status}{' (blocking)' if blocking else ''}")
    return 0
