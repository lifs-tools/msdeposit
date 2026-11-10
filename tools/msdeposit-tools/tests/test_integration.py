"""End-to-end chain through the CLI on synthetic data, as the pipeline will run it (design §7):
validate-inputs → mzml-metadata → [identifier-denylist] → deposit-plan → mzml-finalise → isa/maf/mztab/mzqc →
assemble → check → [check-sensitive, identifier-scan] → gate → deposit-report.
"""

import csv
import json
from pathlib import Path

import pytest

from msdeposit_tools.cli import main
from synthetic import PLANTED, write_example_inputs


def cli(*argv) -> int:
    return main([str(a) for a in argv])


def chain(tmp_path: Path, mode: str, body_rewrite: str = "true") -> dict:
    sheets = write_example_inputs(tmp_path / "in", mode=mode, srm=True)
    priv, pub = tmp_path / "private", tmp_path / "public"
    v = priv / "validated"
    args = [x for k, p in sheets.items() for x in (f"--{k.replace('_', '-')}", p)]
    assert cli("validate-inputs", *args, "--mode", mode, "--base-dir", sheets["input"].parent, "--outdir", v) == 0
    runs = list(csv.DictReader(open(v / "runs.normalised.csv")))
    checks = [v / "S1"]
    for r in runs:
        assert cli("mzml-metadata", "--mzml", r["path"], "--run", r["run"], "--study", "S1",
                   "--out", priv / "meta" / r["run"] / "metadata.json") == 0
    checks.append(priv / "meta")
    sensitive = mode == "sensitive"
    deny = priv / "denylist.tsv"
    if sensitive:
        user = tmp_path / "in" / "user_denylist.txt"
        user.write_text("\n".join(PLANTED.values()) + "\n")
        assert cli("identifier-denylist", "--study", "S1", "--user-denylist", user, "--run-keys",
                   v / "runs.normalised.csv", "--metadata", priv / "meta", "--out", deny) == 0
    plan_dir = priv / "plan"
    assert cli("deposit-plan", "--study-json", v / "S1" / "study.json", "--metadata", priv / "meta",
               "--runs-normalised", v / "runs.normalised.csv", "--deposited-naming",
               "neutral" if sensitive else "original", "--deposit-raw", "false" if sensitive else "true",
               "--outdir", plan_dir) == 0
    checks.append(plan_dir)
    plan = plan_dir / "plan.json"
    for r in runs:
        extra = ["--denylist", deny, "--body-rewrite", body_rewrite] if sensitive else []
        assert cli("mzml-finalise", "--mzml", r["path"], "--plan", plan, "--run", r["run"], "--mode",
                   "anonymise" if sensitive else "rename", "--outdir", priv / "final" / r["run"], *extra) == 0
    checks.append(priv / "final")
    d = priv / "describe"
    common = ["--study-json", v / "S1" / "study.json", "--plan", plan, "--metadata", priv / "meta"]
    assert cli("isa", *common, "--finalise", priv / "final", "--outdir", d / "isa", "--created-at", "2026-10-15") == 0
    assert cli("maf", *common, "--outdir", d / "maf") == 0
    assert cli("mztab", *common, "--finalise", priv / "final", "--versions", "2.0.0-M,2.1.0-M",
               "--outdir", d / "mztab") == 0
    assert cli("mzqc", *common, "--finalise", priv / "final", "--outdir", d / "mzqc") == 0
    mzml = [p for p in (priv / "final").rglob("*.mzML")]
    asm = priv / "assembled"
    assert cli("assemble", "--plan", plan, "--isa", d / "isa", "--maf", d / "maf", "--mztab", d / "mztab",
               "--mzqc", d / "mzqc", "--mzml", *mzml, "--outdir", asm) == 0
    upload = asm / "S1"
    checks.append(asm)
    assert cli("check", "--upload-set", upload, "--study", "S1", "--plan", plan, "--mztab-versions",
               "2.0.0-M,2.1.0-M", "--outdir", priv / "checks") == 0
    checks.append(priv / "checks")
    if sensitive:
        assert cli("check-sensitive", "--study", "S1", "--upload-set", upload, "--plan", plan, "--private-dir", priv,
                   "--outdir", pub, "--runs-normalised", v / "runs.normalised.csv", "--denylist", deny,
                   "--check-dir", priv / "checks_s") == 0
        assert cli("identifier-scan", "--denylist", deny, "--files", upload, "--out-dir", priv / "scan",
                   "--study", "S1", "--self-test") == 0
        checks += [priv / "checks_s", priv / "scan"]
    gate = priv / "gate.json"
    assert cli("gate", "--study", "S1", "--checks", *checks, "--mode", mode, "--out", gate) == 0
    report = upload.parent / "deposit_report.html"
    assert cli("deposit-report", "--study", "S1", "--plan", plan, "--gate", gate, "--checks", *checks,
               "--upload-manifest", upload / "upload_manifest.tsv", "--mode", mode,
               "--public", report, "--private", priv / "deposit_report.private.html") == 0
    return dict(upload=upload, gate=json.loads(gate.read_text()), report=report, priv=priv,
                plan=json.loads(plan.read_text()))


def all_text(root: Path) -> str:
    return "\n".join(p.read_bytes().decode("utf-8", "replace") + "\n" + p.name
                     for p in root.rglob("*") if p.is_file())


def test_standard_chain(tmp_path):
    r = chain(tmp_path, "standard")
    g = r["gate"]
    # validators are not available here (C6-C8 skipped, non-blocking by default); everything else must pass
    assert g["status"] == "passed", g["reasons"]
    assert all(g["checks"][c] in ("pass", "warn") for c in ("C1", "C2", "C3", "C4", "C5")), g["checks"]
    names = {p.name for p in (r["upload"] / "FILES" / "DERIVED_FILES").iterdir()}
    assert names == {x["deposited_mzml"] for x in r["plan"]["runs"]}
    assert (r["upload"] / "i_Investigation.txt").exists() and (r["upload"] / "s_S1.txt").exists()


def test_sensitive_chain_removes_all_planted_identifiers(tmp_path):
    r = chain(tmp_path, "sensitive")
    g = r["gate"]
    assert g["status"] == "passed", (g["reasons"], g["checks"])
    for c in ("S1", "S2", "S3", "S4", "S5", "S7"):
        assert g["checks"][c] == "pass", (c, g["checks"])
    text = all_text(r["upload"]) + r["report"].read_text()
    for key, token in PLANTED.items():
        assert token.lower() not in text.lower(), key
    assert all(x["raw_path"] == "" for x in r["plan"]["runs"])


def test_sensitive_chain_without_body_rewrite_is_blocked(tmp_path):
    """With --body-rewrite false the planted SRM transition name stays in a chromatogram id: S2 must block."""
    r = chain(tmp_path, "sensitive", body_rewrite="false")
    g = r["gate"]
    assert g["status"] == "blocked" and g["checks"]["S2"] == "fail", g
    assert PLANTED["transition"] not in r["report"].read_text()


@pytest.mark.parametrize("mode", ["standard", "sensitive"])
def test_public_artefacts_have_no_input_paths(tmp_path, mode):
    r = chain(tmp_path, mode)
    text = all_text(r["upload"]) + r["report"].read_text()
    assert str(tmp_path) not in text
