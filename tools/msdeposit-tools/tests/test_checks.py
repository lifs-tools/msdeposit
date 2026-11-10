import json
import sys
import types

import pytest
from helpers_assembly import assemble_argv, check_json, make_plan, write_described, write_plan

from msdeposit_tools import checks, cli


def fake_module(monkeypatch, name, fn):
    mod = types.ModuleType(f"msdeposit_tools.{name}")
    mod.validate = fn
    monkeypatch.setitem(sys.modules, f"msdeposit_tools.{name}", mod)
    return mod


def result(errors=0, warnings=0, available=True, tool="fake 1.0", message="SECRET-MESSAGE /data/x"):
    msgs = [dict(level="error", rule="rule_1", message=message)] * errors
    msgs += [dict(level="warning", rule="rule_2", message=message)] * warnings
    return dict(available=available, tool=tool, errors=errors, warnings=warnings, messages=msgs if available else [])


@pytest.fixture
def upload(tmp_path):
    plan = make_plan()
    plan_path = write_plan(tmp_path / "plan.json", plan)
    inputs = write_described(tmp_path / "in", plan)
    assert cli.main(assemble_argv(inputs, plan_path, tmp_path / "assembled")) == 0
    return tmp_path / "assembled" / "S1", plan_path


def run_checks(upload_dir, outdir, *extra):
    assert cli.main(["check", "--upload-set", str(upload_dir), "--study", "S1", "--outdir", str(outdir),
                     *extra]) == 0


def test_validators_pass(upload, tmp_path, monkeypatch):
    seen = {}
    fake_module(monkeypatch, "validate_mtbls", lambda d: seen.setdefault("mtbls", d) and result())
    fake_module(monkeypatch, "validate_mztab", lambda p, v: seen.setdefault("mztab", []).append((p.name, v)) or
                result(tool="jmztabm 1.0.6"))
    fake_module(monkeypatch, "validate_mzqc", lambda p: result(warnings=1, tool="pymzqc 1.0.3"))
    run_checks(upload[0], tmp_path / "c", "--checks", "C6,C7,C8")
    c6, c7, c8 = (check_json(tmp_path / "c", c) for c in ("C6", "C7", "C8"))
    assert seen["mtbls"] == upload[0]
    assert seen["mztab"] == [("S1.mztab-2.0.0-M.mzTab", "2.0.0-M")]
    assert (c6["status"], c7["status"], c8["status"]) == ("pass", "pass", "warn")
    assert not c6["blocking"] and c8["counts"] == dict(files=1, validated=1, unavailable=0, missing=0, errors=0,
                                                       warnings=1)
    assert "pymzqc 1.0.3" in c8["summary"]
    assert not (tmp_path / "c" / "check_C9.json").exists()


def test_errors_blocking_and_private_messages(upload, tmp_path, monkeypatch):
    fake_module(monkeypatch, "validate_mtbls", lambda d: result(errors=3, warnings=2))
    run_checks(upload[0], tmp_path / "c", "--checks", "C6", "--fail-on-validation-errors", "true")
    c6 = check_json(tmp_path / "c", "C6")
    assert c6["status"] == "fail" and c6["blocking"] is True
    assert c6["counts"]["errors"] == 3 and c6["counts"]["warnings"] == 2
    public = (tmp_path / "c" / "check_C6.json").read_text()
    assert "SECRET-MESSAGE" not in public and "/data/x" not in public
    private = json.loads((tmp_path / "c" / "check_C6.private.json").read_text())
    assert len(private["details"]) == 5 and private["details"][0]["rule"] == "rule_1"


def test_unavailable_is_skipped_never_pass(upload, tmp_path, monkeypatch):
    fake_module(monkeypatch, "validate_mtbls", lambda d: result(available=False))
    monkeypatch.setitem(sys.modules, "msdeposit_tools.validate_mzqc", None)  # import fails -> unavailable
    run_checks(upload[0], tmp_path / "c", "--checks", "C6,C8", "--fail-on-validation-errors", "true")
    c6, c8 = check_json(tmp_path / "c", "C6"), check_json(tmp_path / "c", "C8")
    assert c6["status"] == c8["status"] == "skipped"
    assert c6["blocking"] and c6["summary"].startswith("NOT RUN although blocking")
    run_checks(upload[0], tmp_path / "d", "--checks", "C6")
    assert not check_json(tmp_path / "d", "C6")["summary"].startswith("NOT RUN")


