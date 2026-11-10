import json

import pytest

from helpers_results import describe_inputs, parse_args
from msdeposit_tools import validate_mzqc
from msdeposit_tools.describe import mzqc
from msdeposit_tools.io import InputError, write_json


def run_mzqc(validated, tmp_path, monkeypatch, finalise=True):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1767225600")
    d = describe_inputs(validated, tmp_path)
    out = tmp_path / "qc"
    argv = ["--study-json", d["study"], "--plan", d["plan"], "--metadata", d["metadata"], "--outdir", out]
    argv += ["--finalise", d["finalise"]] if finalise else []
    assert mzqc.run(parse_args(mzqc, argv)) == 0
    return d, out / "S1.mzQC"


def cv_terms(obj):
    """All CV parameter objects (dicts with an accession) in a JSON structure."""
    if isinstance(obj, dict):
        if "accession" in obj:
            yield obj
        for v in obj.values():
            yield from cv_terms(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from cv_terms(v)


def test_run_qualities(validated, tmp_path, monkeypatch):
    d, path = run_mzqc(validated, tmp_path, monkeypatch)
    doc = json.loads(path.read_text())["mzQC"]
    assert doc["version"] == "1.0.0" and doc["creationDate"] == "2026-01-01T00:00:00Z"
    assert doc["contactName"] == "Ada Example"
    plan = d["plan_data"]
    assert [q["metadata"]["label"] for q in doc["runQualities"]] == [r["ms_assay_name"] for r in plan["runs"]]
    for q, r in zip(doc["runQualities"], plan["runs"]):
        (f,) = q["metadata"]["inputFiles"]
        assert f["name"] == r["deposited_mzml"] and f["location"] == "file:///" + r["derived_path"]
        sha1 = json.loads((d["finalise"] / f"{r['run']}.finalise.json").read_text())["sha1"]
        assert {"accession": "MS:1000569", "name": "SHA-1", "value": sha1} in f["fileProperties"]
        assert q["metadata"]["cvParameters"] == [{"accession": "MS:4000088", "name": "batch label",
                                                  "value": r["assay"]}]
        metrics = {m["accession"]: m for m in q["qualityMetrics"]}
        assert metrics["MS:4000059"]["value"] == 6 and metrics["MS:4000070"]["value"] == [6.0, 36.0]
        assert metrics["MS:4000132"]["unit"]["accession"] == "UO:0000028"
        assert "MS:4000100" not in metrics and "MS:4000030" not in metrics     # no MS2 spectra
    assert not [t for t in cv_terms(doc) if "description" in t]
    text = path.read_text()
    assert str(tmp_path) not in text and "SN1" not in text and "x.raw" not in text


def test_validates_offline(validated, tmp_path, monkeypatch):
    monkeypatch.delenv("MSDEPOSIT_TOOLS_DIR", raising=False)
    _, path = run_mzqc(validated, tmp_path, monkeypatch)
    res = validate_mzqc.validate(path)
    assert res["available"] and res["tool"].startswith("pymzqc")
    assert res["errors"] == 0 and res["warnings"] == 0, res["messages"]
    assert all(m["level"] == "info" for m in res["messages"])
    assert len(res["messages"]) == len({m["message"] for m in res["messages"]})


def test_validator_reports_schema_and_semantic_errors(validated, tmp_path, monkeypatch):
    _, path = run_mzqc(validated, tmp_path, monkeypatch)
    doc = json.loads(path.read_text())
    del doc["mzQC"]["runQualities"][0]["metadata"]["inputFiles"][0]["fileFormat"]
    bad = tmp_path / "bad.mzQC"
    write_json(bad, doc)
    res = validate_mzqc.validate(bad)
    assert res["errors"] == 1 and res["messages"][0]["rule"] == "mzqc_schema"
    doc = json.loads(path.read_text())
    doc["mzQC"]["runQualities"][0]["qualityMetrics"][0]["name"] = "number of MS2 spectra"
    write_json(bad, doc)
    res = validate_mzqc.validate(bad)
    assert res["errors"] >= 1 and any("name conflict" in m["message"] for m in res["messages"])
    bad.write_text("{not json")
    assert validate_mzqc.validate(bad)["messages"][0]["rule"] == "mzqc_json"


def test_without_finalise_and_generic_model(validated, tmp_path, monkeypatch):
    run = dict(run="r1", assay="POS", ms_assay_name="A", deposited_mzml="A.mzML",
               derived_path="FILES/DERIVED_FILES/A.mzML")
    meta = {"instrument": {"model": "instrument model", "model_accession": "MS:1000031"}, "counts": {},
            "chromatograms": {"n": 3}, "metrics": {}, "mz_window": None}
    q = mzqc.run_quality(run, meta, None)
    assert "fileProperties" not in q["metadata"]["inputFiles"][0]
    assert q["qualityMetrics"] == [{"accession": "MS:4000071", "name": "number of chromatograms", "value": 3,
                                    "unit": {"accession": "UO:0000189", "name": "count unit"}}]


def test_no_metadata_is_an_input_error(validated, tmp_path, monkeypatch):
    d = describe_inputs(validated, tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(InputError):
        mzqc.run(parse_args(mzqc, ["--study-json", d["study"], "--plan", d["plan"], "--metadata", empty,
                                   "--outdir", tmp_path / "qc"]))


def test_load_finalise_rejects_duplicates(tmp_path):
    for i in (1, 2):
        write_json(tmp_path / f"f{i}.json", dict(schema="msdeposit/finalise/1", run="r1", sha1="a"))
    with pytest.raises(InputError, match="twice"):
        mzqc.load_finalise([tmp_path])
