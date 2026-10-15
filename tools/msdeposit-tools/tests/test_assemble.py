import json
import os

import pytest
from helpers_assembly import assemble_argv, check_json, make_plan, write_described, write_plan

from msdeposit_tools import cli
from msdeposit_tools.assemble import check_upload_set, write_manifest
from msdeposit_tools.io import read_table


@pytest.fixture
def setup(tmp_path):
    plan = make_plan()
    plan_path = write_plan(tmp_path / "plan.json", plan)
    inputs = write_described(tmp_path / "in", plan)
    return plan, plan_path, inputs, tmp_path / "out"


def assemble(inputs, plan_path, out, report=None):
    assert cli.main(assemble_argv(inputs, plan_path, out, report)) == 0
    return out / "S1", check_json(out, "C5")


def test_complete_upload_set_passes(setup, tmp_path):
    plan, plan_path, inputs, out = setup
    report = tmp_path / "deposit_report.html"
    report.write_text("<html>report</html>")
    root, c5 = assemble(inputs, plan_path, out, report)
    assert c5["status"] == "pass", json.loads((out / "check_C5.private.json").read_text())
    assert c5["blocking"] and c5["check"] == "C5"
    expected = {"i_Investigation.txt", "s_S1.txt", "a_S1_POS.txt", "m_S1_POS_maf.tsv",
                "FILES/DERIVED_FILES/QC_01.mzML", "FILES/DERIVED_FILES/P1_A.mzML", "FILES/RAW_FILES/QC_01.wiff",
                "FILES/RAW_FILES/QC_01.wiff.scan", "FILES/RAW_FILES/P1_A.d.zip",
                "FILES/RESULTS/S1.mztab-2.0.0-M.mzTab", "FILES/QC/S1.mzQC", "FILES/QC/deposit_report.html"}
    rows = read_table(root / "upload_manifest.tsv")
    assert {r["path"] for r in rows} == expected
    assert list(rows[0]) == ["path", "bytes", "sha256", "kind"]
    kinds = {r["path"]: r["kind"] for r in rows}
    assert kinds["FILES/RAW_FILES/P1_A.d.zip"] == "raw" and kinds["a_S1_POS.txt"] == "isa"
    assert kinds["FILES/QC/deposit_report.html"] == "report" and kinds["m_S1_POS_maf.tsv"] == "maf"
    assert os.path.samefile(root / "FILES/DERIVED_FILES/QC_01.mzML", inputs["mzml"][0])  # hard link
    assert not any(p.is_symlink() for p in root.rglob("*"))
    assert not list(root.glob("check_*")), "check files stay outside the upload set"
    assert c5["counts"]["files"] == len(expected) + 1  # + the manifest
    assert str(tmp_path) not in (out / "check_C5.json").read_text()


def test_missing_file_fails(setup):
    plan, plan_path, inputs, out = setup
    inputs["mzml"] = inputs["mzml"][:1]
    _, c5 = assemble(inputs, plan_path, out)
    assert c5["status"] == "fail" and c5["counts"]["missing"] == 1
    details = json.loads((out / "check_C5.private.json").read_text())["details"]
    assert {"problem": "missing", "path": "FILES/DERIVED_FILES/P1_A.mzML"} in details
    assert "P1_A.mzML" not in c5["summary"]


def test_isa_reference_without_file_fails(setup):
    plan, plan_path, inputs, out = setup
    a = inputs["isa"] / "a_S1_POS.txt"
    a.write_text(a.read_text().replace("FILES/DERIVED_FILES/P1_A.mzML", "FILES/DERIVED_FILES/ghost.mzML"))
    _, c5 = assemble(inputs, plan_path, out)
    # ghost is missing, and the planned P1_A.mzML is not referenced by the assay table
    assert c5["status"] == "fail"
    assert c5["counts"]["missing"] == 1 and c5["counts"]["not_in_isa"] == 1


def test_unplanned_input_and_unreferenced_file_fail(setup):
    plan, plan_path, inputs, out = setup
    extra = inputs["mzml"][0].parent / "extra_run.mzML"
    extra.write_text("<indexedmzML/>")
    inputs["mzml"].append(extra)
    root, c5 = assemble(inputs, plan_path, out)
    assert c5["status"] == "fail" and c5["counts"]["unplanned_inputs"] == 1
    assert not (root / "FILES/DERIVED_FILES/extra_run.mzML").exists()

    # a file slipped into an upload set afterwards is unreferenced
    (root / "FILES/DERIVED_FILES/extra_run.mzML").write_text("<indexedmzML/>")
    write_manifest(root)
    status, counts, details = check_upload_set(root, plan)
    assert status == "fail" and counts["unreferenced"] == 1 and counts["manifest_problems"] == 0


