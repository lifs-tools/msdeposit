"""assemble (check C5): build the unpublished upload set of one study from the plan and the described files.

Layout (CONTRACTS.md §7) under `<outdir>/<study>/`:
  i_Investigation.txt  s_*.txt  a_*.txt  m_*_maf.tsv
  FILES/DERIVED_FILES/*.mzML  FILES/RAW_FILES/*  FILES/RESULTS/*.mzTab  FILES/QC/*.mzQC  FILES/QC/deposit_report.html
  upload_manifest.tsv          path, bytes, sha256, kind (isa|maf|mzml|raw|mztab|mzqc|report)
Files are hard-linked when possible, copied otherwise, never symlinked. Inputs are matched to the plan by file name;
inputs the plan does not know are not placed and fail C5.

C5 (`check_upload_set`, also usable on any upload set):
  - every file referenced by the ISA files (investigation -> s_/a_ files; assay columns `* Data File` and
    `Metabolite Assignment File`) and by the plan is present, and the plan's mzML and main raw files are the ones the
    assay tables reference;
  - no unreferenced files except the manifest, mzTab, mzQC and the deposit report;
  - the manifest lists every file with bytes and SHA-256 (equal to the raw-package records when those are given);
  - MetaboLights names (`.-_A-Za-z0-9` per path component, sub-folders only below FILES/); no zero-byte files;
    no symlinks; no inputs the plan does not know.
Writes `<outdir>/check_C5.json` (+ `.private.json` with the paths), outside the upload set.
"""

import csv
import os
import re
from pathlib import Path

from .fixity import hash_many
from .io import InputError, read_json, read_table, safe_name, write_check, write_table
from .raw_package import link_or_copy

