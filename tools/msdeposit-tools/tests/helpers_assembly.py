"""Synthetic plans and described files for the assemble/check/gate/report tests (no real study data)."""

import json
from pathlib import Path

from synthetic import PLANTED

ASSAY_HEADER = ["Sample Name", "Protocol REF", "Parameter Value[Chromatography Instrument]",
                "Parameter Value[Column model]", "Parameter Value[Column type]", "Protocol REF",
                "Parameter Value[Scan polarity]", "Parameter Value[Scan m/z range]", "Parameter Value[Instrument]",
                "MS Assay Name", "Raw Spectral Data File", "Protocol REF", "Derived Spectral Data File",
                "Protocol REF", "Metabolite Assignment File"]
SAMPLE_HEADER = ["Source Name", "Characteristics[Organism]", "Characteristics[Organism part]", "Protocol REF",
                 "Sample Name"]
MAF_HEADER = ["database_identifier", "chemical_formula", "metabolite_identification", "mass_to_charge",
              "retention_time"]


def make_plan(study="S1", mode="standard", deposit_raw=True, run_keys=("r001", "r002")) -> dict:
    """A plan.json (CONTRACTS.md §3) with one LC-MS assay and two runs: a SCIEX file with companion, a directory."""
    neutral = mode == "sensitive"
    stems = ["QC_01_POS", "P1_A_POS"] if neutral else ["QC_01", "P1_A"]
    raw_files = [dict(raw_id=f"{study}_RAW_001", packaging="copy",
                      deposited=["FILES/RAW_FILES/QC_01.wiff", "FILES/RAW_FILES/QC_01.wiff.scan"]),
                 dict(raw_id=f"{study}_RAW_002", packaging="zip", deposited=["FILES/RAW_FILES/P1_A.d.zip"])]
    if not deposit_raw:
        raw_files = [dict(raw_id=x["raw_id"], packaging="none", deposited=[]) for x in raw_files]
    runs = []
    for i, (run, sample, stem) in enumerate(zip(run_keys, ["QC_01", "P1_A"], stems)):
        runs.append(dict(run=run, assay="POS", sample=sample, ms_assay_name=stem, deposited_mzml=f"{stem}.mzML",
                         derived_path=f"FILES/DERIVED_FILES/{stem}.mzML", raw_id=raw_files[i]["raw_id"],
                         raw_path=raw_files[i]["deposited"][0] if deposit_raw else "", polarity="positive",
                         vendor="sciex", nativeid_format={"accession": "MS:1000770", "name": "WIFF nativeID format"},
                         source_sha1="ab" * 20))
    return {
        "schema": "msdeposit/plan/1", "study": study, "accession": None, "prefix": study, "mode": mode,
        "deposited_naming": "neutral" if neutral else "original", "deposit_raw": deposit_raw,
        "investigation_file": "i_Investigation.txt", "sample_file": f"s_{study}.txt",
        "assays": [{"assay": "POS", "file": f"a_{study}_POS.txt", "maf": f"m_{study}_POS_maf.tsv",
                    "technique": "LC-MS", "polarity": "positive", "acquisition": "", "vendor": "sciex",
                    "instrument_model": {"name": "Triple Quad 6500", "accession": "MS:1002594"},
                    "runs": list(run_keys)}],
        "runs": runs, "raw_files": raw_files, "issues": [], "status": "pass"}


def write_plan(path: Path, plan: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=1))
    return path


def _tsv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join("\t".join(f'"{c}"' for c in r) for r in [header] + rows) + "\n")


