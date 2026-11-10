"""mzqc: one mzQC 1.0 file per study with a runQuality (identification-free, run-level metrics) per deposited mzML.

Metrics come from the public fields of the mzml-metadata JSON (`counts`, `chromatograms`, `mz_window`, `metrics`,
`injection_time_mean_ms`). Input files are the deposited mzML: location relative to the MetaboLights study root
(`file:///FILES/DERIVED_FILES/<name>`), checksum = SHA-1 from finalise.json. Label = plan `ms_assay_name`,
batch label = assay key. Runs without metadata are counted in the description and left out.
CV terms carry no `description` (semantic validators compare it with the CV definition) and empty
`fileProperties` are omitted. `creationDate` honours SOURCE_DATE_EPOCH for reproducible output.

Output (CONTRACTS.md §6): <prefix>.mzQC
"""

import os
from datetime import datetime, timezone
from pathlib import Path

from .. import __version__
from ..io import InputError, load_finalise, read_json, write_json
from ..io import load_metadata

LOCATION_ROOT = "file:///"
MZQC_VERSION = "1.0.0"
SOFTWARE = {"accession": "MS:1000799", "name": "custom unreleased software tool", "value": "msdeposit-tools",
            "version": __version__, "uri": "https://github.com/lifs-tools/msdeposit"}
# PSI-MS imports the UO unit terms used here; listing UO separately makes them ambiguous for validators
CVS = [{"name": "Proteomics Standards Initiative Mass Spectrometry Ontology",
        "uri": "https://github.com/HUPO-PSI/psi-ms-CV/releases/download/v4.2.2/psi-ms.obo", "version": "4.2.2"}]
SECOND = {"accession": "UO:0000010", "name": "second"}
COUNT = {"accession": "UO:0000189", "name": "count unit"}
HERTZ = {"accession": "UO:0000106", "name": "hertz"}
MILLISECOND = {"accession": "UO:0000028", "name": "millisecond"}
MZ = {"accession": "MS:1000040", "name": "m/z"}


def add_arguments(p):
    p.add_argument("--study-json", required=True, type=Path)
    p.add_argument("--plan", required=True, type=Path)
    p.add_argument("--metadata", required=True, nargs="+", type=Path, help="metadata.json files or directories")
    p.add_argument("--finalise", nargs="*", type=Path, default=[], help="finalise.json files or directories")
    p.add_argument("--outdir", type=Path, default=Path("."))


def _metric(acc: str, name: str, value, unit: dict | None = None) -> dict:
    d = {"accession": acc, "name": name, "value": value}
    if unit:
        d["unit"] = unit
    return d


def _num(v, digits: int | None = None):
    if isinstance(v, float) and digits is not None:
        return round(v, digits)
    return v


def run_metrics(meta: dict) -> list[dict]:
    c, mt, ch = meta.get("counts") or {}, meta.get("metrics") or {}, meta.get("chromatograms") or {}
    out = []
    if c.get("spectra"):
        out.append(_metric("MS:4000059", "number of MS1 spectra", c.get("ms1", 0), COUNT))
        out.append(_metric("MS:4000060", "number of MS2 spectra", c.get("ms2", 0), COUNT))
    out.append(_metric("MS:4000071", "number of chromatograms", ch.get("n", 0), COUNT))
    if mt.get("rt_range_s"):
        out.append(_metric("MS:4000070", "retention time acquisition range", [_num(x, 3) for x in mt["rt_range_s"]],
                           SECOND))
    if mt.get("ms_run_duration_s") is not None:
        out.append(_metric("MS:4000067", "MS run duration", _num(mt["ms_run_duration_s"], 3), SECOND))
    if meta.get("mz_window"):
        out.append(_metric("MS:4000069", "m/z acquisition range", [_num(x, 4) for x in meta["mz_window"]], MZ))
    if not c.get("spectra"):
        return out
    if mt.get("tic_auc_ms1") is not None:
        out.append(_metric("MS:4000029", "area under TIC in MS1", mt["tic_auc_ms1"]))
    if c.get("ms2") and mt.get("tic_auc_ms2") is not None:
        out.append(_metric("MS:4000030", "area under TIC in MS2", mt["tic_auc_ms2"]))
    for key, acc, name in (("ms1_freq_fastest_hz", "MS:4000065", "fastest frequency for MS level 1 collection"),
                           ("ms1_freq_slowest_hz", "MS:4000095", "slowest frequency for MS level 1 collection")):
        if mt.get(key) is not None:
            out.append(_metric(acc, name, mt[key], HERTZ))
    if c.get("ms1"):
        for key, acc, name in (("ms1_jump10", "MS:4000097", "MS1 signal jump (10x) count"),
                               ("ms1_fall10", "MS:4000098", "MS1 signal fall (10x) count"),
                               ("empty_ms1", "MS:4000099", "number of empty MS1 scans")):
            if mt.get(key) is not None:
                out.append(_metric(acc, name, mt[key], COUNT))
    if c.get("ms2") and mt.get("empty_ms2") is not None:
        out.append(_metric("MS:4000100", "number of empty MS2 scans", mt["empty_ms2"], COUNT))
    it = {str(k): v for k, v in (meta.get("injection_time_mean_ms") or {}).items()}
    if it.get("1"):
        out.append(_metric("MS:4000132", "MS1 ion collection time mean", it["1"], MILLISECOND))
    if it.get("2"):
        out.append(_metric("MS:4000137", "MS2 ion collection time mean", it["2"], MILLISECOND))
    return out