def test_bad_names_and_folders_fail(setup):
    plan, plan_path, inputs, out = setup
    root, c5 = assemble(inputs, plan_path, out)
    assert c5["status"] == "pass"
    (root / "FILES/QC/bad name.mzQC").write_text("{}")
    (root / "extra").mkdir()
    (root / "extra" / "notes.txt").write_text("x")
    write_manifest(root)
    status, counts, details = check_upload_set(root, plan)
    assert status == "fail" and counts["bad_names"] == 2
    assert {"problem": "bad_folder", "path": "extra/notes.txt"} in details
    assert {"problem": "bad_name", "path": "FILES/QC/bad name.mzQC"} in details


def test_zero_byte_and_symlink_fail(setup):
    plan, plan_path, inputs, out = setup
    (inputs["mzqc"] / "S1.mzQC").write_text("")
    root, c5 = assemble(inputs, plan_path, out)
    assert c5["status"] == "fail" and c5["counts"]["zero_bytes"] == 1
    (root / "FILES/QC/link.mzQC").symlink_to(root / "s_S1.txt")
    status, counts, _ = check_upload_set(root, plan)
    assert counts["symlinks"] == 1


def test_manifest_must_match(setup):
    plan, plan_path, inputs, out = setup
    root, _ = assemble(inputs, plan_path, out)
    (root / "s_S1.txt").write_text((root / "s_S1.txt").read_text() + "\n")  # size changes after hashing
    status, counts, _ = check_upload_set(root, plan)
    assert status == "fail" and counts["manifest_problems"] == 1
    (root / "upload_manifest.tsv").unlink()
    assert check_upload_set(root, plan)[1]["manifest_problems"] == 1


def test_raw_package_hash_mismatch(setup):
    plan, plan_path, inputs, out = setup
    rec = inputs["raw"][0].parent / "S1_RAW_002.raw_package.json"
    rec.write_text(json.dumps(dict(schema="msdeposit/raw_package/1", study="S1", raw_id="S1_RAW_002",
                                   packaging="zip", files=[dict(deposited="FILES/RAW_FILES/P1_A.d.zip", bytes=1,
                                                                sha256="0" * 64)])))
    inputs["raw"].append(rec)
    root, c5 = assemble(inputs, plan_path, out)
    assert c5["status"] == "fail" and c5["counts"]["sha256_mismatches"] == 1
    assert not (root / "FILES/RAW_FILES" / rec.name).exists()


def test_sensitive_plan_rejects_raw_files(tmp_path):
    plan = make_plan(mode="sensitive", deposit_raw=False)
    plan_path = write_plan(tmp_path / "plan.json", plan)
    inputs = write_described(tmp_path / "in", plan)
    root, c5 = assemble(inputs, plan_path, tmp_path / "out")
    assert c5["status"] == "pass"
    assert not (root / "FILES/RAW_FILES").exists()
    stray = tmp_path / "in" / "QC_01.wiff"
    stray.write_bytes(b"raw")
    inputs["raw"] = [stray]
    _, c5 = assemble(inputs, plan_path, tmp_path / "out2")
    assert c5["status"] == "fail" and c5["counts"]["unplanned_inputs"] == 1


def test_existing_upload_set_is_refused(setup):
    plan, plan_path, inputs, out = setup
    assemble(inputs, plan_path, out)
    assert cli.main(assemble_argv(inputs, plan_path, out)) == 1


def test_extend_adds_report_without_rehashing(setup, tmp_path, monkeypatch):
    plan, plan_path, inputs, out = setup
    root, c5 = assemble(inputs, plan_path, out)
    assert c5["status"] == "pass"
    report = tmp_path / "deposit_report.html"
    report.write_text("<html>report</html>")
    hashed = []
    import msdeposit_tools.assemble as asm
    real = asm.hash_many
    monkeypatch.setattr(asm, "hash_many", lambda paths, jobs: hashed.extend(paths) or real(paths, jobs))
    out2 = tmp_path / "out2"
    assert cli.main(["assemble", "--plan", str(plan_path), "--extend", str(root), "--report", str(report),
                     "--outdir", str(out2)]) == 0
    assert check_json(out2, "C5")["status"] == "pass"
    assert [p.name for p in hashed] == ["deposit_report.html"]
    rows = {r["path"]: r for r in read_table(out2 / "S1" / "upload_manifest.tsv")}
    old = {r["path"]: r for r in read_table(root / "upload_manifest.tsv")}
    assert set(rows) == set(old) | {"FILES/QC/deposit_report.html"}
    assert all(rows[k]["sha256"] == old[k]["sha256"] for k in old)
    assert os.path.samefile(out2 / "S1" / "FILES/DERIVED_FILES/QC_01.mzML", root / "FILES/DERIVED_FILES/QC_01.mzML")
