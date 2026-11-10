import csv
import json
import shutil
import zipfile

import helpers_sensitive as hs
import pytest
from synthetic import PLANTED

from msdeposit_tools import cli
from msdeposit_tools.io import read_json, write_json
from msdeposit_tools.sensitive.denylist import Entry, write_denylist


@pytest.fixture
def case(tmp_path):
    info = hs.upload_set(tmp_path / "work")
    info["private"] = tmp_path / "private"
    info["private"].mkdir()
    info["outdir"] = tmp_path / "outdir"
    info["outdir"].mkdir()
    info["denylist"] = hs.planted_denylist(info["private"] / "denylist.tsv")
    info["checks"] = tmp_path / "checks"
    return info


def run_checks(case, checks, *extra, policy="keep"):
    argv = ["check-sensitive", "--study", "S1", "--upload-set", str(case["dir"]), "--plan", str(case["plan"]),
            "--private-dir", str(case["private"]), "--outdir", str(case["outdir"]), "--denylist", str(case["denylist"]),
            "--timestamp-policy", policy, "--checks", checks, "--check-dir", str(case["checks"]), *map(str, extra)]
    assert cli.main(argv) == 0
    return {c: read_json(case["checks"] / f"check_{c}.json", "msdeposit/check/1") for c in checks.split(",")}


def edit_mzml(case, old: bytes, new: bytes, i=0):
    p = case["mzml"][i]
    data = p.read_bytes()
    assert old in data
    p.write_bytes(data.replace(old, new, 1))


def test_all_pass_on_clean_set(case):
    res = run_checks(case, "S3,S4,S5,S7")
    assert {c: r["status"] for c, r in res.items()} == dict(S3="pass", S4="pass", S5="pass", S7="pass")
    assert all(r["blocking"] for r in res.values())
    assert res["S3"]["counts"]["files"] == 2 and res["S4"]["counts"]["assay_files"] == 1


# --- S3 ---------------------------------------------------------------------------------------------------------------
SERIAL = b'<cvParam cvRef="MS" accession="MS:1000529" name="instrument serial number" value="SN-ZX9917"/>'
S3_CASES = {
    "serial": (b'<instrumentConfiguration id="IC1">', b'<instrumentConfiguration id="IC1">' + SERIAL, "serials"),
    "contact": (b"</fileDescription>", b'<contact><cvParam cvRef="MS" accession="MS:1000586" name="contact name" '
                                       b'value="x"/></contact></fileDescription>', "contacts"),
    "userparam": (b'<sample id="QC_01" name="QC_01">', b'<sample id="QC_01" name="QC_01"><userParam name="Operator" '
                                                       b'value="QMF" type="xsd:string"/>', "user_params"),
    "location": (b'location="file:///"', b'location="file:///D:/Users/x/HLB.raw"', "source_file_paths"),
    "location_drive": (b'location="file:///"', b'location="file:///C:"', "source_file_paths"),
    "mzml_id": (b'id="QC_01_POS" version', b'id="HLB_qmf_plasma_007" version', "id_mismatches"),
    "run_id": (b'<run id="QC_01_POS"', b'<run id="other"', "id_mismatches"),
}


@pytest.mark.parametrize("name", sorted(S3_CASES))
def test_s3_residual_fields(case, name):
    old, new, counter = S3_CASES[name]
    edit_mzml(case, old, new)
    r = run_checks(case, "S3")["S3"]
    assert r["status"] == "fail" and r["counts"][counter] >= 1 and r["counts"]["files_failed"] == 1
    assert "QMF" not in json.dumps(r) and "Users" not in json.dumps(r)


def test_s3_allowed_values(case):
    edit_mzml(case, b'<sample id="QC_01" name="QC_01">',
              b'<sample id="QC_01" name="QC_01"><userParam name="injection volume" value="2.5" type="xsd:double"/>')
    edit_mzml(case, b'location="file:///"', b'location="file:///S1_RAW_002.d/AcqData"', i=1)
    assert run_checks(case, "S3")["S3"]["status"] == "pass"


@pytest.mark.parametrize("policy,start,ok", [("keep", "2024-03-05T09:41:17Z", True),
                                             ("date", "2024-03-05T09:41:17Z", False),
                                             ("date", "2024-03-05T00:00:00Z", True), ("date", "2024-03-05", True),
                                             ("drop", "2024-03-05T00:00:00Z", False)])
