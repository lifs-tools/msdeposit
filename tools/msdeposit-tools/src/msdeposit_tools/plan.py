"""deposit-plan (check C3): the per-study deposit plan, the central interface of the describe and finalise steps.

Inputs: study.json, the mzml-metadata JSON of every run, runs.normalised.csv (original names, private).
Outputs (CONTRACTS.md §3): plan.json (public), mapping.tsv (private), check_C3.json (+ .private.json).
"""

import re
from collections import Counter, defaultdict
from pathlib import Path

from .io import POL_TOKEN, InputError, as_bool, read_json, read_table, safe_name, write_check, write_json, write_table

DATA_EXT = (".mzml", ".raw", ".d", ".wiff2", ".wiff", ".lcd", ".txt", ".gz")
DIR_VENDORS = {"waters", "agilent", "bruker_tdf", "bruker_baf"}
MAPPING_FIELDS = ["run", "raw_id", "original_path", "original_name", "deposited_mzml", "source_sha1"]


def add_arguments(p):
    p.add_argument("--study-json", required=True, type=Path)
    p.add_argument("--metadata", required=True, nargs="+", type=Path,
                   help="metadata.json files or directories containing them")
    p.add_argument("--runs-normalised", required=True, type=Path)
    p.add_argument("--deposited-naming", choices=("original", "neutral"), default="original")
    p.add_argument("--deposit-raw", default="true")
    p.add_argument("--outdir", type=Path, default=Path("."))


def strip_data_ext(name: str) -> str:
    """'x.raw.mzML' -> 'x'; strips known data extensions repeatedly."""
    while True:
        low = name.lower()
        ext = next((e for e in DATA_EXT if low.endswith(e) and len(name) > len(e)), None)
        if ext is None:
            return name
        name = name[: -len(ext)]


def load_metadata(paths: list[Path]) -> dict[str, dict]:
    files = []
    for p in paths:
        files += sorted(p.rglob("*.json")) if p.is_dir() else [p]
    out = {}
    for f in files:
        d = read_json(f)
        if not str(d.get("schema", "")).startswith("msdeposit/mzml_metadata/"):
            continue
        read_json(f, "msdeposit/mzml_metadata/1")
        if d["run"] in out:
            raise InputError(f"metadata for run '{d['run']}' given twice")
        out[d["run"]] = d
    return out


