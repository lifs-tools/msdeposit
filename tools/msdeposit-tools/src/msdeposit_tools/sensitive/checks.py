"""check-sensitive: the sensitive-path checks S3, S4, S5 and S7 on an assembled upload set (design §8.2).
All are blocking; each writes check_<ID>.json (public counts) and check_<ID>.private.json (details) to --check-dir.

S3 residual fields of every mzML in FILES/DERIVED_FILES (header up to <spectrumList>/<chromatogramList>, parsed
   with lxml): no instrument serial (MS:1000529), no fileDescription/contact, no free-text userParam (value not
   empty and not a number), sourceFile/@location without directories (allowed: empty, file:///, file:///<name>, or
   file:///<raw_id>…/<inner> for directory formats), sourceFile/@name not an original file name (with
   --runs-normalised), run/@startTimeStamp and MS:1000747 completion times as the timestamp policy says
   (keep: anything; date: date only or T00:00:00; drop: absent), mzML/@id and run/@id equal to the deposited stem,
   and the file is planned.
S4 raw absence: no vendor files or directories (by extension, vendor-internal member names or file signature),
   no archives containing them (zip/tar inspected; other archive formats fail), FILES/RAW_FILES empty or absent;
   every ISA assay file has an empty `Raw Spectral Data File` column and a filled `Comment[Raw data file
   identifier]` column whose values are raw ids of the plan.
S5 mapping isolation: no private artefacts (mapping.tsv, denylist.tsv, hits.tsv, candidates.tsv,
   runs.normalised.csv, raw_ids.tsv, metadata.json, *.private.*, vendor_metadata/, blocked/) under the upload set or
   --outdir; no file there contains the path of --private-dir; --outdir and --private-dir are not nested.
S7 run-key safety: study, run and assay keys, raw ids (process tags), planned deposited names and the relative
   paths of all files under the upload set and --outdir contain no denylist token and no path separators in keys.
"""

import csv
import fnmatch
import os
import re
import tarfile
import zipfile
from pathlib import Path

from lxml import etree

from ..io import InputError, read_json, read_table, write_check
from .denylist import Matcher, read_denylist, strip_data_ext

ALL_CHECKS = ("S3", "S4", "S5", "S7")
VENDOR_EXT = (".raw", ".d", ".wiff", ".wiff2", ".wiff.scan", ".wiff2.scan", ".lcd", ".baf", ".tdf", ".tdf_bin",
              ".tsf", ".tsf_bin", ".yep", ".fid", ".mcf", ".mcf_idx", ".qgd", ".uep", ".t2d", ".ser", ".sms",
              ".ms2raw")
VENDOR_MEMBER = re.compile(r"(?i)^(?:_func\d+\.(?:dat|idx|sts)|_header\.txt|_extern\.inf|_inlet\.inf|_chroms\.inf|"
                           r"analysis\.(?:tdf|tdf_bin|tsf|tsf_bin|baf|baf_idx|baf_xtr)|msscan\.bin|msprofile\.bin|"
                           r"mspeak\.bin|acqmethod\.xml|msts\.xml)$")
VENDOR_DIR_MEMBERS = ("acqdata", "analysis.tdf", "analysis.baf", "_header.txt")
MAGIC = {b"\x01\xa1F\x00i\x00n\x00n\x00i\x00g\x00a\x00n\x00": "Thermo RAW signature",
         b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1": "compound file (SCIEX/Shimadzu) signature",
         b"SQLite format 3\x00": "SQLite (Bruker TDF) signature"}
ARCHIVE_EXT = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".7z", ".rar", ".gz", ".bz2",
               ".xz")
PRIVATE_NAMES = ("mapping.tsv", "denylist.tsv", "hits.tsv", "candidates.tsv", "runs.normalised.csv", "raw_ids.tsv",
                 "metadata.json", "canary_hits.tsv", "*.private.*")
PRIVATE_DIRS = ("vendor_metadata", "blocked")
RAW_COL, RAW_ID_COL = "raw spectral data file", "comment[raw data file identifier]"
NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}(?:T00:00:00(?:\.0+)?(?:Z|[+-]00:?00)?)?")


