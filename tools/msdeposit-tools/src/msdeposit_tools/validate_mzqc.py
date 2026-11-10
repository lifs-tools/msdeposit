"""mzQC validation (check C8; CONTRACTS.md §10): JSON schema (syntax) and pymzqc semantic checks, offline.

`validate(path) -> dict` never raises for findings.
  - syntax: the mzQC 1.0.0 JSON schema bundled in `data/mzqc_schema.json` (HUPO-PSI/mzQC tag v1.0.0, CC BY 4.0);
    pymzqc's own SyntaxCheck downloads the schema and is therefore not used
  - semantics: `mzqc.SemanticCheck` with the PSI-MS ontology loaded from a local copy instead of the URI declared in
    the file: `$MSDEPOSIT_TOOLS_DIR/psi-ms.obo` when present, else the copy vendored with psims. Other vocabularies
    are loaded from their URI (needs network; a failure is reported as an error by pymzqc)
pymzqc reports every CV term without `description` as a warning ("Used CVTerm without definition"). Descriptions are
optional in mzQC 1.0 and the writer omits them on purpose (a text that differs from the CV definition is an error),
so these are reported as `info`. Identical messages (one per run) are reported once.
"""

import json
import os
import re
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

SCHEMA = Path(__file__).parent / "data" / "mzqc_schema.json"
LEVELS = {"ERROR": "error", "WARNING": "warning", "INFO": "info"}
AS_INFO = ("Used CVTerm without definition",)


def _result(available: bool, tool: str, messages: list[dict]) -> dict:
    return dict(available=available, tool=tool, errors=sum(1 for m in messages if m["level"] == "error"),
                warnings=sum(1 for m in messages if m["level"] == "warning"), messages=messages)


@lru_cache(maxsize=1)
def _schema_validator():
    import jsonschema
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker())


def local_psi_ms() -> Path | None:
    d = os.environ.get("MSDEPOSIT_TOOLS_DIR")
    if d and (Path(d) / "psi-ms.obo").is_file():
        return Path(d) / "psi-ms.obo"
    try:
        p = Path(str(files("psims.controlled_vocabulary.vendor") / "psi-ms.obo.gz"))
    except ModuleNotFoundError:
        return None
    return p if p.is_file() else None


def syntax(doc) -> list[dict]:
    return [dict(level="error", rule="mzqc_schema",
                 message=e.message.partition("\n")[0] + " @ /" + "/".join(str(k) for k in e.path))
            for e in _schema_validator().iter_errors(doc)]


def semantics(text: str) -> list[dict]:
    from mzqc import MZQCFile
    from mzqc.SemanticCheck import SemanticCheck
    obj = MZQCFile.JsonSerialisable.from_json(text)
    psi_ms = local_psi_ms()
    for cv in obj.controlledVocabularies:
        if psi_ms and ("psi-ms" in cv.uri.lower() or "mass spectrometry" in cv.name.lower()):
            cv.uri = str(psi_ms)
    check = SemanticCheck(obj)
    check.validate(load_local=True)
    hide = [str(psi_ms)] if psi_ms else []
    out = {}
    for category, issues in check.items():
        for i in issues:
            message = f"{i.name}: {i.message}"
            for h in hide:
                message = message.replace(h, "psi-ms.obo")
            level = "info" if i.name in AS_INFO else LEVELS.get(str(i.severity).upper(), "error")
            rule = "mzqc_" + re.sub(r"\W+", "_", category).strip("_")
            out.setdefault((level, rule, message), dict(level=level, rule=rule, message=message))
    return list(out.values())


def validate(path: Path) -> dict:
    try:
        import mzqc
        tool = f"pymzqc {getattr(mzqc, '__version__', '') or _pkg_version()}".strip()
    except ImportError:
        return _result(False, "pymzqc", [])
    text = Path(path).read_text(encoding="utf-8")
    try:
        doc = json.loads(text)
    except ValueError as e:
        return _result(True, tool, [dict(level="error", rule="mzqc_json", message=f"not JSON: {e}")])
    messages = syntax(doc)
    if not messages:
        try:
            messages += semantics(text)
        except Exception as e:  # pymzqc raises on structures it cannot map
            messages.append(dict(level="error", rule="mzqc_semantic", message=f"semantic check failed: "
                                                                              f"{e.__class__.__name__}: {e}"))
    return _result(True, tool, messages)


def _pkg_version() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("pymzqc")
    except PackageNotFoundError:
        return ""
