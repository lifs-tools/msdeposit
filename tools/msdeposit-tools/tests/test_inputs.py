import csv
import json

from conftest import validate_args
from msdeposit_tools import inputs
from synthetic import PLANTED


def test_valid_inputs(example_inputs, tmp_path):
    out = tmp_path / "out"
    assert inputs.run(validate_args(example_inputs, out)) == 0
    study = json.loads((out / "S1" / "study.json").read_text())
    assert study["schema"] == "msdeposit/study/1"
    assert [r["raw_id"] for r in study["runs"]] == ["S1_RAW_001", "S1_RAW_002", "S1_RAW_003", "S1_RAW_004"]
    assert study["assays"][0]["protocol_keys"] == ["extraction", "chromatography"]
    org = study["samples"][0]["characteristics"][0]
    assert org["name"] == "Organism" and org["source"] == "NCBITaxon"
    assert study["values"][0]["value"] == 12.5
    assert study["config"]["license"] == "CC BY 4.0"
    assert study["contacts"][0]["roles"] == ["Principal Investigator", "Submitter"]
    # no paths in the public study.json
    assert "/" not in json.dumps(study["runs"]) and PLANTED["raw_name"] not in json.dumps(study)
    c1 = json.loads((out / "S1" / "check_C1.json").read_text())
    assert c1["status"] == "pass" and c1["counts"]["runs"] == 4
    rows = list(csv.DictReader(open(out / "runs.normalised.csv")))
    assert rows[0]["path"].startswith("/")


def _rewrite(path, fn):
    rows = list(csv.DictReader(open(path)))
    fields = list(rows[0])
    rows = fn(rows) or rows
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def test_reference_errors(example_inputs, tmp_path):
    def bad(rows):
        rows[0]["assay"] = "XXX"
        rows[1]["sample"] = "nope"
        rows[2]["run"] = rows[3]["run"]
    _rewrite(example_inputs["input"], bad)
    out = tmp_path / "out"
    assert inputs.run(validate_args(example_inputs, out)) == 1
    det = json.loads((out / "S1" / "check_C1.private.json").read_text())["details"]
    msgs = " ".join(d["message"] for d in det)
    assert "assay 'XXX' not defined" in msgs and "sample 'nope'" in msgs and "duplicate run key" in msgs
    assert json.loads((out / "S1" / "check_C1.json").read_text())["status"] == "fail"


def test_values_need_exactly_one_target(example_inputs, tmp_path):
    def bad(rows):
        rows[0]["run"] = "r001"
    _rewrite(example_inputs["values"], bad)
    assert inputs.run(validate_args(example_inputs, tmp_path / "out")) == 1


def test_sensitive_run_keys(example_inputs, tmp_path):
    def bad(rows):
        rows[1]["run"] = rows[1]["path"].split("/")[-1].removesuffix(".mzML")
    _rewrite(example_inputs["input"], bad)
    assert inputs.run(validate_args(example_inputs, tmp_path / "o1", mode="standard")) == 0
    assert inputs.run(validate_args(example_inputs, tmp_path / "o2", mode="sensitive")) == 1


def test_sensitive_user_denylist(example_inputs, tmp_path):
    deny = tmp_path / "deny.txt"
    deny.write_text("r00\n")      # inside 'r001' only: no word match (the identifier scan would not match either)
    assert inputs.run(validate_args(example_inputs, tmp_path / "o1", mode="sensitive", denylist=deny)) == 0
    deny.write_text("POS\n")      # the assay key itself
    assert inputs.run(validate_args(example_inputs, tmp_path / "o2", mode="sensitive", denylist=deny)) == 1


