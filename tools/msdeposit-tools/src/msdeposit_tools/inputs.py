"""validate-inputs (check C1): validate the input sheets of design §4 and write the normalised per-study inputs.

Outputs (CONTRACTS.md §1) in --outdir:
  runs.normalised.csv     private: run sheet with vendor, raw_id, companions and absolute paths resolved
  raw_ids.tsv             private: study, raw_id, path
  <study>/study.json      public-safe study inputs (no paths)
  <study>/check_C1.json   C1 result per study (+ .private.json with the error list)
Exit 1 when any error was found (fail fast); the check files are written either way.
"""

import copy
import csv
import re
from collections import Counter, defaultdict
from pathlib import Path

import yaml

from .io import InputError, VENDORS, read_table, safe_name, write_check, write_json, write_table

RUN_COLUMNS = ["study", "run", "assay", "sample", "format", "path", "vendor", "companions", "run_index", "polarity",
               "deposited_name", "raw_id"]
RUN_REQUIRED = ["study", "run", "assay", "sample", "format", "path"]
FORMATS = ("vendor_raw", "mzml", "pwiz_text")
POLARITIES = ("positive", "negative", "alternating")
TECHNIQUES = ("LC-MS", "DI-MS", "FIA-MS", "GC-MS", "CE-MS", "MALDI-MS", "SFC-MS")
ASSAY_COLUMNS = ["study", "assay", "technique", "polarity", "acquisition", "column_model", "column_type",
                 "injection_system", "measurement_type", "instrument_fallback", "protocol_keys"]
FEATURE_COLUMNS = ["study", "assay", "feature", "name", "level", "formula", "mz", "rt", "adduct", "charge",
                   "database_id", "quantifier"]
VALUE_COLUMNS = ["study", "feature", "run", "study_variable", "value", "unit", "variation", "variation_type"]
VARIATION_TYPES = ("sd", "cv_percent", "none", "")
SV_COLUMNS = ["study", "study_variable", "description", "average_function"]
MEMBER_COLUMNS = ["study", "study_variable", "sample"]
TRANSITION_COLUMNS = ["study", "assay", "transition", "q1", "q3", "polarity", "feature", "collision_energy"]
CONTACT_COLUMNS = ["study", "first_name", "last_name", "email", "affiliation", "address", "orcid", "roles"]
NUMERIC = {"mz", "rt", "charge", "value", "variation", "q1", "q3", "collision_energy"}
SAMPLE_PREFIXES = ("characteristics", "factor", "comment")
VENDOR_RAW_EXT = (".raw", ".d", ".wiff", ".wiff2", ".lcd", ".baf", ".tdf")


def add_arguments(p):
    p.add_argument("--input", required=True, type=Path, help="run sheet (CSV, one row per run)")
    p.add_argument("--assays", required=True, type=Path)
    p.add_argument("--samples", required=True, type=Path)
    p.add_argument("--studies", required=True, type=Path, help="studies YAML")
    p.add_argument("--contacts", type=Path)
    p.add_argument("--publication", type=Path)
    p.add_argument("--features", type=Path)
    p.add_argument("--values", type=Path)
    p.add_argument("--study-variables", type=Path)
    p.add_argument("--study-variable-members", type=Path)
    p.add_argument("--transitions", type=Path)
    p.add_argument("--denylist", type=Path, help="user denylist (one token per line or TSV with 'token')")
    p.add_argument("--mode", choices=("standard", "sensitive"), default="standard")
    p.add_argument("--base-dir", type=Path, default=Path.cwd(), help="base for relative paths in the run sheet")
    p.add_argument("--outdir", type=Path, default=Path("."))


class Errors:
    """Collects messages per study; `None` = global."""

    def __init__(self):
        self.items: list[tuple[str | None, str, str]] = []

    def error(self, study, msg):
        self.items.append((study, "error", msg))

    def warn(self, study, msg):
        self.items.append((study, "warning", msg))

    def of(self, study, level=None):
        return [m for s, lv, m in self.items if s in (study, None) and (level is None or lv == level)]


def header_of(path: Path) -> list[str]:
    delim = "\t" if path.suffix.lower() in (".tsv", ".txt") else ","
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return [h.strip() for h in next(csv.reader(fh, delimiter=delim), [])]


