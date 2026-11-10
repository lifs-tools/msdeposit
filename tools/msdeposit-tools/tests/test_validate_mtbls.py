import json
import os
from pathlib import Path

import pytest
from helpers_isa import assemble_stub, complete_study, write_isa

from msdeposit_tools import validate_mtbls

TOOLS = validate_mtbls.find_tools()


def needs_tools(test):
    """Run with MSDEPOSIT_TOOLS_DIR pointing at the validation bundle and OPA (present in the container)."""
    skip = pytest.mark.skipif(TOOLS is None, reason="MSDEPOSIT_TOOLS_DIR with the MetaboLights validation bundle "
                                                    "and OPA not available")
    return pytest.mark.external(skip(test))


def sensitive_complete(s):
    complete_study(s)
    s["mode"] = "sensitive"


def upload_set(validated, tmp_path, edit_study=None, naming="neutral", deposit_raw=False, **opts) -> Path:
    out, _ = write_isa(validated, tmp_path, naming=naming, deposit_raw=deposit_raw, edit_study=edit_study, **opts)
    return assemble_stub(out, tmp_path / "plan" / "plan.json", tmp_path / "upload" / "S1")


def rules(result, level="error") -> set[str]:
    return {m["rule"] for m in result["messages"] if m["level"] == level}


def test_unavailable_without_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("MSDEPOSIT_TOOLS_DIR", str(tmp_path))
    r = validate_mtbls.validate(tmp_path)
    assert r["available"] is False and r["errors"] == 0 and r["warnings"] == 0
    assert set(r) == {"available", "tool", "errors", "warnings", "messages"}
    monkeypatch.delenv("MSDEPOSIT_TOOLS_DIR")
    assert validate_mtbls.validate(tmp_path)["available"] is False


def test_message_mapping():
    m = validate_mtbls._message({"identifier": "rule_x", "title": "Title", "violation": "details", "type": "WARNING",
                                 "sourceFile": "a_x.txt", "sourceColumnHeader": "MS Assay Name", "values": ["v"]})
    assert m == {"level": "warning", "rule": "rule_x", "message": "Title: details", "file": "a_x.txt",
                 "column": "MS Assay Name", "values": ["v"]}


@needs_tools
def test_complete_synthetic_study_has_no_errors(validated, tmp_path):
    root = upload_set(validated, tmp_path, sensitive_complete)
    before = sorted(p.relative_to(root) for p in root.rglob("*"))
    r = validate_mtbls.validate(root)
    assert sorted(p.relative_to(root) for p in root.rglob("*")) == before        # nothing written into the set
    assert r["available"] and r["tool"].startswith("MetaboLights validation bundle 2.2.1")
    assert r["errors"] == 0, [m for m in r["messages"] if m["level"] == "error"]
    assert r["warnings"] == len([m for m in r["messages"] if m["level"] == "warning"]) > 0
    assert all({"level", "rule", "message"} <= set(m) for m in r["messages"])
    # expected warnings: mzML-only deposition, free-text chromatography instrument, submission date set later
    assert {"rule_a_200_300_001_02", "rule_a_200_090_002_22", "rule_i_100_300_005_01"} <= rules(r, "warning")


@needs_tools
def test_minimal_study_reports_input_gaps(validated, tmp_path):
    root = upload_set(validated, tmp_path, naming="original", deposit_raw=True, protocols=False)
    r = validate_mtbls.validate(root)
    assert r["available"] and r["errors"] > 0
    expected = {"rule___100_200_001_08",     # s_ file name without accession
                "rule_i_100_300_002_01",     # no study identifier yet
                "rule_i_100_320_001_01",     # no publication
                "rule_i_100_350_003_01",     # protocol text shorter than 40 characters ([[TODO-...]] placeholder)
                "rule_a_200_090_004_01"}     # Chromatography Instrument empty (assay sheet injection_system)
    assert expected <= rules(r)
    assert "rule_a_200_300_001_03" not in rules(r)   # mzML input is not planned as a raw file


@needs_tools
def test_local_validator_rejects_mhd_terms_in_template_1_0(validated, tmp_path):
    """The bundle lists MetabolomicsHub terms for ms-mhd-enabled studies, but the local validation of template
    version 1.0 enforces the generic values: this is why `validator` is the default profile."""
    root = upload_set(validated, tmp_path, sensitive_complete, controlled_value_profile="mhd")
    r = validate_mtbls.validate(root)
    bad = {m.get("column") for m in r["messages"] if m["rule"] == "rule_a_200_090_002_21"}
    assert bad == {"Parameter Value[Scan polarity]", "Parameter Value[Column type]"}


@needs_tools
def test_shipped_templates_match_bundle():
    bundle, _ = TOOLS
    shipped = json.loads((Path(validate_mtbls.__file__).parent / "data" / "mtbls_templates.json").read_text())
    assert validate_mtbls.templates_from_bundle(bundle) == shipped


def test_tools_dir_env(tmp_path, monkeypatch):
    (tmp_path / validate_mtbls.BUNDLE).write_bytes(b"")
    opa = tmp_path / validate_mtbls.OPA
    opa.write_text("#!/bin/sh\n")
    monkeypatch.setenv("MSDEPOSIT_TOOLS_DIR", str(tmp_path))
    assert validate_mtbls.find_tools() is None          # opa not executable
    os.chmod(opa, 0o755)
    assert validate_mtbls.find_tools() == (tmp_path / validate_mtbls.BUNDLE, opa)
