"""deposit-report: the public deposit report of one study (`FILES/QC/deposit_report.html`) and the private report.

The public report is built from an explicit allow-list data model (`PublicReport`, design §9.6), never by filtering:
every field is copied from a known input field and validated against a strict pattern or vocabulary; a value that
does not match is replaced by "(withheld)". It contains
  - study key, accession, mode, generation date, gate status and the ids of failed/missing/skipped checks;
  - per check: id, status, blocking, numeric counts (and the per-run status counts of per-run checks);
    the check's one-line summary only in standard mode;
  - assays (key, ISA/MAF file, technique, polarity, vendor, instrument model, number of runs);
  - runs by deposited name with assay, polarity and raw id (the run key only in standard mode);
  - raw files by raw id (packaging, deposited names), upload files (path, kind, bytes, SHA-256) and totals;
  - plan issue counts by level and code, tool and validator versions, parameters with non-path values.
It never contains paths, original or raw file names, run keys (sensitive), denylist tokens, hit contexts, sample-sheet
free text, error messages or stderr. In sensitive mode (either --mode or the plan/gate says so) check summaries and
string parameters outside a fixed list of enumerated parameters are left out as well.

The private report adds the check summaries and private details, plan issues with messages, the plan's run keys and
samples, and optionally the private mapping (--mapping) and identifier hits (--hits).
Both are self-contained HTML (inline CSS, no scripts, no external resources), every value escaped.
"""

import html
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from . import __version__
from .gate import RANK, check_files
from .io import CHECK_STATUSES, VENDORS, InputError, read_json, read_table

WITHHELD = "(withheld)"
CHECK_ID = re.compile(r"^[CS]\d+$")
COUNT_KEY = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+(),-]{0,79}$")
DEPOSITED = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
PARAM_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
PARAM_VALUE = re.compile(r"^[A-Za-z0-9._,+:-]{0,64}$")
SECRET_NAME = re.compile(r"(email|hook|token|password|secret|key|cred|url|path|dir|file|input|list)", re.I)
# string parameters whose values are enumerations; only these show string values in sensitive mode
ENUM_PARAMS = {"mztab_versions", "finalise_mode", "deposited_naming", "timestamp_policy", "lipid_annotation",
               "thermo_converter", "mtbls_study_category", "controlled_value_profile", "publish_mode"}
# parameter names of the pipeline (design §5); in sensitive mode no other names are shown
PIPELINE_PARAMS = ENUM_PARAMS | {
    "sensitive_data", "deposit_raw", "identifier_scan", "scan_mzml", "canary_scan", "fail_on_identifier_hits",
    "mzml_body_rewrite", "enforce_private_launch", "deposit_report", "private_outdir", "email", "hook_url", "maf",
    "swisslipids_cache", "allow_network", "accept_vendor_licenses", "fail_on_validation_errors",
    "fail_on_todo_placeholders", "outdir", "input", "assays", "samples", "studies", "contacts", "publication",
    "protocols", "features", "values", "study_variables", "study_variable_members", "transitions", "denylist",
    "allowlist"}
POLARITIES = {"positive", "negative", "alternating", ""}
KINDS = {"isa", "maf", "mzml", "raw", "mztab", "mzqc", "report", "other"}
PACKAGING = {"copy", "zip", "none"}
LEVELS = {"error", "warning", "info"}


def add_arguments(p):
    p.add_argument("--study", required=True)
    p.add_argument("--plan", required=True, type=Path)
    p.add_argument("--gate", type=Path, help="gate.json")
    p.add_argument("--checks", nargs="*", type=Path, default=[], help="check_*.json files or directories")
    p.add_argument("--upload-manifest", type=Path)
    p.add_argument("--versions", nargs="*", type=Path, default=[], help="tool versions (YAML or JSON)")
    p.add_argument("--params", type=Path, help="pipeline parameters (JSON)")
    p.add_argument("--mapping", type=Path, help="private mapping.tsv (private report only)")
    p.add_argument("--hits", type=Path, help="private hits.tsv (private report only)")
    p.add_argument("--mode", choices=("standard", "sensitive"), default="standard")
    p.add_argument("--public", type=Path, help="public report (deposit_report.html)")
    p.add_argument("--private", type=Path, help="private report (deposit_report.private.html)")


# --- allow-list data model ----------------------------------------------------------------------------------------

def _match(value, pattern: re.Pattern, default: str = WITHHELD) -> str:
    """The value when it matches the pattern, "" when empty, else `default`."""
    s = "" if value is None else str(value)
    return s if pattern.match(s) else ("" if s == "" else default)