def load_sheet(path: Path, what: str, required: list[str], allowed: list[str] | None, errs: Errors) -> list[dict]:
    if not path.exists():
        raise InputError(f"{what}: file not found: {path}")
    header = header_of(path)
    dup = [h for h, n in Counter(header).items() if n > 1]
    if dup:
        errs.error(None, f"{what}: duplicate columns {dup}")
    missing = [c for c in required if c not in header]
    if missing:
        raise InputError(f"{what}: missing required columns {missing}")
    if allowed is not None:
        unknown = [h for h in header if h not in allowed]
        if unknown:
            errs.error(None, f"{what}: unknown columns {unknown}")
    rows = read_table(path)
    for i, r in enumerate(rows, 2):
        r["_line"] = i
        for c in required:
            if not r.get(c):
                errs.error(r.get("study") or None, f"{what} line {i}: '{c}' is empty")
    return rows


def detect_vendor(path: Path) -> str:
    name = path.name.lower()
    if path.is_dir():
        if name.endswith(".raw"):
            return "waters"
        if name.endswith(".d"):
            if (path / "analysis.tdf").exists():
                return "bruker_tdf"
            if (path / "analysis.baf").exists():
                return "bruker_baf"
            if (path / "AcqData").is_dir():
                return "agilent"
        return "unknown"
    if name.endswith(".raw"):
        return "thermo"
    if name.endswith((".wiff", ".wiff2")):
        return "sciex"
    if name.endswith(".lcd"):
        return "shimadzu"
    return "unknown"


def _parse_number(v: str, what: str, errs: Errors, study):
    if v in ("", None):
        return None
    try:
        f = float(v)
    except ValueError:
        errs.error(study, f"{what}: not a number: {v!r}")
        return None
    return int(f) if f.is_integer() and "." not in v and "e" not in v.lower() else f


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def load_studies(path: Path) -> dict[str, dict]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict) or not isinstance(doc.get("studies"), dict):
        raise InputError("studies YAML: expected a mapping with a 'studies' mapping (and optional 'defaults')")
    defaults = doc.get("defaults") or {}
    return {str(k): deep_merge(defaults, v or {}) for k, v in doc["studies"].items()}


def parse_sample_columns(header: list[str], errs: Errors) -> dict:
    """'characteristics:Organism::source' -> ('characteristics', 'Organism', 'source')."""
    cols = {}
    for h in header:
        if h in ("study", "source_name", "sample_name"):
            continue
        m = re.fullmatch(r"(characteristics|factor|comment):([^:]+?)(?:::(source|accession|unit))?", h)
        if not m:
            errs.error(None, f"samples: column '{h}' is not of the form characteristics:<Name>[::source|accession|unit], "
                             f"factor:<Name>[...] or comment:<Name>")
            continue
        if m[1] == "comment" and m[3]:
            errs.error(None, f"samples: comment columns take no '::{m[3]}' suffix ({h})")
        cols[h] = (m[1], m[2].strip(), m[3] or "value")
    return cols


def build_samples(rows: list[dict], cols: dict) -> dict[str, list[dict]]:
    out = defaultdict(list)
    for r in rows:
        entry = {"source_name": r["source_name"], "sample_name": r["sample_name"], "characteristics": [],
                 "factors": [], "comments": []}
        groups: dict[tuple, dict] = {}
        for h, (kind, name, part) in cols.items():
            key = (kind, name)
            if key not in groups:
                groups[key] = {"name": name, "value": ""} if kind == "comment" else \
                    {"name": name, "value": "", "source": "", "accession": "", "unit": ""}
                entry["characteristics" if kind == "characteristics" else
                      "factors" if kind == "factor" else "comments"].append(groups[key])
            groups[key][part] = r.get(h, "")
        out[r["study"]].append(entry)
    return out


def load_tokens(path: Path) -> list[str]:
    if path.suffix.lower() in (".tsv", ".csv"):
        return [r["token"] for r in read_table(path) if r.get("token")]
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]