def test_s3_timestamp_policy(case, policy, start, ok):
    for i in range(2):
        edit_mzml(case, b'startTimeStamp="2024-03-05T09:41:17Z"', f'startTimeStamp="{start}"'.encode(), i)
    assert run_checks(case, "S3", policy=policy)["S3"]["status"] == ("pass" if ok else "fail")


def test_s3_dropped_timestamp_and_unplanned_file(case):
    for i in range(2):
        edit_mzml(case, b' startTimeStamp="2024-03-05T09:41:17Z"', b"", i)
    assert run_checks(case, "S3", policy="drop")["S3"]["status"] == "pass"
    shutil.copy(case["mzml"][0], case["dir"] / "FILES" / "DERIVED_FILES" / "extra.mzML")
    r = run_checks(case, "S3", policy="drop")["S3"]
    assert r["status"] == "fail" and r["counts"]["unplanned"] == 1


def test_s3_original_name_and_unreadable(case, tmp_path):
    runs = tmp_path / "runs.normalised.csv"
    with open(runs, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["study", "run", "assay", "path", "companions"])
        w.writeheader()
        w.writerow(dict(study="S1", run="r001", assay="POS", path=f"/data/{PLANTED['raw_name']}.raw", companions=""))
    edit_mzml(case, b'name="S1_RAW_001.raw"', f'name="{PLANTED["raw_name"]}.raw"'.encode())
    r = run_checks(case, "S3", "--runs-normalised", runs)["S3"]
    assert r["status"] == "fail" and r["counts"]["original_names"] == 1
    case["mzml"][1].write_bytes(b"<mzML><broken")
    assert run_checks(case, "S3")["S3"]["counts"]["unreadable"] == 1


def test_s3_fails_without_mzml(case):
    for p in case["mzml"]:
        p.unlink()
    assert run_checks(case, "S3")["S3"]["status"] == "fail"


# --- S4 ---------------------------------------------------------------------------------------------------------------
def zip_with(path, *names):
    with zipfile.ZipFile(path, "w") as z:
        for n in names:
            z.writestr(n, b"x")


def put(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


S4_CASES = {
    "raw_file": lambda up: put(up / "FILES" / "RAW_FILES" / "a.raw"),
    "wiff_scan": lambda up: (up / "FILES" / "a.wiff.scan").write_bytes(b"x"),
    "bruker_dir": lambda up: put(up / "FILES" / "run.d" / "analysis.tdf"),
    "waters_member": lambda up: (up / "FILES" / "QC").joinpath("_FUNC001.DAT").write_bytes(b"x"),
    "acqdata_dir": lambda up: (up / "FILES" / "QC" / "x" / "AcqData").mkdir(parents=True),
    "thermo_magic": lambda up: put(up / "FILES" / "QC" / "notes.bin", b"\x01\xa1" + "Finnigan".encode("utf-16-le")),
    "sqlite_magic": lambda up: (up / "FILES" / "QC" / "x.db").write_bytes(b"SQLite format 3\x00" + bytes(100)),
    "zip_raw": lambda up: zip_with(up / "FILES" / "QC" / "extra.zip", "a/b.d/analysis.tdf"),
    "zip_raw_name": lambda up: zip_with(up / "FILES" / "QC" / "extra.zip", "x.RAW"),
    "seven_zip": lambda up: (up / "FILES" / "QC" / "x.7z").write_bytes(b"7z\xbc\xaf\x27\x1c" + bytes(30)),
}


@pytest.mark.parametrize("name", sorted(S4_CASES))
def test_s4_raw_absence(case, name):
    S4_CASES[name](case["dir"])
    r = run_checks(case, "S4")["S4"]
    assert r["status"] == "fail", name


def test_s4_harmless_zip_passes(case):
    zip_with(case["dir"] / "FILES" / "QC" / "extra.zip", "readme.txt", "plots/a.png")
    assert run_checks(case, "S4")["S4"]["status"] == "pass"


def rewrite_assay(case, fn):
    a = case["dir"] / "a_S1_POS.txt"
    rows = list(csv.reader(open(a, newline=""), delimiter="\t"))
    rows = fn(rows)
    with open(a, "w", newline="") as fh:
        csv.writer(fh, delimiter="\t", quoting=csv.QUOTE_ALL, lineterminator="\n").writerows(rows)


@pytest.mark.parametrize("change,counter", [
    (lambda rows: [rows[0]] + [r[:3] + ["x.raw"] + r[4:] for r in rows[1:]], "assay_raw_filled"),
    (lambda rows: [r[:4] + r[5:] for r in rows], "assay_missing_raw_id"),
    (lambda rows: rows[:2] + [rows[2][:4] + [""] + rows[2][5:]], "assay_missing_raw_id"),
    (lambda rows: rows[:2] + [rows[2][:4] + [PLANTED["raw_name"]] + rows[2][5:]], "unknown_raw_ids")])
def test_s4_isa_assay_columns(case, change, counter):
    rewrite_assay(case, change)
    r = run_checks(case, "S4")["S4"]
    assert r["status"] == "fail" and r["counts"][counter] == 1
    assert PLANTED["raw_name"] not in json.dumps(r)


# --- S5 ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("where,name", [("upload", "mapping.tsv"), ("outdir", "denylist.tsv"),
                                        ("outdir", "check_S2.private.json"), ("upload", "hits.tsv"),
                                        ("outdir", "vendor_metadata/R1/candidates.tsv"),
                                        ("outdir", "S1/runs.normalised.csv")])