def _enum(value, allowed, default: str = WITHHELD) -> str:
    return value if value in allowed else default


def _int(value) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


@dataclass(frozen=True)
class PublicCheck:
    check: str
    status: str
    blocking: bool
    scope: str
    counts: tuple[tuple[str, float], ...]
    runs: tuple[tuple[str, int], ...] = ()
    summary: str | None = None


@dataclass(frozen=True)
class PublicAssay:
    assay: str
    file: str
    maf: str
    technique: str
    polarity: str
    vendor: str
    instrument: str
    runs: int


@dataclass(frozen=True)
class PublicRun:
    deposited_mzml: str
    assay: str
    polarity: str
    raw_id: str
    run: str | None = None


@dataclass(frozen=True)
class PublicRaw:
    raw_id: str
    packaging: str
    deposited: tuple[str, ...]


@dataclass(frozen=True)
class PublicFile:
    path: str
    kind: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class PublicReport:
    study: str
    accession: str
    mode: str
    redacted: bool
    generated: str
    generator: str
    gate_status: str
    blocking_failures: tuple[str, ...]
    missing_checks: tuple[str, ...]
    skipped_blocking: tuple[str, ...]
    checks: tuple[PublicCheck, ...]
    assays: tuple[PublicAssay, ...]
    runs: tuple[PublicRun, ...]
    raw_files: tuple[PublicRaw, ...]
    files: tuple[PublicFile, ...]
    issues: tuple[tuple[str, str, int], ...]
    tools: tuple[tuple[str, str], ...]
    params: tuple[tuple[str, str], ...]
    totals: dict = field(default_factory=dict)


def _ids(values) -> tuple[str, ...]:
    return tuple(v for v in (values or []) if isinstance(v, str) and CHECK_ID.match(v))


def public_checks(results: list[dict], gate: dict | None, sensitive: bool) -> tuple[PublicCheck, ...]:
    by_check = defaultdict(list)
    for r in results:
        if CHECK_ID.match(str(r.get("check", ""))):
            by_check[r["check"]].append(r)
    out = []
    for check in sorted(by_check, key=lambda c: (c[0], int(c[1:]))):
        rs = by_check[check]
        status = max((_enum(r.get("status"), CHECK_STATUSES, "fail") for r in rs), key=RANK.get)
        if gate and check in gate.get("checks", {}):
            status = _enum(gate["checks"][check], CHECK_STATUSES, status)
        counts = Counter()
        for r in rs:
            for k, v in (r.get("counts") or {}).items():
                if COUNT_KEY.match(str(k)) and isinstance(v, (int, float)) and not isinstance(v, bool):
                    counts[k] += v
        per_run = len(rs) > 1 or any(r.get("scope") == "run" for r in rs)
        runs = tuple(sorted(Counter(_enum(r.get("status"), CHECK_STATUSES, "fail") for r in rs).items()))
        summary = None
        if not sensitive and not per_run:
            summary = str(rs[0].get("summary", ""))[:300]
        out.append(PublicCheck(check=check, status=status, blocking=any(bool(r.get("blocking", True)) for r in rs),
                               scope="run" if per_run else "study", counts=tuple(sorted(counts.items())),
                               runs=runs if per_run else (), summary=summary))
    return tuple(out)


def public_params(params: dict, sensitive: bool) -> tuple[tuple[str, str], ...]:
    """(name, shown value); in sensitive mode only the pipeline's own parameter names (others are counted)."""
    out, hidden = [], 0
    for name in sorted(params or {}):
        if not PARAM_NAME.match(str(name)) or (sensitive and name not in PIPELINE_PARAMS):
            hidden += 1
            continue
        v = params[name]
        if isinstance(v, bool):
            shown = str(v).lower()
        elif isinstance(v, (int, float)):
            shown = str(v)
        elif v is None or v == "":
            shown = ""
        elif (isinstance(v, str) and PARAM_VALUE.match(v) and not SECRET_NAME.search(name)
              and (not sensitive or name in ENUM_PARAMS)):
            shown = v
        else:
            shown = "(not shown)"
        out.append((name, shown))
    if hidden:
        out.append(("(other parameters)", f"{hidden} not shown"))
    return tuple(out)