def run_quality(run: dict, meta: dict, fin: dict | None) -> dict:
    props = [{"accession": "MS:1000569", "name": "SHA-1", "value": fin["sha1"]}] if fin and fin.get("sha1") else []
    inst = meta.get("instrument") or {}
    if inst.get("model_accession") and inst.get("model") and "instrument model" not in inst["model"]:
        props.append({"accession": inst["model_accession"], "name": inst["model"]})
    input_file = {"location": LOCATION_ROOT + run["derived_path"], "name": run["deposited_mzml"],
                  "fileFormat": {"accession": "MS:1000584", "name": "mzML format"}}
    if props:
        input_file["fileProperties"] = props
    return {"metadata": {"label": run["ms_assay_name"], "inputFiles": [input_file],
                         "analysisSoftware": [SOFTWARE],
                         "cvParameters": [{"accession": "MS:4000088", "name": "batch label", "value": run["assay"]}]},
            "qualityMetrics": run_metrics(meta)}


def creation_date() -> str:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    when = datetime.fromtimestamp(int(epoch), timezone.utc) if epoch else datetime.now(timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def build(study: dict, plan: dict, metadata: dict, finalise: dict) -> tuple[dict, int]:
    runs, missing = [], 0
    for r in plan["runs"]:
        meta = metadata.get(r["run"])
        if not meta:
            missing += 1
            continue
        runs.append(run_quality(r, meta, finalise.get(r["run"])))
    if not runs:
        raise InputError("mzQC: no run has mzML metadata")
    contact = (study.get("contacts") or [{}])[0]
    doc = {"version": MZQC_VERSION, "creationDate": creation_date()}
    name = " ".join(x for x in (contact.get("first_name"), contact.get("last_name")) if x)
    if name:
        doc["contactName"] = name
    if contact.get("email"):
        doc["contactAddress"] = contact["email"]
    title = study.get("config", {}).get("title") or plan["study"]
    doc["description"] = (
        f"Run-level, identification-free QC metrics of the mzML files deposited for '{title}'. Labels are the ISA "
        f"'MS Assay Name'; batch label = assay. Input file locations are relative to the MetaboLights study root. "
        f"Retention time metrics of chromatogram-only runs come from the TIC chromatogram; m/z acquisition range = "
        f"lowest/highest scan window limit; TIC areas are trapezoidal integrals over retention time in seconds."
        + (f" {missing} runs without metadata are not included." if missing else ""))
    doc["runQualities"] = runs
    doc["controlledVocabularies"] = CVS
    return {"mzQC": doc}, missing


def run(args) -> int:
    study = read_json(args.study_json, "msdeposit/study/1")
    plan = read_json(args.plan, "msdeposit/plan/1")
    doc, missing = build(study, plan, load_metadata(args.metadata), load_finalise(args.finalise))
    path = args.outdir / f"{plan['prefix']}.mzQC"
    write_json(path, doc)
    print(f"{path.name}: {len(doc['mzQC']['runQualities'])} runs, {missing} without metadata")
    return 0