def build(study: dict, metadata: dict[str, dict], originals: dict[str, dict], naming: str, deposit_raw: bool) -> tuple:
    s = study["study"]
    cfg = study["config"]
    prefix = safe_name(cfg.get("accession") or s)
    issues = []

    def issue(level, code, message, run=None):
        issues.append(dict(level=level, code=code, run=run, message=message))

    assays = {a["assay"]: a for a in study["assays"]}
    runs_by_path = Counter(originals[r["run"]]["path"] for r in study["runs"] if r["run"] in originals)

    # 1. per-run facts from the metadata
    planned = []
    for r in study["runs"]:
        meta = metadata.get(r["run"])
        orig = originals.get(r["run"], {})
        a = assays.get(r["assay"], {})
        if meta is None:
            issue("error", "not_converted", "no mzML metadata (conversion missing or failed)", r["run"])
            meta = {}
        elif meta.get("status") == "fail":
            issue("error", "conversion_check_failed",
                  "mzML failed the conversion check (C2): " + ", ".join(meta.get("issues", [])), r["run"])
        pol = meta.get("polarity") or r.get("polarity") or a.get("polarity", "")
        for given, what in ((r.get("polarity"), "run sheet"), (a.get("polarity"), "assay")):
            if given and meta.get("polarity") and given != meta["polarity"]:
                issue("error", "polarity_conflict", f"{what} polarity '{given}', mzML '{meta['polarity']}'", r["run"])
        if not pol:
            issue("warning", "polarity_unknown", "polarity unknown", r["run"])
        inst = meta.get("instrument", {})
        sf = (meta.get("source_files") or [{}])[0]
        orig_name = Path(orig.get("path", r["run"])).name
        planned.append(dict(
            run=r["run"], assay=r["assay"], sample=r["sample"], polarity=pol, vendor=r["vendor"],
            raw_id=r["raw_id"], run_index=r.get("run_index"), explicit=(r.get("deposited_name") or ""),
            instrument_model={"name": inst.get("model", ""), "accession": inst.get("model_accession", "")},
            nativeid_format=meta.get("nativeid_format") or {"accession": "", "name": ""},
            source_sha1=sf.get("sha1", ""), original_path=orig.get("path", ""), original_name=orig_name,
            multi=runs_by_path.get(orig.get("path"), 0) > 1))

    # 2. deposited names
    def generated(p):
        if naming == "neutral":
            acq = assays.get(p["assay"], {}).get("acquisition", "")
            parts = [p["sample"], POL_TOKEN.get(p["polarity"], "UNK")] + ([acq] if acq else [])
            return safe_name("_".join(parts))
        stem = safe_name(strip_data_ext(p["original_name"]))
        return f"{stem}_{p['run_index']}" if p["multi"] and p["run_index"] else stem

    explicit = Counter(strip_data_ext(p["explicit"]) for p in planned if p["explicit"])
    for name, n in explicit.items():
        if n > 1:
            issue("error", "duplicate_deposited_name", f"deposited_name '{name}' given for {n} runs")
    taken = set(explicit)
    for p in planned:
        if p["explicit"]:
            p["stem"] = strip_data_ext(p["explicit"])
            continue
        base = generated(p) or safe_name(p["run"])
        stem, k = base, 1
        while stem.lower() in {t.lower() for t in taken}:
            k += 1
            stem = f"{base}_{k}"
        if k > 1:
            issue("warning", "name_collision", f"deposited name '{base}' already used; using '{stem}'", p["run"])
        taken.add(stem)
        p["stem"] = stem

    # 3. assays: homogeneity, file names
    plan_assays = []
    for key, a in assays.items():
        members = [p for p in planned if p["assay"] == key]
        if not members:
            issue("warning", "empty_assay", f"assay '{key}' has no runs")
            continue
        vendors = {p["vendor"] for p in members}
        models = {p["instrument_model"]["accession"] or p["instrument_model"]["name"] for p in members} - {""}
        pols = {p["polarity"] for p in members} - {""}
        if len(vendors) > 1:
            issue("error", "inhomogeneous_assay", f"assay '{key}' mixes vendors {sorted(vendors)}")
        if len(models) > 1:
            issue("error", "inhomogeneous_assay", f"assay '{key}' mixes instrument models {sorted(models)}")
        if len(pols) > 1:
            issue("error", "inhomogeneous_assay", f"assay '{key}' mixes polarities {sorted(pols)}")
        model = next((p["instrument_model"] for p in members if p["instrument_model"]["name"]),
                     {"name": a.get("instrument_fallback", ""), "accession": ""})
        stem = safe_name(f"{prefix}_{key}")
        plan_assays.append(dict(assay=key, file=f"a_{stem}.txt", maf=f"m_{stem}_maf.tsv",
                                technique=a.get("technique", ""), polarity=next(iter(pols), a.get("polarity", "")),
                                acquisition=a.get("acquisition", ""), vendor=next(iter(sorted(vendors)), ""),
                                instrument_model=model, runs=[p["run"] for p in members]))
    files = Counter(x["file"] for x in plan_assays)
    for f, n in files.items():
        if n > 1:
            issue("error", "duplicate_assay_file", f"assay keys map to the same file name {f}")

    # 4. raw files
    raw_files, raw_main = [], {}
    by_raw = defaultdict(list)
    for p in planned:
        by_raw[p["raw_id"]].append(p)
    names_used = Counter(safe_name(ps[0]["original_name"]) for ps in by_raw.values())
    for rid in sorted(by_raw):
        ps = by_raw[rid]
        if not deposit_raw:
            raw_files.append(dict(raw_id=rid, packaging="none", deposited=[]))
            raw_main[rid] = ""
            continue
        orig = originals.get(ps[0]["run"], {})
        name = safe_name(ps[0]["original_name"])
        if names_used[name] > 1:
            issue("warning", "raw_name_collision", f"raw file name '{name}' used by several raw paths; "
                                                   f"prefixed with the raw id", ps[0]["run"])
            name = f"{rid}_{name}"
        directory = ps[0]["vendor"] in DIR_VENDORS or Path(orig.get("path", "")).suffix.lower() == ".d"
        if directory:
            deposited = [f"FILES/RAW_FILES/{name}.zip"]
        else:
            companions = [c for c in (orig.get("companions") or "").split(";") if c]
            deposited = [f"FILES/RAW_FILES/{name}"] + [
                f"FILES/RAW_FILES/{safe_name(Path(c).name)}" for c in companions]
        raw_files.append(dict(raw_id=rid, packaging="zip" if directory else "copy", deposited=deposited))
        raw_main[rid] = deposited[0]
    dep_raw = Counter(d for x in raw_files for d in x["deposited"])
    for d, n in dep_raw.items():
        if n > 1:
            issue("error", "duplicate_raw_name", f"deposited raw file name {Path(d).name} used {n} times")

    runs = [dict(run=p["run"], assay=p["assay"], sample=p["sample"], ms_assay_name=p["stem"],
                 deposited_mzml=f"{p['stem']}.mzML", derived_path=f"FILES/DERIVED_FILES/{p['stem']}.mzML",
                 raw_id=p["raw_id"], raw_path=raw_main.get(p["raw_id"], ""), polarity=p["polarity"],
                 vendor=p["vendor"], nativeid_format=p["nativeid_format"], source_sha1=p["source_sha1"])
            for p in planned]
    samples = {x["sample_name"] for x in study["samples"]}
    orphans = samples - {p["sample"] for p in planned}
    if orphans:
        issue("warning", "samples_without_runs", f"{len(orphans)} samples have no runs")
    status = "fail" if any(i["level"] == "error" for i in issues) else "pass"
    plan = dict(schema="msdeposit/plan/1", study=s, accession=cfg.get("accession"), prefix=prefix,
                mode=study.get("mode", "standard"), deposited_naming=naming, deposit_raw=deposit_raw,
                investigation_file="i_Investigation.txt", sample_file=f"s_{prefix}.txt", assays=plan_assays,
                runs=runs, raw_files=raw_files, issues=issues, status=status)
    mapping = [dict(run=p["run"], raw_id=p["raw_id"], original_path=p["original_path"],
                    original_name=p["original_name"], deposited_mzml=f"{p['stem']}.mzML", source_sha1=p["source_sha1"])
               for p in planned]
    return plan, mapping