def load_versions(paths: list[Path]) -> list[tuple[str, str]]:
    """(tool, version) pairs from nf-core versions.yml ({process: {tool: version}}) or flat {tool: version}."""
    pairs = []
    for p in paths:
        data = yaml.safe_load(Path(p).read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise InputError(f"{Path(p).name}: versions must be a mapping")
        for k, v in data.items():
            items = v.items() if isinstance(v, dict) else [(k, v)]
            pairs += [(str(t), str(ver)) for t, ver in items if not isinstance(ver, (dict, list))]
    return pairs


def public_tools(pairs: list[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted({(_match(t, TOKEN), _match(v, TOKEN)) for t, v in pairs}))


def generated_date() -> str:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    when = datetime.fromtimestamp(int(epoch), UTC) if epoch else datetime.now(UTC)
    return when.strftime("%Y-%m-%d")


def build_public(study: str, plan: dict, gate: dict | None, results: list[dict], manifest: list[dict],
                 versions: list[tuple[str, str]], params: dict | None, mode: str,
                 redact: bool | None = None) -> PublicReport:
    """The allow-list model; `redact` (default: sensitive mode) leaves out summaries, run keys, string parameters."""
    sensitive = mode == "sensitive" if redact is None else redact
    assays = tuple(PublicAssay(
        assay=_match(a.get("assay"), KEY), file=_match(a.get("file"), DEPOSITED), maf=_match(a.get("maf"), DEPOSITED),
        technique=_match(a.get("technique"), TOKEN), polarity=_enum(a.get("polarity", ""), POLARITIES),
        vendor=_enum(a.get("vendor"), VENDORS), instrument=_match((a.get("instrument_model") or {}).get("name"), TOKEN),
        runs=len(a.get("runs", []))) for a in plan.get("assays", []))
    runs = tuple(PublicRun(
        deposited_mzml=_match(r.get("deposited_mzml"), DEPOSITED), assay=_match(r.get("assay"), KEY),
        polarity=_enum(r.get("polarity", ""), POLARITIES), raw_id=_match(r.get("raw_id"), KEY),
        run=None if sensitive else _match(r.get("run"), KEY)) for r in plan.get("runs", []))
    raws = tuple(PublicRaw(raw_id=_match(x.get("raw_id"), KEY), packaging=_enum(x.get("packaging"), PACKAGING),
                           deposited=tuple(_match(d, DEPOSITED) for d in x.get("deposited", [])))
                 for x in plan.get("raw_files", []))
    files = tuple(PublicFile(path=_match(r.get("path"), DEPOSITED), kind=_enum(r.get("kind"), KINDS),
                             bytes=int(r["bytes"]) if str(r.get("bytes", "")).isdigit() else 0,
                             sha256=_match(r.get("sha256"), SHA256, "")) for r in manifest)
    issues = Counter((_enum(i.get("level"), LEVELS), _match(i.get("code"), COUNT_KEY)) for i in plan.get("issues", []))
    totals = dict(runs=len(runs), assays=len(assays), raw_files=len(raws), files=len(files),
                  bytes=sum(f.bytes for f in files))
    for f in files:
        totals[f"files_{f.kind}"] = totals.get(f"files_{f.kind}", 0) + 1
        totals[f"bytes_{f.kind}"] = totals.get(f"bytes_{f.kind}", 0) + f.bytes
    gate = gate or {}
    return PublicReport(
        study=_match(study, KEY), accession=_match(plan.get("accession"), KEY), mode=mode, redacted=sensitive,
        generated=generated_date(), generator=f"msdeposit-tools {_match(__version__, TOKEN)}",
        gate_status=_enum(gate.get("status"), ("passed", "blocked"), "not gated"),
        blocking_failures=_ids(gate.get("blocking_failures")), missing_checks=_ids(gate.get("missing_checks")),
        skipped_blocking=_ids(gate.get("skipped_blocking")), checks=public_checks(results, gate, sensitive),
        assays=assays, runs=runs, raw_files=raws, files=files,
        issues=tuple((lv, code, n) for (lv, code), n in sorted(issues.items())), tools=public_tools(versions),
        params=public_params(params or {}, sensitive), totals=totals)


# --- HTML ---------------------------------------------------------------------------------------------------------

CSS = """
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;margin:2rem auto;max-width:72rem;padding:0 1rem;
color:#1d2433;background:#fff;line-height:1.45}
h1{font-size:1.6rem;margin-bottom:.2rem}h2{font-size:1.2rem;margin-top:2rem;border-bottom:1px solid #d8dde6}
table{border-collapse:collapse;width:100%;font-size:.9rem;margin:.5rem 0}
th,td{border:1px solid #d8dde6;padding:.25rem .5rem;text-align:left;vertical-align:top}
th{background:#f2f4f8}td.n{text-align:right;font-variant-numeric:tabular-nums}
code,pre{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.85rem}pre{white-space:pre-wrap;
background:#f6f7f9;padding:.5rem;border:1px solid #e3e6eb;overflow-x:auto}
.s{display:inline-block;padding:0 .45rem;border-radius:.6rem;font-size:.8rem;font-weight:600}
.pass,.passed{background:#dcf3e3;color:#155d2c}.warn{background:#fff1cc;color:#6b4e00}
.fail,.blocked{background:#fbdcdc;color:#8a1c1c}.skipped,.not{background:#e6e8ee;color:#3b4252}
.meta{color:#5b6475}
"""


def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def badge(status: str) -> str:
    return f'<span class="s {esc(status.split()[0])}">{esc(status)}</span>'


def table(headers: list[str], rows: list[list], numeric: set[int] = frozenset()) -> str:
    if not rows:
        return '<p class="meta">none</p>'
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f'<td class="n">{c}</td>' if i in numeric else f"<td>{c}</td>"
                                    for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def page(title: str, sections: list[str]) -> str:
    return ("<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{esc(title)}</title><style>{CSS}</style></head><body>\n" + "\n".join(sections)
            + "\n</body></html>\n")


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return str(n)


def public_sections(r: PublicReport) -> list[str]:
    s = [f"<h1>Deposit report: {esc(r.study)}</h1>",
         f'<p class="meta">Mode {esc(r.mode)} · accession {esc(r.accession or "not assigned")} · generated '
         f"{esc(r.generated)} by {esc(r.generator)}</p>",
         f"<p>Gate: {badge(r.gate_status)}</p>"]
    gate_rows = [[esc(what), esc(", ".join(ids))] for what, ids in (
        ("blocking checks failed", r.blocking_failures), ("expected checks missing", r.missing_checks),
        ("blocking checks not run", r.skipped_blocking)) if ids]
    if gate_rows:
        s.append(table(["gate", "checks"], gate_rows))
    s.append("<h2>Checks</h2>")
    rows = []
    for c in r.checks:
        counts = ", ".join(f"{k}: {v:g}" for k, v in c.counts)
        runs = ", ".join(f"{k}: {n}" for k, n in c.runs)
        rows.append([esc(c.check), badge(c.status), "yes" if c.blocking else "no", esc(c.scope),
                     esc(runs), esc(counts)] + ([] if r.redacted else [esc(c.summary or "")]))
    s.append(table(["check", "status", "blocking", "scope", "runs", "counts"] + ([] if r.redacted else ["summary"]),
                   rows))
    t = r.totals
    s += ["<h2>Upload set</h2>",
          table(["runs", "assays", "raw data units", "files", "size"],
                [[t.get("runs", 0), t.get("assays", 0), t.get("raw_files", 0), t.get("files", 0),
                  esc(human(t.get("bytes", 0)))]], {0, 1, 2, 3, 4}),
          table(["kind", "files", "size"], [[esc(k), t[f"files_{k}"], esc(human(t[f"bytes_{k}"]))]
                                            for k in sorted(KINDS) if f"files_{k}" in t], {1, 2})]
    s += ["<h2>Assays</h2>", table(
        ["assay", "assay file", "MAF", "technique", "polarity", "vendor", "instrument", "runs"],
        [[esc(a.assay), f"<code>{esc(a.file)}</code>", f"<code>{esc(a.maf)}</code>", esc(a.technique),
          esc(a.polarity), esc(a.vendor), esc(a.instrument), a.runs] for a in r.assays], {7})]
    with_run = not r.redacted
    s += ["<h2>Runs</h2>", table(
        (["run"] if with_run else []) + ["deposited mzML", "assay", "polarity", "raw id"],
        [([esc(x.run)] if with_run else []) + [f"<code>{esc(x.deposited_mzml)}</code>", esc(x.assay),
                                               esc(x.polarity), esc(x.raw_id)] for x in r.runs])]
    s += ["<h2>Raw data</h2>", table(["raw id", "packaging", "deposited"], [
        [esc(x.raw_id), esc(x.packaging), "<br>".join(f"<code>{esc(d)}</code>" for d in x.deposited)]
        for x in r.raw_files])]
    s += ["<h2>Files</h2>", table(["path", "kind", "bytes", "SHA-256"], [
        [f"<code>{esc(f.path)}</code>", esc(f.kind), f.bytes, f"<code>{esc(f.sha256)}</code>"] for f in r.files], {2})]
    s += ["<h2>Plan issues</h2>", table(["level", "code", "count"], [[esc(lv), esc(c), n] for lv, c, n in r.issues],
                                        {2})]
    s += ["<h2>Tools and validators</h2>", table(["tool", "version"], [[esc(t), esc(v)] for t, v in r.tools])]
    s += ["<h2>Parameters</h2>", table(["parameter", "value"], [[esc(k), esc(v)] for k, v in r.params])]
    return s


def render_public(r: PublicReport) -> str:
    return page(f"Deposit report {r.study}", public_sections(r))


def render_private(r: PublicReport, plan: dict, gate: dict | None, results: list[dict], details: list[dict],
                   mapping: list[dict], hits: list[dict], params: dict | None) -> str:
    """The private report: the full (standard-mode) public view plus details. Never published."""
    s = public_sections(r)
    s[0] = f"<h1>PRIVATE deposit report: {esc(r.study)}</h1>"
    s.append("<h2>Gate</h2>" + table(["reason"], [[esc(x)] for x in (gate or {}).get("reasons", [])
                                                   + (gate or {}).get("notes", [])]))
    s.append("<h2>Check summaries</h2>" + table(
        ["check", "run", "status", "summary"],
        [[esc(x.get("check")), esc(x.get("run") or ""), badge(_enum(x.get("status"), CHECK_STATUSES, "fail")),
          esc(x.get("summary", ""))] for x in results]))
    s.append("<h2>Check details</h2>")
    for d in details:
        s.append(f"<h3>{esc(d.get('check'))} {esc(d.get('run') or '')}</h3>"
                 f"<pre>{esc(json.dumps(d.get('details', []), indent=1, ensure_ascii=False))}</pre>")
    s.append("<h2>Plan runs</h2>" + table(
        ["run", "sample", "MS assay name", "deposited mzML", "raw id", "raw path", "source SHA-1"],
        [[esc(x.get(k, "")) for k in ("run", "sample", "ms_assay_name", "deposited_mzml", "raw_id", "raw_path",
                                      "source_sha1")] for x in plan.get("runs", [])]))
    s.append("<h2>Plan issues</h2>" + table(
        ["level", "code", "run", "message"],
        [[esc(i.get(k, "")) for k in ("level", "code", "run", "message")] for i in plan.get("issues", [])]))
    if mapping:
        keys = list(mapping[0])
        s.append("<h2>Mapping</h2>" + table(keys, [[esc(m.get(k, "")) for k in keys] for m in mapping]))
    if hits:
        keys = list(hits[0])
        s.append("<h2>Identifier hits</h2>" + table(keys, [[esc(h.get(k, "")) for k in keys] for h in hits]))
    if params:
        s.append("<h2>All parameters</h2>" + f"<pre>{esc(json.dumps(params, indent=1, ensure_ascii=False))}</pre>")
    return page(f"PRIVATE deposit report {r.study}", s)


# --- command --------------------------------------------------------------------------------------------------------

def load_checks(paths: list[Path], study: str) -> tuple[list[dict], list[dict]]:
    """(public check results, private details) of the study."""
    results, details = [], []
    for f in check_files(paths):
        r = read_json(f, "msdeposit/check/1")
        if r.get("study") != study:
            continue
        results.append(r)
        priv = f.with_name(f.name[: -len(".json")] + ".private.json")
        if priv.is_file():
            details.append(read_json(priv))
    results.sort(key=lambda r: (str(r.get("check")), str(r.get("run") or "")))
    return results, details


def run(args) -> int:
    if not args.public and not args.private:
        raise InputError("give --public and/or --private")
    plan = read_json(args.plan, "msdeposit/plan/1")
    if plan["study"] != args.study:
        raise InputError(f"plan is for study {plan['study']}, not {args.study}")
    gate = read_json(args.gate, "msdeposit/gate/1") if args.gate else None
    if gate is not None and gate.get("study") != args.study:
        raise InputError("gate.json is for another study")
    results, details = load_checks(args.checks, args.study)
    manifest = read_table(args.upload_manifest, "\t") if args.upload_manifest else []
    versions = load_versions(args.versions)
    params = read_json(args.params) if args.params else None
    sensitive = "sensitive" in (args.mode, plan.get("mode"), (gate or {}).get("mode"))
    mode = "sensitive" if sensitive else "standard"
    if args.public:
        report = build_public(args.study, plan, gate, results, manifest, versions, params, mode)
        args.public.parent.mkdir(parents=True, exist_ok=True)
        args.public.write_text(render_public(report), encoding="utf-8")
    if args.private:
        full = build_public(args.study, plan, gate, results, manifest, versions, params, mode, redact=False)
        mapping = read_table(args.mapping, "\t") if args.mapping else []
        hits = read_table(args.hits, "\t") if args.hits else []
        args.private.parent.mkdir(parents=True, exist_ok=True)
        args.private.write_text(render_private(full, plan, gate, results, details, mapping, hits, params),
                                encoding="utf-8")
    print(f"{args.study}: deposit report ({mode})")
    return 0
