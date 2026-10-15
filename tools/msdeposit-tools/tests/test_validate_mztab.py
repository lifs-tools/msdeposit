import os
import stat

import pytest

from helpers_results import describe_inputs, parse_args, tools_dir
from msdeposit_tools import validate_mztab
from msdeposit_tools.describe import mztab

STRUCTURE_OUTPUT = """Running jmztabm-cli

 version: '1.0.6'

Validator set to level 'Info'
Beginning validation of mztab file: {path}
[Warn-2018] line 16: Software parameter "[,, Tool, ]" does not contain version information.
[Error-2000] line 30: Column "reliability" is an empty cell, or the current column cannot use "null".
[Info-2037] line -1: The mzTab file should contain a small molecule feature section.
There were 3 validation messages including 2 warnings or errors during validation of your file!
Finished validation!
"""


def fake_validator(directory, output: str):
    """An executable standing in for the 2.1 validator that prints `output` ({path} = checked file)."""
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / validate_mztab.VALIDATOR_21
    exe.write_text("#!/bin/sh\ncat <<'EOF' | sed \"s|{path}|$2|\"\n" + output + "EOF\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


def test_unavailable_without_tools(tmp_path, monkeypatch):
    f = tmp_path / "x.mzTab"
    f.write_text("MTD\tmzTab-version\t2.0.0-M\n")
    monkeypatch.delenv("MSDEPOSIT_TOOLS_DIR", raising=False)
    for version in validate_mztab.VERSIONS:
        res = validate_mztab.validate(f, version)
        assert res == dict(available=False, tool=res["tool"], errors=0, warnings=0, messages=[])
    monkeypatch.setenv("MSDEPOSIT_TOOLS_DIR", str(tmp_path / "empty"))
    assert not validate_mztab.validate(f, "2.1.0-M")["available"]
    with pytest.raises(ValueError):
        validate_mztab.validate(f, "1.0.0")


def test_parse_messages_and_scrub_paths(tmp_path, monkeypatch):
    f = tmp_path / "S1.mztab-2.1.0-M.mzTab"
    f.write_text("MTD\tmzTab-version\t2.1.0-M\n")
    fake_validator(tmp_path / "tools", STRUCTURE_OUTPUT + f"[Warn-9999] path {f} echoed\n")
    monkeypatch.setenv("MSDEPOSIT_TOOLS_DIR", str(tmp_path / "tools"))
    res = validate_mztab.validate(f, "2.1.0-M")
    assert res["available"] and res["tool"] == "jmztabm-validator 1.0.6"
    assert (res["errors"], res["warnings"]) == (1, 2)
    assert [m["rule"] for m in res["messages"]] == ["jmztabm_2018", "jmztabm_2000", "jmztabm_2037", "jmztabm_9999"]
    assert res["messages"][1] == dict(level="error", rule="jmztabm_2000",
                                      message='line 30: Column "reliability" is an empty cell, or the current '
                                              'column cannot use "null".')
    assert str(tmp_path) not in str(res)


def test_silent_abort_is_an_error(tmp_path, monkeypatch):
    f = tmp_path / "x.mzTab"
    f.write_text("")
    fake_validator(tmp_path / "tools", "Running jmztabm-cli\n")
    monkeypatch.setenv("MSDEPOSIT_TOOLS_DIR", str(tmp_path / "tools"))
    res = validate_mztab.validate(f, "2.1.0-M")
    assert res["errors"] == 1 and res["messages"][0]["rule"] == "no_result"


def test_semantic_messages():
    p = validate_mztab.Path("/x/y.mzTab")
    assert validate_mztab.semantic_messages("No errors found for semantic validation on level Error", p) == []
    assert validate_mztab.semantic_messages("[Error-2000] line 3: structural", p) == []   # semantic step skipped
    msgs = validate_mztab.semantic_messages("Beginning semantic validation of mztab file: /x/y.mzTab\n", p)
    assert msgs[0]["level"] == "warning" and msgs[0]["rule"] == "semantic_incomplete"
    sem = ("Beginning semantic validation of mztab file: /x/y.mzTab with mapping file: file:/m.xml\n"
           "class ValidationMessage {\n    code: [Error-3013] line -1: The CV with prefix \"UO\" MUST be defined\n}\n"
           "class ValidationMessage {\n    code: [Error-3013] line -1: The CV with prefix \"UO\" MUST be defined\n}\n")
    msgs = validate_mztab.semantic_messages(sem, p)
    assert msgs == [dict(level="error", rule="jmztabm_3013",
                         message='line -1: The CV with prefix "UO" MUST be defined')]
    offline = sem + ('    code: [Error-3015] line -1: An exception occurred during semantic validation: "I/O error on '
                     'GET request for "https://www.ebi.ac.uk/ols/api/ontologies/MS/terms": Connection refused".\n')
    msgs = validate_mztab.semantic_messages(offline, p)
    assert [m["rule"] for m in msgs] == ["jmztabm_3013", "semantic_incomplete"] and msgs[1]["level"] == "warning"


needs_tools = pytest.mark.skipif(
    not (tools_dir() and (tools_dir() / validate_mztab.JAR).exists()
         and (tools_dir() / validate_mztab.VALIDATOR_21).exists()),
    reason="MSDEPOSIT_TOOLS_DIR with the jmzTab-m validators not set")


@pytest.mark.external
@needs_tools
@pytest.mark.parametrize("version", validate_mztab.VERSIONS)
def test_external_validator_finds_errors(validated, tmp_path, version):
    d = describe_inputs(validated, tmp_path)
    out = tmp_path / "mztab"
    assert mztab.run(parse_args(mztab, ["--study-json", d["study"], "--plan", d["plan"], "--versions", version,
                                        "--outdir", out])) == 0
    path = out / mztab.file_name("S1", version)
    lines = [ln for ln in path.read_text().splitlines() if not ln.startswith("MTD\tmzTab-ID")]
    lines = [ln.replace("\tSpecies\t", "\tnull\t") for ln in lines]
    path.write_text("\n".join(lines) + "\n")
    res = validate_mztab.validate(path, version)
    print(version, res)
    assert res["available"] and res["errors"] >= 2
    assert os.fspath(tmp_path) not in str(res)