def validate(args) -> tuple[dict, list[dict], list[dict], Errors]:
    errs = Errors()
    runs = load_sheet(args.input, "run sheet", RUN_REQUIRED, RUN_COLUMNS, errs)
    assays = load_sheet(args.assays, "assays", ["study", "assay", "technique"], ASSAY_COLUMNS, errs)
    sample_rows = load_sheet(args.samples, "samples", ["study", "source_name", "sample_name"], None, errs)
    sample_cols = parse_sample_columns(header_of(args.samples), errs)
    studies_cfg = load_studies(args.studies)
    optional = {}
    for key, what, cols, req in (
            ("features", "features", FEATURE_COLUMNS, ["study", "feature", "name"]),
            ("values", "values", VALUE_COLUMNS, ["study", "feature", "value"]),
            ("study_variables", "study variables", SV_COLUMNS, ["study", "study_variable"]),
            ("study_variable_members", "study variable members", MEMBER_COLUMNS, MEMBER_COLUMNS),
            ("transitions", "transitions", TRANSITION_COLUMNS, ["study", "assay", "transition", "q1", "q3"]),
            ("contacts", "contacts", CONTACT_COLUMNS, ["first_name", "last_name"])):
        path = getattr(args, key)
        optional[key] = load_sheet(path, what, req, cols, errs) if path else []
    publication = yaml.safe_load(args.publication.read_text(encoding="utf-8")) if args.publication else None
    user_tokens = load_tokens(args.denylist) if args.denylist else []

    study_keys = sorted({r["study"] for r in runs if r["study"]})
    for s in study_keys:
        if s not in studies_cfg:
            errs.error(s, f"study '{s}' is not defined in the studies YAML")
    for s in studies_cfg:
        if s not in study_keys:
            errs.warn(s, f"study '{s}' in the studies YAML has no runs")
        if not studies_cfg[s].get("title"):
            errs.error(s, "studies YAML: 'title' missing")

    # assays
    assay_def: dict[tuple, dict] = {}
    for a in assays:
        s = a["study"]
        if (s, a["assay"]) in assay_def:
            errs.error(s, f"assays line {a['_line']}: duplicate assay '{a['assay']}'")
        if a["technique"] and a["technique"] not in TECHNIQUES:
            errs.error(s, f"assays line {a['_line']}: technique '{a['technique']}' not in {list(TECHNIQUES)}")
        if a.get("polarity") and a["polarity"] not in POLARITIES:
            errs.error(s, f"assays line {a['_line']}: polarity '{a['polarity']}' not in {list(POLARITIES)}")
        assay_def[(s, a["assay"])] = a

    # samples
    samples_by_study = build_samples(sample_rows, sample_cols)
    sample_names = defaultdict(set)
    for r in sample_rows:
        if r["sample_name"] in sample_names[r["study"]]:
            errs.error(r["study"], f"samples line {r['_line']}: duplicate sample_name")
        sample_names[r["study"]].add(r["sample_name"])

    # runs
    base = args.base_dir.resolve()
    seen_runs = set()
    by_path: dict[tuple, list] = defaultdict(list)
    for r in runs:
        s, line = r["study"], r["_line"]
        if (s, r["run"]) in seen_runs:
            errs.error(s, f"run sheet line {line}: duplicate run key '{r['run']}'")
        seen_runs.add((s, r["run"]))
        if (s, r["assay"]) not in assay_def:
            errs.error(s, f"run sheet line {line}: assay '{r['assay']}' not defined in the assays sheet")
        if r["sample"] and r["sample"] not in sample_names[s]:
            errs.error(s, f"run sheet line {line}: sample '{r['sample']}' not defined in the samples sheet")
        if r["format"] not in FORMATS:
            errs.error(s, f"run sheet line {line}: format '{r['format']}' not in {list(FORMATS)}")
        p = Path(r["path"])
        p = p if p.is_absolute() else base / p
        r["path"] = str(p)
        if not p.exists():
            errs.error(s, f"run sheet line {line}: path not found: {p}")
        if r.get("vendor") and r["vendor"] not in VENDORS:
            errs.error(s, f"run sheet line {line}: vendor '{r['vendor']}' not in {list(VENDORS)}")
        if r["format"] == "vendor_raw":
            detected = detect_vendor(p) if p.exists() else "unknown"
            if r.get("vendor") and detected not in ("unknown", r["vendor"]):
                errs.error(s, f"run sheet line {line}: vendor '{r['vendor']}' given, '{detected}' detected")
            r["vendor"] = r.get("vendor") or detected
            if r["vendor"] == "unknown":
                errs.error(s, f"run sheet line {line}: vendor could not be detected; set the 'vendor' column")
        elif not r.get("vendor"):
            errs.error(s, f"run sheet line {line}: 'vendor' is required for format '{r['format']}'")
        if r.get("polarity") and r["polarity"] not in POLARITIES:
            errs.error(s, f"run sheet line {line}: polarity '{r['polarity']}' not in {list(POLARITIES)}")
        a = assay_def.get((s, r["assay"]))
        if a and a.get("polarity") and r.get("polarity") and r["polarity"] != a["polarity"]:
            errs.error(s, f"run sheet line {line}: polarity '{r['polarity']}' differs from assay '{a['polarity']}'")
        if r.get("run_index"):
            if not r["run_index"].isdigit() or int(r["run_index"]) < 1:
                errs.error(s, f"run sheet line {line}: run_index must be a positive integer")
        comps = [c.strip() for c in (r.get("companions") or "").split(";") if c.strip()]
        comps = [str(Path(c) if Path(c).is_absolute() else base / c) for c in comps]
        if r["vendor"] == "sciex" and not comps and p.exists():
            comps = [str(x) for x in sorted(p.parent.glob(p.name + "*")) if x != p and x.is_file()]
        for c in comps:
            if not Path(c).exists():
                errs.error(s, f"run sheet line {line}: companion not found: {c}")
        r["companions"] = ";".join(comps)
        if r.get("deposited_name") and safe_name(r["deposited_name"]) != r["deposited_name"]:
            errs.error(s, f"run sheet line {line}: deposited_name may only contain .-_A-Za-z0-9")
        by_path[(s, r["path"])].append(r)

    # raw ids: user-given ids must be consistent per path; defaults numbered by sorted path per study
    raw_rows = []
    for s in study_keys:
        paths = sorted(p for (st, p) in by_path if st == s)
        used = {r["raw_id"] for (st, _), rs in by_path.items() if st == s for r in rs if r.get("raw_id")}
        n = 0
        for p in paths:
            rs = by_path[(s, p)]
            given = {r["raw_id"] for r in rs if r.get("raw_id")}
            if len(given) > 1:
                errs.error(s, f"run sheet: rows sharing one path have different raw_id values {sorted(given)}")
            if given:
                rid = next(iter(given))
            else:
                n += 1
                while f"{safe_name(s)}_RAW_{n:03d}" in used:
                    n += 1
                rid = f"{safe_name(s)}_RAW_{n:03d}"
            for r in rs:
                r["raw_id"] = rid
            raw_rows.append(dict(study=s, raw_id=rid, path=p))
            if len(rs) > 1 and not all(r.get("run_index") for r in rs):
                errs.error(s, f"run sheet: {len(rs)} rows share one path; each needs a run_index")
            idx = [r.get("run_index") for r in rs if r.get("run_index")]
            if len(idx) != len(set(idx)):
                errs.error(s, "run sheet: duplicate run_index for one path")
        rid_paths = Counter(r["raw_id"] for r in raw_rows if r["study"] == s)
        for rid, k in rid_paths.items():
            if k > 1:
                errs.error(s, f"run sheet: raw_id '{rid}' used for {k} different paths")

    # assay homogeneity declared in the sheet (vendor); instrument models are checked by the plan (C3)
    for (s, a), _ in assay_def.items():
        vendors = {r["vendor"] for r in runs if r["study"] == s and r["assay"] == a}
        if len(vendors) > 1:
            errs.error(s, f"assay '{a}': runs from several vendors {sorted(vendors)}")

    # sensitive mode: run and assay keys appear in logs, tags and traces and must not identify the lab
    if args.mode == "sensitive":
        for r in runs:
            stem = Path(r["path"]).name
            for suffix in VENDOR_RAW_EXT + (".mzml", ".txt"):
                if stem.lower().endswith(suffix):
                    stem = stem[: -len(suffix)]
            for key in ("run", "assay"):
                v = r[key]
                if "/" in v or "\\" in v:
                    errs.error(r["study"], f"run sheet line {r['_line']}: {key} key contains a path separator")
                if len(stem) >= 4 and (stem.lower() in v.lower() or v.lower() in stem.lower()):
                    errs.error(r["study"], f"run sheet line {r['_line']}: {key} key repeats the original file name "
                                           f"(not allowed in sensitive mode)")
                for tok in user_tokens:
                    if len(tok) >= 2 and tok.lower() in v.lower():
                        errs.error(r["study"], f"run sheet line {r['_line']}: {key} key contains a denylist token")

    # optional tables
    features = defaultdict(dict)
    for f in optional["features"]:
        s = f["study"]
        if f.get("assay") and (s, f["assay"]) not in assay_def:
            errs.error(s, f"features line {f['_line']}: assay '{f['assay']}' not defined")
        if f["feature"] in features[s] and features[s][f["feature"]].get("assay") == f.get("assay"):
            errs.error(s, f"features line {f['_line']}: duplicate feature '{f['feature']}' for assay")
        features[s][f["feature"]] = f
    svs = defaultdict(set)
    for sv in optional["study_variables"]:
        if sv["study_variable"] in svs[sv["study"]]:
            errs.error(sv["study"], f"study variables line {sv['_line']}: duplicate '{sv['study_variable']}'")
        svs[sv["study"]].add(sv["study_variable"])
    for m in optional["study_variable_members"]:
        if m["study_variable"] not in svs[m["study"]]:
            errs.error(m["study"], f"study variable members line {m['_line']}: unknown study_variable")
        if m["sample"] not in sample_names[m["study"]]:
            errs.error(m["study"], f"study variable members line {m['_line']}: unknown sample '{m['sample']}'")
    for v in optional["values"]:
        s = v["study"]
        if bool(v.get("run")) == bool(v.get("study_variable")):
            errs.error(s, f"values line {v['_line']}: give exactly one of 'run' and 'study_variable'")
        if v["feature"] not in features[s]:
            errs.error(s, f"values line {v['_line']}: unknown feature '{v['feature']}'")
        if v.get("run") and (s, v["run"]) not in seen_runs:
            errs.error(s, f"values line {v['_line']}: unknown run '{v['run']}'")
        if v.get("study_variable") and v["study_variable"] not in svs[s]:
            errs.error(s, f"values line {v['_line']}: unknown study_variable '{v['study_variable']}'")
        if v.get("variation_type", "") not in VARIATION_TYPES:
            errs.error(s, f"values line {v['_line']}: variation_type not in {list(VARIATION_TYPES)}")
    for t in optional["transitions"]:
        if (t["study"], t["assay"]) not in assay_def:
            errs.error(t["study"], f"transitions line {t['_line']}: assay '{t['assay']}' not defined")
    for key in ("features", "values", "transitions"):
        for row in optional[key]:
            for c in NUMERIC & row.keys():
                row[c] = _parse_number(row[c], f"{key} line {row['_line']} column {c}", errs, row["study"])

    model = dict(studies=studies_cfg, assays=assay_def, samples=samples_by_study, optional=optional,
                 publication=publication, study_keys=study_keys)
    return model, runs, raw_rows, errs


