"""Synthetic inputs for the ISA writer and the MetaboLights validation tests (no real study data)."""

import argparse
import json
from pathlib import Path

from conftest import fake_metadata
from synthetic import PLANTED

from msdeposit_tools import plan as plan_mod
from msdeposit_tools.describe import isa
from msdeposit_tools.io import read_table, write_json

RUN_POLARITY = {"r001": "positive", "r002": "positive", "r003": "negative", "r004": "negative"}


def rich_metadata(run: str, polarity: str = "positive", start: str = "2024-03-05T09:41:17Z", srm: int = 0) -> dict:
    """fake_metadata with instrument components, software and planted private fields (serial, source file,
    run info) that the ISA writer must never copy."""
    m = fake_metadata(run, polarity)
    m["instrument"]["serial"] = PLANTED["serial"]
    m["instrument"]["configurations"] = [{"id": "IC1", "components": [
        {"type": "source", "order": 1, "terms": [["MS:1000073", "electrospray ionization"]]},
        {"type": "analyzer", "order": 2, "terms": [["MS:1000081", "quadrupole"]]},
        {"type": "analyzer", "order": 3, "terms": [["MS:1000484", "orbitrap"]]},
        {"type": "detector", "order": 4, "terms": [["MS:1000624", "inductive detector"]]}]}]
    m["software"] = [{"id": "Xcalibur", "version": "4.1.31.9", "terms": [["MS:1000532", "Xcalibur"]]},
                     {"id": "pwiz", "version": "3.0.26121", "terms": [["MS:1000615", "ProteoWizard software"]]}]
    m["source_files"] = [{"id": PLANTED["raw_name"] + ".raw", "name": PLANTED["raw_name"] + ".raw",
                          "location": "file:///" + PLANTED["path"].replace("\\", "/"), "sha1": "cd" * 20,
                          "format": ["MS:1000563", "Thermo RAW format"]}]
    m["run_info"] = {"id": PLANTED["raw_name"], "start": start, "sample_ref": PLANTED["initials"],
                     "default_instrument_configuration_ref": "IC1"}
    m["counts"].update(centroid=6, ms2=0)
    m["chromatograms"]["srm"] = srm
    return m


def make_plan(validated: Path, tmp_path: Path, naming: str = "original", deposit_raw: bool = True,
              edit_study=None, metas: list[dict] | None = None) -> tuple[Path, Path, Path]:
    """study.json, plan.json and a metadata directory of the example study (validate-inputs output)."""
    study_path = validated / "S1" / "study.json"
    study = json.loads(study_path.read_text())
    if edit_study:
        edit_study(study)
        study_path.write_text(json.dumps(study))
    metas = metas or [rich_metadata(r, p) for r, p in RUN_POLARITY.items()]
    mdir = tmp_path / "meta"
    for m in metas:
        write_json(mdir / f"{m['run']}.json", m)
    originals = {r["run"]: r for r in read_table(validated / "runs.normalised.csv", ",") if r["study"] == "S1"}
    plan, _ = plan_mod.build(study, {m["run"]: m for m in metas}, originals, naming, deposit_raw)
    assert plan["status"] == "pass", plan["issues"]
    plan_path = tmp_path / "plan" / "plan.json"
    write_json(plan_path, plan)
    return study_path, plan_path, mdir


def isa_args(study_path: Path, plan_path: Path, mdir: Path, outdir: Path, **extra) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    isa.add_arguments(p)
    argv = ["--study-json", str(study_path), "--plan", str(plan_path), "--metadata", str(mdir),
            "--outdir", str(outdir), "--created-at", "2026-01-01"]
    for k, v in extra.items():
        argv += [f"--{k.replace('_', '-')}"] + ([str(x) for x in v] if isinstance(v, list) else [str(v)])
    return p.parse_args(argv)


def read_isa_table(path: Path) -> tuple[list[str], list[list[str]]]:
    import csv
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh, delimiter="\t"))
    return rows[0], rows[1:]


def column(path: Path, header: str, nth: int = 0) -> list[str]:
    hdr, rows = read_isa_table(path)
    i = [k for k, h in enumerate(hdr) if h == header][nth]
    return [r[i] for r in rows]


MAF_COLUMNS = ["database_identifier", "chemical_formula", "smiles", "inchi", "metabolite_identification",
               "mass_to_charge", "fragmentation", "modifications", "charge", "retention_time", "taxid", "species",
               "database", "database_version", "reliability", "uri", "search_engine", "search_engine_score",
               "smallmolecule_abundance_sub", "smallmolecule_abundance_stdev_sub",
               "smallmolecule_abundance_std_error_sub"]