def run(args) -> int:
    study = read_json(args.study_json, "msdeposit/study/1")
    metadata = load_metadata(args.metadata)
    originals = {r["run"]: r for r in read_table(args.runs_normalised, ",") if r["study"] == study["study"]}
    deposit_raw = as_bool(args.deposit_raw)
    if study.get("mode") == "sensitive" and (deposit_raw or args.deposited_naming != "neutral"):
        raise InputError("sensitive mode requires --deposit-raw false and --deposited-naming neutral")
    # issue messages are built from run keys, sample names and metadata only, never from paths
    plan, mapping = build(study, metadata, originals, args.deposited_naming, deposit_raw)
    out = args.outdir
    write_json(out / "plan.json", plan)
    write_table(out / "mapping.tsv", mapping, MAPPING_FIELDS)
    errors = [i for i in plan["issues"] if i["level"] == "error"]
    warnings = [i for i in plan["issues"] if i["level"] == "warning"]
    write_check(out, plan["study"], "C3", plan["status"] if not warnings or errors else "warn",
                counts=dict(runs=len(plan["runs"]), assays=len(plan["assays"]), errors=len(errors),
                            warnings=len(warnings)),
                summary=f"{len(plan['runs'])} runs in {len(plan['assays'])} assays; "
                        f"{len(errors)} errors, {len(warnings)} warnings",
                details=plan["issues"])
    return 0