def _rows_for(rows: list[dict], study: str) -> list[dict]:
    return [{k: v for k, v in r.items() if k not in ("study", "_line")}
            for r in rows if r.get("study") in (study, "", None)]


def study_json(s: str, model: dict, runs: list[dict], mode: str) -> dict:
    cfg = model["studies"].get(s, {})
    opt = model["optional"]
    contacts = []
    for c in _rows_for(opt["contacts"], s):
        c["roles"] = [x.strip() for x in (c.get("roles") or "").split(";") if x.strip()]
        contacts.append(c)
    return dict(
        schema="msdeposit/study/1", study=s, mode=mode, config=dict(cfg, accession=cfg.get("accession")),
        assays=[{c: a.get(c, "") for c in ASSAY_COLUMNS[1:-1]}
                | {"protocol_keys": [x.strip() for x in a.get("protocol_keys", "").split(";") if x.strip()]}
                for (st, _), a in model["assays"].items() if st == s],
        samples=model["samples"].get(s, []),
        runs=[dict(run=r["run"], assay=r["assay"], sample=r["sample"], format=r["format"], vendor=r["vendor"],
                   run_index=int(r["run_index"]) if r.get("run_index") else None, polarity=r.get("polarity", ""),
                   deposited_name=r.get("deposited_name", ""), raw_id=r["raw_id"])
              for r in runs if r["study"] == s],
        features=_rows_for(opt["features"], s), values=_rows_for(opt["values"], s),
        study_variables=_rows_for(opt["study_variables"], s),
        study_variable_members=_rows_for(opt["study_variable_members"], s),
        transitions=_rows_for(opt["transitions"], s), contacts=contacts, publication=model["publication"])


def run(args) -> int:
    model, runs, raw_rows, errs = validate(args)
    out = args.outdir
    write_table(out / "runs.normalised.csv", runs, RUN_COLUMNS, delimiter=",")
    write_table(out / "raw_ids.tsv", raw_rows, ["study", "raw_id", "path"])
    failed = False
    for s in model["study_keys"] or [None]:
        e, w = errs.of(s, "error"), errs.of(s, "warning")
        failed |= bool(e)
        if s is None:
            break
        write_json(out / s / "study.json", study_json(s, model, runs, args.mode))
        write_check(out / s, s, "C1", "fail" if e else "warn" if w else "pass",
                    counts=dict(errors=len(e), warnings=len(w),
                                runs=sum(1 for r in runs if r["study"] == s),
                                raw_files=sum(1 for r in raw_rows if r["study"] == s)),
                    summary=f"{len(e)} input errors, {len(w)} warnings",
                    details=[dict(level="error", message=m) for m in e] + [dict(level="warning", message=m) for m in w])
    for _, level, msg in errs.items:
        print(f"{level.upper()}: {msg}")
    return 1 if failed else 0