def test_s5_private_artefacts(case, where, name):
    p = (case["dir"] if where == "upload" else case["outdir"]) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x\n")
    r = run_checks(case, "S5")["S5"]
    assert r["status"] == "fail" and r["counts"]["private_artefacts"] >= 1


def test_s5_private_path_reference(case):
    html = case["dir"] / "FILES" / "QC" / "deposit_report.html"
    html.write_text(html.read_text() + f'<a href="file://{case["private"].resolve()}/mapping.tsv">x</a>')
    r = run_checks(case, "S5")["S5"]
    assert r["status"] == "fail" and r["counts"]["private_path_references"] == 1
    assert str(case["private"]) not in json.dumps(r)


def test_s5_nested_dirs_and_extra_patterns(case, tmp_path):
    (case["outdir"] / "fixity.tsv").write_text("x\n")
    assert run_checks(case, "S5")["S5"]["status"] == "pass"
    assert run_checks(case, "S5", "--private-patterns", "fixity*.tsv")["S5"]["status"] == "fail"
    case["outdir"] = case["private"] / "public"
    case["outdir"].mkdir()
    r = run_checks(case, "S5")["S5"]
    assert r["status"] == "fail" and r["counts"]["nested_outdirs"] == 1


# --- S7 ---------------------------------------------------------------------------------------------------------------
def edit_plan(case, fn):
    plan = json.loads(case["plan"].read_text())
    fn(plan)
    write_json(case["plan"], plan)


@pytest.mark.parametrize("fn", [lambda p: p["runs"][0].update(run="HLB_007"),
                                lambda p: p["assays"][0].update(assay="qmarbleford_pos"),
                                lambda p: p["runs"][1].update(raw_id="S1_HLB_002"),
                                lambda p: p["runs"][0].update(run="lab/r1")])
def test_s7_keys(case, fn):
    edit_plan(case, fn)
    r = run_checks(case, "S7")["S7"]
    assert r["status"] == "fail" and r["counts"]["hits"] + r["counts"]["separators"] >= 1
    assert "HLB" not in json.dumps(r) and "qmarbleford" not in json.dumps(r)


def test_s7_published_paths_and_run_sheet(case, tmp_path):
    (case["outdir"] / "pipeline_info").mkdir()
    (case["outdir"] / "pipeline_info" / "trace_Haldenbrook.txt").write_text("x")
    assert run_checks(case, "S7")["S7"]["status"] == "fail"
    (case["outdir"] / "pipeline_info" / "trace_Haldenbrook.txt").unlink()
    runs = tmp_path / "runs.csv"
    runs.write_text("study,run,assay,path\nS1,Marbleford_1,POS,/x/a.raw\nS2,QMF,POS,/x/b.raw\n")
    r = run_checks(case, "S7", "--runs-normalised", runs)["S7"]
    assert r["status"] == "fail" and r["counts"]["hits"] == 1             # other studies are not checked


def test_s7_empty_denylist_fails(case):
    write_denylist(case["denylist"], [])
    assert run_checks(case, "S7")["S7"]["status"] == "fail"
    write_denylist(case["denylist"], [Entry("Unrelated", "user", False)])
    assert run_checks(case, "S7")["S7"]["status"] == "pass"


def test_usage_errors(case):
    base = ["check-sensitive", "--study", "S1", "--upload-set", str(case["dir"]), "--plan", str(case["plan"]),
            "--check-dir", str(case["checks"])]
    assert cli.main(base + ["--checks", "S5"]) == 1
    assert cli.main(base + ["--checks", "S7"]) == 1
    assert cli.main(base + ["--checks", "S9"]) == 1
    assert cli.main(base + ["--checks", "S3,S4"]) == 0
