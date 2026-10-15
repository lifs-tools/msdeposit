import json

import pytest
from helpers_assembly import TOKENS, make_plan, write_plan
from synthetic import PLANTED

from msdeposit_tools import cli
from msdeposit_tools.io import write_check, write_json, write_table
from msdeposit_tools.report import build_public

RUN_KEYS = (f"{PLANTED['raw_name']}_r1", f"run_{PLANTED['initials']}_2")


@pytest.fixture
def planted(tmp_path):
    """Plan, checks, gate, manifest, versions, params and mapping with planted identifiers everywhere but in the
    allow-listed fields."""
    plan = make_plan(mode="sensitive", deposit_raw=False, run_keys=RUN_KEYS)
    plan["issues"] = [dict(level="warning", code="name_collision", run=RUN_KEYS[0],
                           message=f"file {PLANTED['path']}\\{PLANTED['raw_name']}.raw renamed")]
    plan["assays"][0]["instrument_model"]["name"] = PLANTED["path"]
    plan_path = write_plan(tmp_path / "plan.json", plan)
    c = tmp_path / "checks"
    write_check(c, "S1", "C5", "pass", counts={"files": 7, "bytes": 1234},
                summary=f"7 files incl. {PLANTED['raw_name']}.raw", details=[{"path": PLANTED["path"]}])
    write_check(c, "S1", "S2", "fail", counts={"hits": 2},
                summary=f"token {PLANTED['operator']} found",
                details=[{"token": PLANTED["initials"], "context": PLANTED["email"]}])
    write_check(c, "S1", "C2", "pass", scope="run", run=RUN_KEYS[0], summary=f"run {RUN_KEYS[0]} ok")
    write_check(c, "S1", "C9", "warn", blocking=False, counts={"todo_placeholders": 4}, summary="4 TODO")
    # a misbehaving check: identifying count key and a non-numeric value smuggled in after writing
    bad = json.loads((c / "check_C9.json").read_text())
    bad["counts"][PLANTED["serial"]] = 1
    bad["counts"]["note"] = PLANTED["lab"]
    (c / "check_C9.json").write_text(json.dumps(bad))
    gate = dict(schema="msdeposit/gate/1", study="S1", mode="sensitive", status="blocked",
                checks={"C2": "pass", "C5": "pass", "C9": "warn", "S2": "fail"}, blocking_failures=["S2"],
                missing_checks=["S1"], skipped_blocking=[], runs={"C2": {"pass": 1}},
                reasons=[f"blocking check failed: S2 ({PLANTED['operator']})"], notes=[], summary=PLANTED["lab"])
    write_json(tmp_path / "gate.json", gate)
    manifest = [dict(path="FILES/DERIVED_FILES/QC_01_POS.mzML", bytes=100, sha256="a" * 64, kind="mzml"),
                dict(path=f"FILES/DERIVED_FILES/{PLANTED['path']}.mzML", bytes=5, sha256="b" * 64, kind="mzml"),
                dict(path="i_Investigation.txt", bytes=10, sha256="c" * 64, kind="isa")]
    write_table(tmp_path / "upload_manifest.tsv", manifest, ["path", "bytes", "sha256", "kind"])
    (tmp_path / "versions.yml").write_text(
        f'"MSDEPOSIT:MZML_FINALISE":\n  msdeposit-tools: 0.1.0\n  pwiz: "3.0.26121"\n'
        f'"MSDEPOSIT:CONVERT":\n  wrapper: "/home/{PLANTED["initials"]}/bin/msconvert"\n')
    write_json(tmp_path / "params.json", {"input": f"/data/{PLANTED['raw_name']}/runs.csv",
                                          "outdir": "/srv/results", "email": PLANTED["email"],
                                          "sensitive_data": True, "mztab_versions": "2.0.0-M",
                                          "study_title": PLANTED["lab"], "max_forks": 4,
                                          f"{PLANTED['initials'].lower()}_flag": True})
    write_table(tmp_path / "mapping.tsv", [dict(run=RUN_KEYS[0], raw_id="S1_RAW_001",
                                                original_path=PLANTED["path"], original_name=PLANTED["raw_name"],
                                                deposited_mzml="QC_01_POS.mzML", source_sha1="ab" * 20)])
    return tmp_path, plan_path