def walk(root: Path) -> list[Path]:
    """Files and directories below root (symlinks followed, loops avoided)."""
    out, seen = [], set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen:
            dirnames[:] = []
            continue
        seen.add(real)
        dirnames.sort()
        out += [Path(dirpath) / d for d in dirnames] + [Path(dirpath) / f for f in sorted(filenames)]
    return out


def is_vendor_name(name: str) -> bool:
    low = name.lower()
    if low.endswith((".gz", ".bz2", ".xz")):
        low = low.rsplit(".", 1)[0]
    return low.endswith(VENDOR_EXT) or bool(VENDOR_MEMBER.match(Path(low).name))


# --- S3 -------------------------------------------------------------------------------------------------------------
def mzml_header(path: Path) -> dict:
    """Fields of the mzML header (everything before the spectrum/chromatogram lists)."""
    h = dict(mzml_id=None, run_id=None, start=None, serials=0, contacts=0, user_params=[], source_files=[],
             completion_times=[])
    ctx = etree.iterparse(str(path), events=("start",), huge_tree=True, resolve_entities=False, no_network=True)
    for _, el in ctx:
        tag = etree.QName(el).localname
        if tag in ("spectrumList", "chromatogramList"):
            break
        if tag == "mzML":
            h["mzml_id"] = el.get("id")
        elif tag == "run":
            h["run_id"], h["start"] = el.get("id"), el.get("startTimeStamp")
        elif tag == "contact":
            h["contacts"] += 1
        elif tag == "sourceFile":
            h["source_files"].append(dict(name=el.get("name", ""), location=el.get("location", "")))
        elif tag == "cvParam":
            if el.get("accession") == "MS:1000529":
                h["serials"] += 1
            elif el.get("accession") == "MS:1000747":
                h["completion_times"].append(el.get("value", ""))
        elif tag == "userParam":
            h["user_params"].append(dict(name=el.get("name", ""), value=el.get("value", "")))
    return h


def location_ok(location: str, raw_ids: set[str]) -> bool:
    p = re.sub(r"^file:(?://)?", "", location or "").replace("\\", "/").strip("/")
    if not p:
        return True
    parts = [x for x in p.split("/") if x]
    if re.match(r"[A-Za-z]:$|[A-Za-z]:", parts[0]):
        return False
    return len(parts) == 1 or any(parts[0] == r or parts[0].startswith(r + ".") for r in raw_ids)


def timestamp_ok(value: str | None, policy: str) -> bool:
    if policy == "keep":
        return True
    if policy == "drop":
        return not value
    return not value or bool(DATE_ONLY.fullmatch(value.strip()))


def check_s3(upload: Path, plan: dict, policy: str, originals: set[str]) -> tuple[str, dict, str, list]:
    planned = {Path(r["deposited_mzml"]).stem for r in plan.get("runs", [])}
    raw_ids = {r["raw_id"] for r in plan.get("runs", []) if r.get("raw_id")}
    files = sorted(p for p in (upload / "FILES" / "DERIVED_FILES").glob("*")
                   if p.is_file() and p.name.lower().endswith(".mzml"))
    counts = dict(files=len(files), files_failed=0, serials=0, contacts=0, user_params=0, source_file_paths=0,
                  original_names=0, timestamps=0, id_mismatches=0, unplanned=0, unreadable=0)
    details = []
    for f in files:
        stem = f.name[: -len(".mzML")]
        issues = []
        try:
            h = mzml_header(f)
        except (etree.XMLSyntaxError, OSError) as e:
            counts["unreadable"] += 1
            details.append(dict(file=f.name, issues=[f"not readable: {e.__class__.__name__}"]))
            counts["files_failed"] += 1
            continue
        if stem not in planned:
            counts["unplanned"] += 1
            issues.append("not in the deposit plan")
        if h["serials"]:
            counts["serials"] += h["serials"]
            issues.append("instrument serial number (MS:1000529) present")
        if h["contacts"]:
            counts["contacts"] += h["contacts"]
            issues.append("fileDescription/contact present")
        free = [u for u in h["user_params"] if u["value"].strip() and not NUMBER.fullmatch(u["value"].strip())]
        if free:
            counts["user_params"] += len(free)
            issues.append(f"{len(free)} free-text userParams in the header: " + ", ".join(u["name"] for u in free))
        for sf in h["source_files"]:
            if not location_ok(sf["location"], raw_ids):
                counts["source_file_paths"] += 1
                issues.append(f"sourceFile location with directories: {sf['location']}")
            if sf["name"] and strip_data_ext(sf["name"]).lower() in originals:
                counts["original_names"] += 1
                issues.append(f"sourceFile name is an original file name: {sf['name']}")
        bad_ts = [t for t in [h["start"]] + h["completion_times"] if not timestamp_ok(t, policy)]
        if bad_ts:
            counts["timestamps"] += len(bad_ts)
            issues.append(f"timestamps do not match policy '{policy}': {bad_ts}")
        for what, value in (("mzML/@id", h["mzml_id"]), ("run/@id", h["run_id"])):
            if value != stem:
                counts["id_mismatches"] += 1
                issues.append(f"{what} '{value}' is not the deposited stem")
        if issues:
            counts["files_failed"] += 1
            details.append(dict(file=f.name, issues=issues))
    if not files:
        details.append(dict(file="", issues=["no mzML in FILES/DERIVED_FILES"]))
    status = "fail" if counts["files_failed"] or not files else "pass"
    summary = f"{counts['files']} mzML checked, {counts['files_failed']} with residual identifying fields"
    return status, counts, summary, details