def assemble_stub(isa_dir: Path, plan_path: Path, root: Path) -> Path:
    """Minimal upload set: ISA files at the root, small non-empty stub files at the derived/raw paths and a
    one-row MAF per assay (MetaboLights MS columns + one column per MS Assay Name)."""
    import csv
    import shutil
    plan = json.loads(plan_path.read_text())
    root.mkdir(parents=True, exist_ok=True)
    for f in isa_dir.iterdir():
        shutil.copy(f, root / f.name)
    runs = {r["run"]: r for r in plan["runs"]}
    for r in plan["runs"]:
        for rel in (r["derived_path"], r["raw_path"]):
            if rel:
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_bytes(b"stub\n")
    for a in plan["assays"]:
        names = [runs[r]["ms_assay_name"] for r in a["runs"]]
        rows = [{c: "" for c in MAF_COLUMNS} | {"metabolite_identification": name, "chemical_formula": formula,
                                                "mass_to_charge": mz, "charge": "1", "retention_time": rt,
                                                "reliability": "2"} | {n: "1.0" for n in names}
                for name, formula, mz, rt in (("PC 34:1", "C42H82NO8P", "760.5851", "5.2"),
                                              ("PC 36:2", "C44H84NO8P", "786.6007", "5.6"))]
        with open(root / a["maf"], "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=MAF_COLUMNS + names, delimiter="\t", quoting=csv.QUOTE_ALL,
                               lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
    return root


def complete_study(study: dict) -> None:
    """Fill the example study with what MetaboLights requires beyond the minimal example inputs (synthetic text)."""
    cfg = study["config"]
    cfg["accession"] = "MTBLS9999"
    cfg["description"] = ("Synthetic test study used to check the ISA-Tab writer of msdeposit-tools. Two assays in "
                          "positive and negative ionisation mode measure a reference plasma and a test plasma; "
                          "all values are invented for software testing.")
    cfg["design_descriptors"] = [{"term": "lipidomics", "source": "EDAM", "accession": "EDAM:topic_0153"}]
    cfg["factors"] = [{"name": "Sample material", "type": {"term": "reference material", "source": "CHMO",
                                                           "accession": "CHMO:0002913"}}]
    cfg["publication"] = {"title": "A synthetic test publication", "author_list": ["A. Example", "B. Example"],
                          "status": "in preparation"}
    for a in study["assays"]:
        a["injection_system"] = "Example UHPLC system"
    for s in study["samples"]:
        for c in s["characteristics"]:
            if c["name"] == "Organism part":
                c.update(source="BTO", accession="BTO:0000131")
        s["characteristics"].append({"name": "Disease", "value": "normal", "source": "PATO",
                                     "accession": "PATO:0000461", "unit": ""})
        s["characteristics"].append({"name": "Sample type", "value": "experimental sample", "source": "CHMO",
                                     "accession": "CHMO:0002746", "unit": ""})


PROTOCOL_TEXTS = {
    "sample_collection": "Synthetic plasma samples were defined for software testing only; no material was collected.",
    "extraction": "Lipids were extracted with a synthetic two-phase protocol described for software testing only.",
    "chromatography": "Reversed phase gradient separation on a C18 column, described for software testing only.",
    "mass_spectrometry": "Full scan acquisition in positive and negative mode, described for software testing only.",
    "metabolite_identification": "Lipids were annotated at species level by accurate mass for software testing.",
}


def write_protocols(directory: Path, texts: dict | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for key, text in (texts or PROTOCOL_TEXTS).items():
        (directory / f"{key}.md").write_text(text + "\n")
    return directory


def write_isa(validated: Path, tmp_path: Path, *, naming: str = "original", deposit_raw: bool = True,
              edit_study=None, metas: list[dict] | None = None, protocols: bool = True, edit_plan=None,
              **opts) -> tuple[Path, dict]:
    """Plan + isa run on the example study; returns (ISA output dir, plan)."""
    study_path, plan_path, mdir = make_plan(validated, tmp_path, naming, deposit_raw, edit_study, metas)
    if edit_plan:
        plan = json.loads(plan_path.read_text())
        edit_plan(plan)
        plan_path.write_text(json.dumps(plan))
    if protocols:
        opts.setdefault("protocols", write_protocols(tmp_path / "protocols"))
    out = tmp_path / "isa"
    assert isa.run(isa_args(study_path, plan_path, mdir, out, **opts)) == 0
    return out, json.loads(plan_path.read_text())