def test_multisample_paths_need_run_index(example_inputs, tmp_path):
    def same_path(rows):
        rows[1]["path"] = rows[0]["path"]
        rows[1]["sample"] = rows[0]["sample"]
    _rewrite(example_inputs["input"], same_path)
    assert inputs.run(validate_args(example_inputs, tmp_path / "o1")) == 1

    rows = list(csv.DictReader(open(example_inputs["input"])))
    rows[0]["run_index"], rows[1]["run_index"] = "1", "2"
    with open(example_inputs["input"], "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), restval="", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    out = tmp_path / "o2"
    assert inputs.run(validate_args(example_inputs, out)) == 0
    study = json.loads((out / "S1" / "study.json").read_text())
    assert study["runs"][0]["raw_id"] == study["runs"][1]["raw_id"]
    assert study["runs"][1]["run_index"] == 2


def test_duplicate_sample_columns(example_inputs, tmp_path):
    p = example_inputs["samples"]
    text = p.read_text().splitlines()
    text[0] = text[0].replace("comment:Sequence position", "characteristics:Organism part")
    p.write_text("\n".join(text) + "\n")
    assert inputs.run(validate_args(example_inputs, tmp_path / "o")) == 1


def test_vendor_detection(tmp_path):
    (tmp_path / "a.d" / "AcqData").mkdir(parents=True)
    (tmp_path / "b.d").mkdir()
    (tmp_path / "b.d" / "analysis.tdf").write_text("")
    (tmp_path / "c.raw").mkdir()
    (tmp_path / "d.raw").write_text("")
    (tmp_path / "e.wiff").write_text("")
    got = [inputs.detect_vendor(tmp_path / n) for n in ("a.d", "b.d", "c.raw", "d.raw", "e.wiff")]
    assert got == ["agilent", "bruker_tdf", "waters", "thermo", "sciex"]


def test_key_charset(example_inputs, tmp_path):
    def bad(rows):
        rows[0]["run"] = "r 1; rm -rf"
    _rewrite(example_inputs["input"], bad)
    assert inputs.run(validate_args(example_inputs, tmp_path / "o")) == 1


def test_path_facts_replace_filesystem(example_inputs, tmp_path):
    """Object-store inputs: the pipeline passes facts instead of paths the container could open."""
    rows = list(csv.DictReader(open(example_inputs["input"])))
    facts = {r["path"]: {"uri": f"s3://bucket/study/{r['path']}", "exists": True, "is_dir": False,
                         "children": [], "siblings": []} for r in rows}
    for r in rows:
        r["format"], r["vendor"] = "vendor_raw", ""
        old = r["path"]
        r["path"] = old.replace(".mzML", ".d")
        facts[r["path"]] = {"uri": f"s3://bucket/study/{r['path']}", "exists": True, "is_dir": True,
                            "children": ["AcqData", "desktop.ini"], "siblings": []}
    _rewrite(example_inputs["input"], lambda rs: rows)
    (tmp_path / "facts.json").write_text(json.dumps(facts))
    out = tmp_path / "o"
    assert inputs.run(validate_args(example_inputs, out, path_facts=tmp_path / "facts.json")) == 0
    norm = list(csv.DictReader(open(out / "runs.normalised.csv")))
    assert norm[0]["path"].startswith("s3://bucket/study/") and norm[0]["vendor"] == "agilent"
    facts[rows[0]["path"]]["exists"] = False
    (tmp_path / "facts.json").write_text(json.dumps(facts))
    assert inputs.run(validate_args(example_inputs, tmp_path / "o2", path_facts=tmp_path / "facts.json")) == 1


def test_shared_raw_id_for_converted_multisample_runs(example_inputs, tmp_path):
    """mzML converted from one multi-sample vendor file: several paths may share an explicit raw_id."""
    rows = list(csv.DictReader(open(example_inputs["input"])))
    for r in rows[:2]:
        r["raw_id"] = "S1_RAW_007"
    with open(example_inputs["input"], "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[2]) + ["raw_id"], restval="", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    out = tmp_path / "o"
    assert inputs.run(validate_args(example_inputs, out)) == 0
    study = json.loads((out / "S1" / "study.json").read_text())
    assert study["runs"][0]["raw_id"] == study["runs"][1]["raw_id"] == "S1_RAW_007"