# --- S4 -------------------------------------------------------------------------------------------------------------
def archive_members(path: Path) -> list[str] | None:
    """Member names of a zip or tar archive; None when it cannot be inspected."""
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                return z.namelist()
        if tarfile.is_tarfile(path):
            with tarfile.open(path) as t:
                return t.getnames()
    except (zipfile.BadZipFile, tarfile.TarError, OSError):
        return None
    return None


def read_isa(path: Path) -> tuple[list[str], list[list[str]]]:
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        rows = [r for r in csv.reader(fh, delimiter="\t", quotechar='"') if any(c.strip() for c in r)]
    return ([re.sub(r"\s+", " ", h.strip()).lower() for h in rows[0]] if rows else []), rows[1:]


def check_s4(upload: Path, plan: dict) -> tuple[str, dict, str, list]:
    raw_ids = {r["raw_id"] for r in plan.get("runs", []) if r.get("raw_id")}
    counts = dict(files=0, vendor_files=0, vendor_dirs=0, signatures=0, archives_with_raw=0,
                  uninspectable_archives=0, raw_files_dir=0, assay_files=0, assay_raw_filled=0,
                  assay_missing_raw_id=0, unknown_raw_ids=0)
    details = []

    def bad(kind, rel, msg):
        counts[kind] += 1
        details.append(dict(path=rel, issue=msg))

    for p in walk(upload):
        rel = p.relative_to(upload).as_posix()
        if p.is_dir():
            names = {x.name.lower() for x in p.iterdir()}
            if p.name.lower().endswith((".d", ".raw")) or names & set(VENDOR_DIR_MEMBERS):
                bad("vendor_dirs", rel, "vendor directory format")
            continue
        counts["files"] += 1
        if is_vendor_name(p.name):
            bad("vendor_files", rel, "vendor raw file name or extension")
            continue
        with open(p, "rb") as fh:
            head = fh.read(32)
        sig = next((v for k, v in MAGIC.items() if head.startswith(k)), None)
        if sig:
            bad("signatures", rel, sig)
            continue
        low = p.name.lower()
        if low.endswith(ARCHIVE_EXT) or head.startswith((b"PK\x03\x04", b"7z\xbc\xaf", b"Rar!")):
            if low.endswith((".gz", ".bz2", ".xz")) and not low.endswith((".tar.gz", ".tar.bz2", ".tar.xz")) \
                    and not tarfile.is_tarfile(p):
                continue        # a single compressed file; its inner name was checked above
            members = archive_members(p)
            if members is None:
                bad("uninspectable_archives", rel, "archive that cannot be inspected")
            elif any(is_vendor_name(m.rstrip("/")) or any(is_vendor_name(x) for x in m.split("/")[:-1])
                     for m in members):
                bad("archives_with_raw", rel, "archive containing vendor raw data")
    raw_dir = upload / "FILES" / "RAW_FILES"
    if raw_dir.is_dir() and any(raw_dir.iterdir()):
        bad("raw_files_dir", "FILES/RAW_FILES", "FILES/RAW_FILES is not empty")
    assays = sorted(upload.glob("a_*.txt"))
    counts["assay_files"] = len(assays)
    for a in assays:
        header, rows = read_isa(a)
        for i, col in enumerate(header):
            if col == RAW_COL and any(i < len(r) and r[i].strip() for r in rows):
                bad("assay_raw_filled", a.name, "'Raw Spectral Data File' is not empty")
        if RAW_ID_COL not in header:
            bad("assay_missing_raw_id", a.name, "column 'Comment[Raw data file identifier]' missing")
            continue
        i = header.index(RAW_ID_COL)
        values = [r[i].strip() if i < len(r) else "" for r in rows]
        if not values or not all(values):
            bad("assay_missing_raw_id", a.name, "'Comment[Raw data file identifier]' has empty cells")
        unknown = sorted({v for v in values if v and raw_ids and v not in raw_ids})
        if unknown:
            bad("unknown_raw_ids", a.name, f"raw data file identifiers not in the plan: {unknown}")
    if not assays:
        details.append(dict(path="", issue="no ISA assay files"))
    failures = sum(v for k, v in counts.items() if k not in ("files", "assay_files"))
    status = "fail" if failures or not assays else "pass"
    return status, counts, f"{counts['files']} files checked, {failures} raw-data findings", details