def report_argv(d, plan_path, mode="sensitive"):
    return ["deposit-report", "--study", "S1", "--plan", str(plan_path), "--gate", str(d / "gate.json"),
            "--checks", str(d / "checks"), "--upload-manifest", str(d / "upload_manifest.tsv"),
            "--versions", str(d / "versions.yml"), "--params", str(d / "params.json"),
            "--mapping", str(d / "mapping.tsv"), "--mode", mode,
            "--public", str(d / "deposit_report.html"), "--private", str(d / "deposit_report.private.html")]


def test_public_sensitive_report_has_no_planted_identifiers(planted):
    d, plan_path = planted
    assert cli.main(report_argv(d, plan_path)) == 0
    public = (d / "deposit_report.html").read_text()
    lower = public.lower()
    for token in TOKENS + list(RUN_KEYS) + ["/data/", "/srv/", "/home/", "users\\"]:
        assert token.lower() not in lower, token
    # allow-listed content is there
    for expected in ("QC_01_POS.mzML", "P1_A_POS.mzML", "S1_RAW_001", "a_S1_POS.txt", "name_collision",
                     "todo_placeholders: 4", "hits: 2", "pwiz", "3.0.26121", "mztab_versions", "2.0.0-M",
                     "sensitive_data", "3 not shown", "(withheld)", "blocked"):
        assert expected in public, expected
    # self-contained, no scripts or external resources
    for forbidden in ("<script", "http://", "https://", "src=", "<link"):
        assert forbidden not in lower


def test_private_report_keeps_details(planted):
    d, plan_path = planted
    assert cli.main(report_argv(d, plan_path)) == 0
    private = (d / "deposit_report.private.html").read_text()
    assert "PRIVATE" in private
    for token in (PLANTED["operator"], PLANTED["initials"], PLANTED["email"], RUN_KEYS[0]):
        assert token in private, token


def test_plan_mode_wins_over_standard_flag(planted):
    d, plan_path = planted
    assert cli.main(report_argv(d, plan_path, mode="standard")) == 0
    public = (d / "deposit_report.html").read_text()
    assert RUN_KEYS[0] not in public and PLANTED["operator"] not in public
    assert "Mode sensitive" in public


def test_standard_report_shows_run_keys_and_summaries_escaped(tmp_path):
    plan = make_plan()
    plan_path = write_plan(tmp_path / "plan.json", plan)
    write_check(tmp_path / "checks", "S1", "C5", "pass", counts={"files": 3}, summary="3 files <b>ok</b>")
    assert cli.main(["deposit-report", "--study", "S1", "--plan", str(plan_path), "--checks",
                     str(tmp_path / "checks"), "--public", str(tmp_path / "r.html")]) == 0
    public = (tmp_path / "r.html").read_text()
    assert "r001" in public and "QC_01.mzML" in public and "P1_A.d.zip" in public
    assert "3 files &lt;b&gt;ok&lt;/b&gt;" in public and "<b>ok</b>" not in public
    assert "not gated" in public


def test_allow_list_model_fields(planted):
    d, plan_path = planted
    plan = json.loads(plan_path.read_text())
    r = build_public("S1", plan, None, [], [], [("tool", "1.0"), ("x", "C:\\path")],
                     {"input": "/x", "flag": False, "mode_x": "abc"}, "sensitive")
    assert r.redacted and all(x.run is None for x in r.runs)
    assert ("x", "(withheld)") in r.tools
    assert dict(r.params) == {"input": "(not shown)", "(other parameters)": "2 not shown"}
    assert r.assays[0].instrument == "(withheld)"
    r = build_public("S1", plan, None, [], [], [], {"mode_x": "abc", "flag": False}, "standard")
    assert dict(r.params) == {"mode_x": "abc", "flag": "false"} and r.runs[0].run == RUN_KEYS[0]


def test_needs_an_output(planted):
    d, plan_path = planted
    assert cli.main(["deposit-report", "--study", "S1", "--plan", str(plan_path)]) == 1
    assert cli.main(["deposit-report", "--study", "S2", "--plan", str(plan_path), "--public", str(d / "x.html")]) == 1