def write_described(root: Path, plan: dict, *, todo: bool = False, maf_rt: bool = True) -> dict[str, Path]:
    """ISA, MAF, mzTab, mzQC, finalised mzML and packaged raw files for `plan` under root; returns the inputs."""
    root = Path(root)
    isa, maf, mztab, mzqc, mzml, raw = (root / d for d in ("isa", "maf", "mztab", "mzqc", "mzml", "raw"))
    a = plan["assays"][0]
    note = "[[TODO-PROTOCOL:extraction]]" if todo else "Lipids were extracted."
    isa.mkdir(parents=True, exist_ok=True)
    protocols = f'"{note}"' + ('\t"[[TODO-PROTOCOL:chromatography]]"' if todo else "")
    (isa / "i_Investigation.txt").write_text(
        f'STUDY\nStudy Identifier\t"{plan["prefix"]}"\nStudy File Name\t"{plan["sample_file"]}"\n'
        f'STUDY ASSAYS\nStudy Assay File Name\t"{a["file"]}"\n'
        f"STUDY PROTOCOLS\nStudy Protocol Description\t{protocols}\n")
    _tsv(isa / plan["sample_file"], SAMPLE_HEADER,
         [["src1", "Homo sapiens", "blood plasma", "Sample collection", r["sample"]] for r in plan["runs"]])
    _tsv(isa / a["file"], ASSAY_HEADER,
         [[r["sample"], "Extraction", "LC 1", "C18", "reverse phase", "Mass spectrometry", "positive", "100-1200",
           "Triple Quad 6500", r["ms_assay_name"], r["raw_path"], "Data transformation", r["derived_path"],
           "Metabolite identification", a["maf"]] for r in plan["runs"]])
    _tsv(maf / a["maf"], MAF_HEADER, [["", "C42H82NO8P", "PC 34:1", "760.5851", "5.2" if maf_rt else ""],
                                      ["", "C41H78NO8P", "PE 36:2", "", "5.9"]])
    mztab.mkdir(parents=True, exist_ok=True)
    (mztab / f"{plan['prefix']}.mztab-2.0.0-M.mzTab").write_text("MTD\tmzTab-version\t2.0.0-M\nMTD\tmzTab-ID\tS1\n")
    mzqc.mkdir(parents=True, exist_ok=True)
    (mzqc / f"{plan['prefix']}.mzQC").write_text('{"mzQC": {"version": "1.0.0"}}\n')
    mzml.mkdir(parents=True, exist_ok=True)
    for i, r in enumerate(plan["runs"]):
        (mzml / r["deposited_mzml"]).write_text(f"<indexedmzML>run {i} {'x' * 100}</indexedmzML>\n")
    inputs = dict(isa=isa, maf=maf, mztab=mztab, mzqc=mzqc, mzml=[mzml / r["deposited_mzml"] for r in plan["runs"]],
                  raw=[])
    if plan["deposit_raw"]:
        raw.mkdir(parents=True, exist_ok=True)
        for x in plan["raw_files"]:
            for d in x["deposited"]:
                (raw / Path(d).name).write_bytes(f"vendor bytes of {Path(d).name}".encode() * 10)
                inputs["raw"].append(raw / Path(d).name)
    return inputs


def assemble_argv(inputs: dict, plan_path: Path, outdir: Path, report: Path | None = None) -> list[str]:
    argv = ["assemble", "--plan", str(plan_path), "--outdir", str(outdir)]
    for k in ("isa", "maf", "mztab", "mzqc"):
        if inputs.get(k):
            argv += [f"--{k}", str(inputs[k])]
    if inputs.get("mzml"):
        argv += ["--mzml", *map(str, inputs["mzml"])]
    if inputs.get("raw"):
        argv += ["--raw", *map(str, inputs["raw"])]
    if report:
        argv += ["--report", str(report)]
    return argv


def check_json(outdir: Path, check: str, run: str | None = None) -> dict:
    return json.loads((Path(outdir) / f"check_{check}{'.' + run if run else ''}.json").read_text())


TOKENS = [PLANTED["operator"], PLANTED["initials"], PLANTED["path"], PLANTED["raw_name"], PLANTED["serial"],
          PLANTED["email"], PLANTED["lab"]]