# --- S5 -------------------------------------------------------------------------------------------------------------
def private_name(name: str, extra: list[str]) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatch(low, pat.lower()) for pat in (*PRIVATE_NAMES, *extra))


def contains_any(path: Path, needles: list[bytes], chunk: int = 8 << 20) -> bool:
    keep = max(len(n) for n in needles)
    carry = b""
    with open(path, "rb") as fh:
        while data := fh.read(chunk):
            buf = carry + data
            if any(n in buf for n in needles):
                return True
            carry = buf[-keep:]
    return False


def nested(a: Path, b: Path) -> bool:
    a, b = a.resolve(), b.resolve()
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def check_s5(upload: Path, private_dir: Path, outdir: Path | None, extra: list[str]) -> tuple[str, dict, str, list]:
    counts = dict(files=0, private_artefacts=0, private_path_references=0, nested_outdirs=0)
    details = []
    needles = {str(private_dir), str(private_dir.resolve()), str(private_dir.absolute())}
    needles = [n.rstrip("/").encode() for n in needles if len(n.rstrip("/")) > 1]
    roots = [("upload_set", upload)] + ([("outdir", outdir)] if outdir and outdir.exists() else [])
    if outdir is not None and nested(outdir, private_dir):
        counts["nested_outdirs"] += 1
        details.append(dict(root="outdir", path="", issue="outdir and private dir are nested"))
    for label, root in roots:
        for p in walk(root):
            rel = p.relative_to(root).as_posix()
            if p.is_dir():
                if p.name.lower() in PRIVATE_DIRS:
                    counts["private_artefacts"] += 1
                    details.append(dict(root=label, path=rel, issue="private directory"))
                continue
            counts["files"] += 1
            if private_name(p.name, extra):
                counts["private_artefacts"] += 1
                details.append(dict(root=label, path=rel, issue="private artefact"))
            if needles and contains_any(p, needles):
                counts["private_path_references"] += 1
                details.append(dict(root=label, path=rel, issue="refers to the private directory path"))
    failures = counts["private_artefacts"] + counts["private_path_references"] + counts["nested_outdirs"]
    summary = f"{counts['files']} files checked, {failures} isolation findings"
    return ("fail" if failures else "pass"), counts, summary, details