def test_mztab_versions_and_missing_files(upload, tmp_path, monkeypatch):
    calls = []
    fake_module(monkeypatch, "validate_mztab", lambda p, v: calls.append(v) or result())
    run_checks(upload[0], tmp_path / "c", "--checks", "C7", "--mztab-versions", "2.0.0-M,2.1.0-M")
    c7 = check_json(tmp_path / "c", "C7")
    assert c7["status"] == "fail" and c7["counts"]["missing"] == 1 and calls == ["2.0.0-M"]
    (upload[0] / "FILES/QC/S1.mzQC").unlink()
    fake_module(monkeypatch, "validate_mzqc", lambda p: result())
    run_checks(upload[0], tmp_path / "d", "--checks", "C8,C7", "--mztab-versions", "none")
    assert check_json(tmp_path / "d", "C8")["status"] == "fail"
    assert check_json(tmp_path / "d", "C7")["status"] == "pass"  # the file present is still validated
    assert cli.main(["check", "--upload-set", str(upload[0]), "--study", "S1", "--checks", "C10"]) == 1
    assert cli.main(["check", "--upload-set", str(upload[0]), "--study", "S1", "--mztab-versions", "3.0"]) == 1


def test_mztab_version_from_content(tmp_path):
    f = tmp_path / "results.mzTab"
    f.write_text("COM\tx\nMTD\tmzTab-version\t2.1.0-M\n")
    assert checks.mztab_version(f) == "2.1.0-M"
    (tmp_path / "S1.mztab-2.0.0-M.mzTab").write_text("MTD\tmzTab-ID\tx\n")
    assert checks.mztab_version(tmp_path / "S1.mztab-2.0.0-M.mzTab") == "2.0.0-M"


def test_c9_counts_todo_and_gaps(tmp_path):
    plan = make_plan()
    plan_path = write_plan(tmp_path / "plan.json", plan)
    inputs = write_described(tmp_path / "in", plan, todo=True, maf_rt=False)
    (inputs["mzml"][0]).write_text("<indexedmzML>[[TODO-NOT-COUNTED]]</indexedmzML>")
    a = inputs["isa"] / "a_S1_POS.txt"
    a.write_text(a.read_text().replace('"reverse phase"', '""', 1))
    mztab = next(inputs["mztab"].iterdir())
    mztab.write_text(mztab.read_text() + "MTD\tdescription\t[[TODO-DESCRIPTION:study]]\n")
    assert cli.main(assemble_argv(inputs, plan_path, tmp_path / "assembled")) == 0
    up = tmp_path / "assembled" / "S1"
    run_checks(up, tmp_path / "c", "--checks", "C9", "--plan", str(plan_path))
    c9 = check_json(tmp_path / "c", "C9")
    assert c9["status"] == "warn" and not c9["blocking"]
    assert c9["counts"]["todo_placeholders"] == 3 and c9["counts"]["files_with_todo"] == 2
    assert c9["counts"]["empty_required_cells"] == 1
    assert c9["counts"]["maf_missing_mz"] == 1 and c9["counts"]["maf_missing_rt"] == 1
    assert "TODO-PROTOCOL" not in (tmp_path / "c" / "check_C9.json").read_text()
    details = json.loads((tmp_path / "c" / "check_C9.private.json").read_text())["details"]
    assert any(d.get("column") == "Parameter Value[Column type]" for d in details)
    run_checks(up, tmp_path / "d", "--checks", "C9", "--plan", str(plan_path), "--fail-on-todo-placeholders", "true")
    assert check_json(tmp_path / "d", "C9")["status"] == "fail" and check_json(tmp_path / "d", "C9")["blocking"]


def test_c9_clean_and_builtin_columns(upload, tmp_path, monkeypatch):
    up, plan_path = upload
    maf = up / "m_S1_POS_maf.tsv"
    maf.unlink()  # break the hard link before editing
    maf.write_text("mass_to_charge\tretention_time\n760.5851\t5.2\n")
    status, counts, _, _ = checks.check_c9(up, json.loads(plan_path.read_text()), blocking=True)
    assert status == "pass" and sum(counts.values()) == 0
    monkeypatch.setattr(checks, "templates", lambda: None)
    assert checks.required_columns("assay", "LC-MS")[-1] == "Parameter Value[Column type]"
    status, counts, _, summary = checks.check_c9(up, json.loads(plan_path.read_text()), blocking=True)
    assert status == "pass" and "built-in" in summary


@pytest.mark.external
def test_bundled_validators_are_available(upload, tmp_path):
    """In the container: C6-C8 run the real validators on a synthetic upload set (findings expected, no skips)."""
    import os
    if not os.environ.get("MSDEPOSIT_TOOLS_DIR"):
        pytest.skip("MSDEPOSIT_TOOLS_DIR not set")
    run_checks(upload[0], tmp_path / "c", "--checks", "C6,C7,C8")
    for c in ("C6", "C7", "C8"):
        res = check_json(tmp_path / "c", c)
        assert res["status"] != "skipped" and res["counts"]["unavailable"] == 0, (c, res)
