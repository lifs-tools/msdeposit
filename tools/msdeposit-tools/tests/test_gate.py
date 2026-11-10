import json

from msdeposit_tools import cli
from msdeposit_tools.io import write_check

ALL_C = ["C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9"]
S_CHECKS = ["S1", "S2", "S3", "S4", "S5", "S7"]


def write_all(d, checks=ALL_C, study="S1", **override):
    for c in checks:
        status, blocking = override.get(c, ("pass", True))
        if c in ("C2", "C4"):
            for run in ("r001", "r002"):
                write_check(d, study, c, status if run == "r002" else "pass", blocking=blocking, scope="run", run=run)
        else:
            write_check(d, study, c, status, blocking=blocking, counts={"n": 1}, summary="ok",
                        details=[{"secret": "x"}])


def gate(tmp_path, d, *extra, study="S1"):
    out = tmp_path / "gate.json"
    assert cli.main(["gate", "--study", study, "--checks", str(d), "--out", str(out), *extra]) == 0
    return json.loads(out.read_text())


def test_all_pass(tmp_path):
    write_all(tmp_path / "c")
    g = gate(tmp_path, tmp_path / "c")
    assert g["schema"] == "msdeposit/gate/1" and g["status"] == "passed"
    assert list(g["checks"]) == ALL_C and set(g["checks"].values()) == {"pass"}
    assert g["runs"] == {"C2": {"pass": 2}, "C4": {"pass": 2}}
    assert g["reasons"] == [] and g["blocking_failures"] == []


def test_blocking_failure_and_non_blocking_failure(tmp_path):
    write_all(tmp_path / "a", C5=("fail", True))
    g = gate(tmp_path, tmp_path / "a")
    assert g["status"] == "blocked" and g["blocking_failures"] == ["C5"]
    write_all(tmp_path / "b", C6=("fail", False), C9=("warn", False))
    g = gate(tmp_path, tmp_path / "b")
    assert g["status"] == "passed" and g["checks"]["C6"] == "fail" and g["checks"]["C9"] == "warn"


def test_per_run_checks_aggregate_to_worst(tmp_path):
    write_all(tmp_path / "c", C4=("fail", True))
    g = gate(tmp_path, tmp_path / "c")
    assert g["checks"]["C4"] == "fail" and g["runs"]["C4"] == {"fail": 1, "pass": 1}
    assert g["status"] == "blocked" and "C4" in g["blocking_failures"]


def test_missing_expected_check_blocks(tmp_path):
    write_all(tmp_path / "c", checks=[c for c in ALL_C if c != "C7"])
    g = gate(tmp_path, tmp_path / "c")
    assert g["status"] == "blocked" and g["missing_checks"] == ["C7"]
    g = gate(tmp_path, tmp_path / "c", "--expected", "C1,C2,C3,C4,C5,C6,C8,C9")
    assert g["status"] == "passed"


def test_no_checks_never_pass(tmp_path):
    (tmp_path / "empty").mkdir()
    g = gate(tmp_path, tmp_path / "empty", "--expected", "")
    assert g["status"] == "blocked" and "no check results found" in g["reasons"]


def test_skipped_blocking_check_blocks(tmp_path):
    write_all(tmp_path / "c", C6=("skipped", True))
    g = gate(tmp_path, tmp_path / "c")
    assert g["status"] == "blocked" and g["skipped_blocking"] == ["C6"]   # fail closed
    assert "blocking check skipped: C6" in g["reasons"] and "C6" in g["summary"]


def test_skipped_non_blocking_check_passes(tmp_path):
    write_all(tmp_path / "c", C6=("skipped", False))
    assert gate(tmp_path, tmp_path / "c")["status"] == "passed"


def test_sensitive_requires_s_checks(tmp_path):
    write_all(tmp_path / "c")
    g = gate(tmp_path, tmp_path / "c", "--mode", "sensitive")
    assert g["status"] == "blocked" and g["missing_checks"] == S_CHECKS
    write_all(tmp_path / "c", checks=S_CHECKS)
    assert gate(tmp_path, tmp_path / "c", "--mode", "sensitive")["status"] == "passed"
    write_all(tmp_path / "c", checks=["S3"], S3=("skipped", False))
    g = gate(tmp_path, tmp_path / "c", "--mode", "sensitive")
    assert g["status"] == "blocked" and "sensitive check skipped: S3" in g["reasons"]
    write_all(tmp_path / "c", checks=["S3"], S3=("fail", False))
    assert gate(tmp_path, tmp_path / "c", "--mode", "sensitive")["status"] == "blocked"


def test_private_files_and_other_studies_ignored(tmp_path):
    write_all(tmp_path / "c")
    write_all(tmp_path / "c" / "other", study="S2", C5=("fail", True))
    (tmp_path / "c" / "check_C1.private.json").write_text("not json at all")
    g = gate(tmp_path, tmp_path / "c")
    assert g["status"] == "passed"


def test_invalid_input(tmp_path):
    (tmp_path / "check_C1.json").write_text(json.dumps({"schema": "msdeposit/other/1"}))
    assert cli.main(["gate", "--study", "S1", "--checks", str(tmp_path / "check_C1.json"),
                     "--out", str(tmp_path / "g.json")]) == 1
    assert cli.main(["gate", "--study", "S1", "--checks", str(tmp_path), "--expected", "X1",
                     "--out", str(tmp_path / "g.json")]) == 1