# --- S7 -------------------------------------------------------------------------------------------------------------
def check_s7(study: str, upload: Path, plan: dict, matcher: Matcher, runs: list[dict],
             outdir: Path | None) -> tuple[str, dict, str, list]:
    keys: list[tuple[str, str]] = [("study", study)]
    for r in plan.get("runs", []):
        keys += [("run", r.get("run", "")), ("assay", r.get("assay", "")), ("raw_id", r.get("raw_id", "")),
                 ("deposited", r.get("deposited_mzml", "")), ("deposited", r.get("derived_path", "")),
                 ("deposited", r.get("ms_assay_name", "")), ("deposited", r.get("raw_path", ""))]
    for a in plan.get("assays", []):
        keys += [("assay", a.get("assay", "")), ("deposited", a.get("file", "")), ("deposited", a.get("maf", ""))]
    keys += [("deposited", plan.get(k) or "") for k in ("prefix", "sample_file", "investigation_file")]
    for r in runs:
        keys += [("run", r.get("run", "")), ("assay", r.get("assay", "")), ("raw_id", r.get("raw_id", "")),
                 ("deposited", r.get("deposited_name", ""))]
    for label, root in [("upload_set", upload)] + ([("outdir", outdir)] if outdir and outdir.exists() else []):
        keys += [(f"path:{label}", p.relative_to(root).as_posix()) for p in walk(root)]
    keys = list(dict.fromkeys((k, v) for k, v in keys if v))
    counts = dict(keys=len(keys), hits=0, separators=0)
    details = []
    for kind, value in keys:
        if kind in ("run", "assay", "raw_id", "study") and re.search(r"[\\/]", value):
            counts["separators"] += 1
            details.append(dict(kind=kind, value=value, issue="path separator in key"))
        for e in matcher.search(value):
            counts["hits"] += 1
            details.append(dict(kind=kind, value=value, token=e.token, issue="denylist token"))
    failures = counts["hits"] + counts["separators"]
    status = "fail" if failures or not matcher else "pass"
    summary = f"{counts['keys']} keys and published paths checked, {failures} findings" + \
        ("" if matcher else "; denylist is empty")
    return status, counts, summary, details


# --- CLI ------------------------------------------------------------------------------------------------------------
def add_arguments(p):
    p.add_argument("--study", required=True)
    p.add_argument("--upload-set", required=True, type=Path, help="assembled upload set of the study")
    p.add_argument("--plan", required=True, type=Path)
    p.add_argument("--private-dir", type=Path, help="private_outdir (S5)")
    p.add_argument("--outdir", type=Path, help="published outdir (S5, S7), optional")
    p.add_argument("--runs-normalised", type=Path, help="runs.normalised.csv (S3 original names, S7 keys)")
    p.add_argument("--denylist", type=Path, help="denylist.tsv (S7)")
    p.add_argument("--timestamp-policy", choices=("keep", "date", "drop"), default="keep")
    p.add_argument("--checks", default=",".join(ALL_CHECKS), help="comma-separated subset of S3,S4,S5,S7")
    p.add_argument("--private-patterns", nargs="*", default=[], help="extra file-name patterns that are private (S5)")
    p.add_argument("--check-dir", type=Path, default=Path("."), help="where the check files are written")


def run(args) -> int:
    checks = [c.strip().upper() for c in args.checks.split(",") if c.strip()]
    unknown = [c for c in checks if c not in ALL_CHECKS]
    if unknown:
        raise InputError(f"unknown checks {unknown}; allowed {list(ALL_CHECKS)}")
    if not args.upload_set.is_dir():
        raise InputError("upload set directory not found")
    if "S5" in checks and not args.private_dir:
        raise InputError("S5 needs --private-dir")
    if "S7" in checks and not args.denylist:
        raise InputError("S7 needs --denylist")
    plan = read_json(args.plan, "msdeposit/plan/1")
    runs = [r for r in read_table(args.runs_normalised, ",") if r.get("study") == args.study] \
        if args.runs_normalised else []
    originals = {strip_data_ext(Path(p).name).lower() for r in runs
                 for p in [r.get("path", "")] + (r.get("companions") or "").split(";") if p}
    for c in checks:
        if c == "S3":
            res = check_s3(args.upload_set, plan, args.timestamp_policy, originals)
        elif c == "S4":
            res = check_s4(args.upload_set, plan)
        elif c == "S5":
            res = check_s5(args.upload_set, args.private_dir, args.outdir, args.private_patterns)
        else:
            res = check_s7(args.study, args.upload_set, plan, Matcher(read_denylist(args.denylist)), runs,
                           args.outdir)
        status, counts, summary, details = res
        write_check(args.check_dir, args.study, c, status, counts=counts, summary=summary, details=details)
        print(f"{c}: {status} ({summary})")
    return 0