MANIFEST = "upload_manifest.tsv"
MANIFEST_FIELDS = ["path", "bytes", "sha256", "kind"]
REPORT = "FILES/QC/deposit_report.html"
NAME_OK = re.compile(r"^[A-Za-z0-9._-]+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
DATA_COLUMN = re.compile(r"(Data File|Assignment File)$")
ISA_NAME = re.compile(r"^[isa]_.*\.txt$")
PROBLEMS = ("missing", "unreferenced", "not_in_isa", "bad_names", "zero_bytes", "symlinks", "manifest_problems",
            "sha256_mismatches", "unplanned_inputs", "isa_problems")


def add_arguments(p):
    p.add_argument("--plan", required=True, type=Path)
    p.add_argument("--isa", type=Path, help="directory with i_/s_/a_ files")
    p.add_argument("--maf", type=Path, help="directory with m_*_maf.tsv files")
    p.add_argument("--mztab", type=Path, help="directory with *.mzTab files")
    p.add_argument("--mzqc", type=Path, help="directory with *.mzQC files")
    p.add_argument("--mzml", nargs="*", type=Path, default=[], help="finalised mzML files or directories")
    p.add_argument("--raw", nargs="*", type=Path, default=[],
                   help="packaged raw files or directories (standard mode), incl. *.raw_package.json")
    p.add_argument("--report", type=Path, help="public deposit report (deposited as FILES/QC/deposit_report.html)")
    p.add_argument("--jobs", type=int, default=4, help="parallel hashing threads")
    p.add_argument("--outdir", type=Path, default=Path("."))


def expand(paths) -> list[Path]:
    out = []
    for p in paths or []:
        p = Path(p)
        if p.is_dir():
            out += sorted(x for x in p.rglob("*") if x.is_file())
        elif p.exists():
            out.append(p)
        else:
            raise InputError(f"input not found: {p.name}")
    return out


def kind_of(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    if rel == MANIFEST:
        return "manifest"
    if rel == REPORT:
        return "report"
    if "/" not in rel and ISA_NAME.match(name):
        return "isa"
    if "/" not in rel and name.startswith("m_"):
        return "maf"
    if rel.startswith("FILES/DERIVED_FILES/") and name.lower().endswith(".mzml"):
        return "mzml"
    if rel.startswith("FILES/RAW_FILES/"):
        return "raw"
    if rel.startswith("FILES/RESULTS/") and name.lower().endswith(".mztab"):
        return "mztab"
    if rel.startswith("FILES/QC/") and name.lower().endswith(".mzqc"):
        return "mzqc"
    return "other"


def plan_files(plan: dict, maf: bool) -> dict[str, str]:
    """Upload paths the plan requires -> kind."""
    out = {plan["investigation_file"]: "isa", plan["sample_file"]: "isa"}
    for a in plan["assays"]:
        out[a["file"]] = "isa"
        if maf:
            out[a["maf"]] = "maf"
    for r in plan["runs"]:
        out[r["derived_path"]] = "mzml"
    if plan.get("deposit_raw"):
        for x in plan.get("raw_files", []):
            for d in x.get("deposited", []):
                out[d] = "raw"
    return out


def _cells(line: str) -> list[str]:
    return next(csv.reader([line], delimiter="\t"), [])


def isa_references(root: Path) -> tuple[set[str], list[str]]:
    """Paths referenced by the ISA files of an upload set (relative to its root) and parse problems."""
    refs, problems = set(), []
    inv = root / "i_Investigation.txt"
    if not inv.is_file():
        return refs, ["no i_Investigation.txt"]
    tables = []
    for line in inv.read_text(encoding="utf-8-sig").splitlines():
        cells = _cells(line)
        if cells and cells[0].strip() in ("Study File Name", "Study Assay File Name"):
            names = [c.strip() for c in cells[1:] if c.strip()]
            refs.update(names)
            tables += [n for n in names if cells[0].strip() == "Study Assay File Name"]
    for name in tables:
        path = root / name
        if not path.is_file():
            continue
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.reader(fh, delimiter="\t"))
        if not rows:
            problems.append(f"{name}: empty assay file")
            continue
        cols = [i for i, h in enumerate(rows[0]) if DATA_COLUMN.search(h.strip())]
        for row in rows[1:]:
            refs.update(row[i].strip() for i in cols if i < len(row) and row[i].strip())
    return refs, problems


def walk(root: Path) -> tuple[list[str], list[str]]:
    """(regular files, symlinks) below root as sorted relative posix paths; links are not followed."""
    files, links = [], []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        for n in dirnames + filenames:
            p = here / n
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                links.append(rel)
            elif n in filenames:
                files.append(rel)
    return sorted(files), sorted(links)


def name_problem(rel: str) -> str | None:
    parts = rel.split("/")
    if any(not NAME_OK.match(p) or p in (".", "..") for p in parts):
        return "bad_name"
    if len(parts) > 1 and parts[0] != "FILES":
        return "bad_folder"
    return None


def write_manifest(root: Path, jobs: int = 4) -> list[dict]:
    files = [f for f in walk(root)[0] if f != MANIFEST]
    digests = hash_many([root / f for f in files], jobs)
    rows = [dict(path=f, bytes=(root / f).stat().st_size, sha256=d, kind=kind_of(f)) for f, d in zip(files, digests)]
    write_table(root / MANIFEST, rows, MANIFEST_FIELDS)
    return rows


def check_upload_set(root: Path, plan: dict, *, maf_expected: bool = False, expected_sha256: dict | None = None,
                     unplanned_inputs: list[str] | None = None) -> tuple[str, dict, list[dict]]:
    """C5 on an assembled upload set: (status, public counts, private details)."""
    root = Path(root)
    files, links = walk(root)
    present = set(files)
    refs, isa_problems = isa_references(root)
    details = [dict(problem="isa_parse", path="", message=m) for m in isa_problems]
    required = plan_files(plan, maf_expected or any(a["maf"] in refs for a in plan["assays"]))
    referenced = refs | {plan["investigation_file"]}
    missing = sorted((referenced | set(required)) - present)
    allowed = {"manifest", "report", "mztab", "mzqc"}
    unreferenced = sorted(f for f in present - referenced - set(required) if kind_of(f) not in allowed)
    # the per-run data files of the plan must also be the ones the assay tables point to
    run_files = {r["derived_path"] for r in plan["runs"]} | {r["raw_path"] for r in plan["runs"] if r.get("raw_path")}
    not_in_isa = sorted(run_files - refs) if not isa_problems else []
    names = [(f, name_problem(f)) for f in files + links]
    names = [(f, p) for f, p in names if p]
    zero = [f for f in files if (root / f).stat().st_size == 0]
    manifest_problems = []
    if MANIFEST in present:
        rows = {r["path"]: r for r in read_table(root / MANIFEST, "\t")}
        for f in sorted(present - {MANIFEST}):
            r = rows.get(f)
            if r is None:
                manifest_problems.append((f, "not in manifest"))
            elif not SHA256.match(r.get("sha256", "")) or str((root / f).stat().st_size) != r.get("bytes"):
                manifest_problems.append((f, "manifest bytes or sha256 invalid"))
        manifest_problems += [(f, "in manifest but absent") for f in sorted(set(rows) - present)]
    else:
        manifest_problems.append((MANIFEST, "manifest missing"))
    hash_mismatch = []
    if expected_sha256:
        listed = {r["path"]: r["sha256"] for r in read_table(root / MANIFEST, "\t")} if MANIFEST in present else {}
        hash_mismatch = sorted(f for f, h in expected_sha256.items() if f in listed and listed[f] != h)
    unplanned = sorted(unplanned_inputs or [])
    details += [dict(problem="missing", path=f) for f in missing]
    details += [dict(problem="unreferenced", path=f) for f in unreferenced]
    details += [dict(problem="not_in_isa", path=f) for f in not_in_isa]
    details += [dict(problem=p, path=f) for f, p in names]
    details += [dict(problem="zero_bytes", path=f) for f in zero]
    details += [dict(problem="symlink", path=f) for f in links]
    details += [dict(problem="manifest", path=f, message=m) for f, m in manifest_problems]
    details += [dict(problem="sha256_mismatch", path=f) for f in hash_mismatch]
    details += [dict(problem="unplanned_input", path=f) for f in unplanned]
    counts = dict(files=len(files), bytes=sum((root / f).stat().st_size for f in files), referenced=len(referenced),
                  missing=len(missing), unreferenced=len(unreferenced), not_in_isa=len(not_in_isa),
                  bad_names=len(names), zero_bytes=len(zero), symlinks=len(links),
                  manifest_problems=len(manifest_problems), sha256_mismatches=len(hash_mismatch),
                  unplanned_inputs=len(unplanned), isa_problems=len(isa_problems))
    problems = sum(v for k, v in counts.items() if k in PROBLEMS)
    return ("fail" if problems else "pass"), counts, details


def summary_of(counts: dict) -> str:
    parts = [f"{counts[k]} {k.replace('_', ' ')}" for k in PROBLEMS if counts[k]]
    return f"{counts['files']} files, {counts['bytes']} bytes; " + (", ".join(parts) if parts else "complete")


def gather(args, plan: dict) -> tuple[dict[str, Path], list[str], dict[str, str], bool]:
    """Planned placements {upload path: source}, unplanned input names, raw-package hashes, maf expected."""
    placements, unplanned, hashes = {}, [], {}

    def put(rel, src):
        if rel in placements:
            unplanned.append(f"{src.name} (given twice)")
        else:
            placements[rel] = src

    isa_names = {plan["investigation_file"], plan["sample_file"]} | {a["file"] for a in plan["assays"]}
    for f in expand([args.isa] if args.isa else []):
        if f.name in isa_names:
            put(f.name, f)
        elif ISA_NAME.match(f.name):
            unplanned.append(f.name)
    maf_names = {a["maf"] for a in plan["assays"]}
    for f in expand([args.maf] if args.maf else []):
        if f.name in maf_names:
            put(f.name, f)
        elif f.name.startswith("m_") and f.name.endswith(".tsv"):
            unplanned.append(f.name)
    for f in expand([args.mztab] if args.mztab else []):
        if f.name.lower().endswith(".mztab"):
            put(f"FILES/RESULTS/{safe_name(f.name)}", f)
    for f in expand([args.mzqc] if args.mzqc else []):
        if f.name.lower().endswith(".mzqc"):
            put(f"FILES/QC/{safe_name(f.name)}", f)
    mzml = {Path(r["derived_path"]).name: r["derived_path"] for r in plan["runs"]}
    for f in expand(args.mzml):
        if f.name in mzml:
            put(mzml[f.name], f)
        else:
            unplanned.append(f.name)
    raw = {Path(d).name: d for x in plan.get("raw_files", []) for d in x.get("deposited", [])}
    for f in expand(args.raw):
        if f.name.endswith(".raw_package.json"):
            rec = read_json(f, "msdeposit/raw_package/1")
            hashes.update({x["deposited"]: x["sha256"] for x in rec["files"]})
        elif f.name in raw and plan.get("deposit_raw"):
            put(raw[f.name], f)
        else:
            unplanned.append(f.name)
    if args.report:
        put(REPORT, args.report)
    return placements, unplanned, hashes, args.maf is not None


def run(args) -> int:
    plan = read_json(args.plan, "msdeposit/plan/1")
    root = args.outdir / safe_name(plan["study"])
    if root.exists() and any(root.iterdir()):
        raise InputError(f"upload set directory exists and is not empty: {root.name}")
    placements, unplanned, hashes, maf_expected = gather(args, plan)
    for rel, src in sorted(placements.items()):
        link_or_copy(src, root / rel)
    root.mkdir(parents=True, exist_ok=True)
    write_manifest(root, args.jobs)
    status, counts, details = check_upload_set(root, plan, maf_expected=maf_expected, expected_sha256=hashes,
                                               unplanned_inputs=unplanned)
    write_check(args.outdir, plan["study"], "C5", status, counts=counts, summary=summary_of(counts), details=details)
    print(f"{plan['study']}: C5 {status}; {counts['files']} files")
    return 0
