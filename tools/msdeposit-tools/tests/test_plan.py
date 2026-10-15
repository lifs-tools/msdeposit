import argparse
import csv
import json

from conftest import fake_metadata
from msdeposit_tools import plan
import pytest

from msdeposit_tools.io import InputError, write_json


def run_plan(validated, tmp_path, metas, naming="original", deposit_raw="true", edit_study=None):
    mdir = tmp_path / "meta"
    for m in metas:
        write_json(mdir / f"{m['run']}.json", m)
    study_path = validated / "S1" / "study.json"
    if edit_study:
        s = json.loads(study_path.read_text())
        edit_study(s)
        study_path.write_text(json.dumps(s))
    p = argparse.ArgumentParser()
    plan.add_arguments(p)
    out = tmp_path / "plan"
    args = p.parse_args(["--study-json", str(study_path), "--metadata", str(mdir), "--runs-normalised",
                         str(validated / "runs.normalised.csv"), "--deposited-naming", naming,
                         "--deposit-raw", deposit_raw, "--outdir", str(out)])
    rc = plan.run(args)
    return rc, (json.loads((out / "plan.json").read_text()) if rc == 0 else None), out


def metas_ok():
    return [fake_metadata("r001"), fake_metadata("r002"), fake_metadata("r003", "negative"),
            fake_metadata("r004", "negative")]


def test_plan_original_names(validated, tmp_path):
    rc, p, out = run_plan(validated, tmp_path, metas_ok())
    assert rc == 0 and p["status"] == "pass", p["issues"]
    assert p["prefix"] == "S1" and p["sample_file"] == "s_S1.txt"
    assert [a["file"] for a in p["assays"]] == ["a_S1_POS.txt", "a_S1_NEG.txt"]
    assert p["runs"][0]["deposited_mzml"] == "HLB_qmf_plasma_007_01.mzML"
    assert p["runs"][0]["derived_path"] == "FILES/DERIVED_FILES/HLB_qmf_plasma_007_01.mzML"
    assert p["raw_files"][0]["packaging"] == "copy"
    assert p["assays"][0]["instrument_model"]["accession"] == "MS:1001911"
    rows = list(csv.DictReader(open(out / "mapping.tsv"), delimiter="\t"))
    assert rows[0]["original_path"].startswith("/")
    assert "/" not in json.dumps(p["runs"]).replace("FILES/", "")


def test_plan_neutral_names_and_collisions(validated, tmp_path):
    def dup(s):
        s["runs"][1]["sample"] = "QC_01"   # two positive runs of QC_01
    rc, p, _ = run_plan(validated, tmp_path, metas_ok(), naming="neutral", deposit_raw="false", edit_study=dup)
    names = [r["deposited_mzml"] for r in p["runs"]]
    assert names == ["QC_01_POS.mzML", "QC_01_POS_2.mzML", "QC_01_NEG.mzML", "P1_A_NEG.mzML"]
    assert p["status"] == "pass" and any(i["code"] == "name_collision" for i in p["issues"])
    assert all(r["raw_path"] == "" for r in p["runs"]) and p["raw_files"][0]["packaging"] == "none"


def test_plan_polarity_conflict_and_inhomogeneity(validated, tmp_path):
    metas = metas_ok()
    metas[0]["polarity"] = "negative"
    metas[1]["instrument"]["model_accession"] = "MS:1002523"
    rc, p, out = run_plan(validated, tmp_path, metas)
    codes = {i["code"] for i in p["issues"]}
    assert p["status"] == "fail" and {"polarity_conflict", "inhomogeneous_assay"} <= codes
    assert json.loads((out / "check_C3.json").read_text())["status"] == "fail"


def test_plan_missing_metadata(validated, tmp_path):
    rc, p, _ = run_plan(validated, tmp_path, metas_ok()[:3])
    assert p["status"] == "fail" and any(i["code"] == "not_converted" and i["run"] == "r004" for i in p["issues"])


def test_plan_accession_prefix(validated, tmp_path):
    def acc(s):
        s["config"]["accession"] = "MTBLS9999"
    rc, p, _ = run_plan(validated, tmp_path, metas_ok(), edit_study=acc)
    assert p["sample_file"] == "s_MTBLS9999.txt" and p["assays"][0]["maf"] == "m_MTBLS9999_POS_maf.tsv"
    assert not p["runs"][0]["deposited_mzml"].startswith("MTBLS")


def test_sensitive_requires_safe_switches(validated, tmp_path):
    def sens(s):
        s["mode"] = "sensitive"
    with pytest.raises(InputError):
        run_plan(validated, tmp_path, metas_ok(), naming="original", deposit_raw="false", edit_study=sens)
