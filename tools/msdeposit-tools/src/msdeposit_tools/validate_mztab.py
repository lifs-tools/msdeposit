"""mzTab-M validation (check C7; CONTRACTS.md §10).

`validate(path, version) -> dict` runs the validator pinned for the version and never raises for findings:
  - 2.0.0-M: jmzTab-m 1.0.6 (`$MSDEPOSIT_TOOLS_DIR/jmztabm-cli/jmztabm-cli-1.0.6.jar`, Java from PATH): structural
    validation at level Info, then a separate semantic pass with the summary CV mapping at level Error (jmzTab-m runs
    the semantic step only when the structural step raised nothing at the chosen level). The mapping is taken from
    `$MSDEPOSIT_TOOLS_DIR/mzTab_2_0-M_summary-mapping.xml`, else the copy bundled in `data/` (jmzTab-m project,
    Apache 2.0). The semantic pass resolves CV terms online; when that fails it is reported as a warning.
  - 2.1.0-M: the jmzTab-m 2.1 development validator (`$MSDEPOSIT_TOOLS_DIR/jmztabm-validator-amd64-linux`) at level
    Info.
A run that does not finish (jmzTab-m 1.0.6 aborts silently on some inputs, e.g. wide files with several `opt_`
columns) is an error. Messages carry no paths.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

JAR = Path("jmztabm-cli") / "jmztabm-cli-1.0.6.jar"
VALIDATOR_21 = "jmztabm-validator-amd64-linux"
MAPPING = "mzTab_2_0-M_summary-mapping.xml"
BUNDLED_MAPPING = Path(__file__).parent / "data" / MAPPING
VERSIONS = ("2.0.0-M", "2.1.0-M")
LEVELS = {"Error": "error", "Warn": "warning", "Info": "info"}
# structural messages start the line; semantic ones are printed as "    code: [Error-3013] line -1: ..."
MESSAGE = re.compile(r"^\s*(?:code:\s*)?\[(Error|Warn|Info)-(\d+)\]\s*(.*)$", re.M)
SEMANTIC_EXCEPTION = "jmztabm_3015"
TIMEOUT = 900


def tools_dir() -> Path | None:
    d = os.environ.get("MSDEPOSIT_TOOLS_DIR")
    return Path(d) if d else None


def _result(available: bool, tool: str, messages: list[dict]) -> dict:
    return dict(available=available, tool=tool, errors=sum(1 for m in messages if m["level"] == "error"),
                warnings=sum(1 for m in messages if m["level"] == "warning"), messages=messages)


def _run(cmd: list[str]) -> tuple[str, bool]:
    """(stdout+stderr, finished)."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return "", False
    except OSError as e:
        return str(e), False
    text = out.stdout + out.stderr
    return text, "Finished validation" in text


def _scrub(text: str, path: Path) -> str:
    """Remove the file's location (validators echo it) from a message."""
    resolved = path.resolve()
    for p in (str(resolved), str(path), str(resolved.parent), str(path.parent)):
        if len(p) > 1:
            text = text.replace("file:" + p, "").replace(p, "")
    return text.strip()


def parse(text: str, path: Path) -> list[dict]:
    """Validator messages, identical ones once."""
    out = {}
    for m in MESSAGE.finditer(text):
        msg = dict(level=LEVELS[m[1]], rule=f"jmztabm_{m[2]}", message=_scrub(m[3], path))
        out.setdefault(tuple(msg.values()), msg)
    return list(out.values())


def tool_version(text: str) -> str:
    m = re.search(r"version:\s*'([^']+)'", text)
    return m[1] if m else ""


def semantic_messages(text: str, path: Path) -> list[dict]:
    """Messages of the semantic pass; [] when it found nothing or was skipped (structural errors at level Error,
    which the structural pass already reported). Rules that could not be evaluated (Error-3015, typically the online
    CV term lookup failing) and a pass without result give one warning, not errors about the file."""
    if "Beginning semantic validation" not in text or "No errors found for semantic validation" in text:
        return []
    msgs = parse(text.split("Beginning semantic validation", 1)[1], path)
    found = [m for m in msgs if m["rule"] != SEMANTIC_EXCEPTION]
    if len(found) < len(msgs) or not msgs:
        found.append(dict(level="warning", rule="semantic_incomplete",
                          message="semantic validation incomplete: mapping rules could not be evaluated "
                                  "(the CV term lookup needs network access)"))
    return found


def validate(path: Path, version: str) -> dict:
    path = Path(path)
    if version not in VERSIONS:
        raise ValueError(f"unknown mzTab-M version {version}")
    tools = tools_dir()
    if version == "2.0.0-M":
        jar, java = (tools / JAR) if tools else None, shutil.which("java")
        if not (jar and jar.is_file() and java):
            return _result(False, "jmztabm-cli 1.0.6", [])
        mapping = tools / MAPPING if (tools / MAPPING).is_file() else BUNDLED_MAPPING
        cmd = [java, "-jar", str(jar), "-c", str(path)]
        text, finished = _run(cmd + ["-level", "Info"])
        messages = parse(text, path)
        if finished:
            sem, _ = _run(cmd + ["-level", "Error", "-s", str(mapping)])
            seen = {(m["level"], m["rule"], m["message"]) for m in messages}
            messages += [m for m in semantic_messages(sem, path) if (m["level"], m["rule"], m["message"]) not in seen]
        tool = f"jmztabm-cli {tool_version(text) or '1.0.6'}"
    else:
        exe = (tools / VALIDATOR_21) if tools else None
        if not (exe and exe.is_file() and os.access(exe, os.X_OK)):
            return _result(False, "jmztabm-validator 2.1", [])
        text, finished = _run([str(exe), "-c", str(path), "-level", "Info"])
        messages = parse(text, path)
        tool = f"jmztabm-validator {tool_version(text) or '2.1'}"
    if not finished:
        messages.append(dict(level="error", rule="no_result",
                             message="validator did not finish (no result; jmzTab-m aborts silently on some inputs)"))
    return _result(True, tool, messages)
