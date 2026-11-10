"""Offline MetaboLights validation of an assembled upload set (check C6; CONTRACTS.md §10).

Equivalent of `mtbls local-validate` (metabolights-utils): the study folder is loaded into the MetaboLights study
model, and the pinned validation bundle is evaluated with OPA. Both tools are looked up in `$MSDEPOSIT_TOOLS_DIR`
(`mtbls_validation_bundle.tar.gz`, `opa`). Nothing is written into the study folder; the validation input JSON goes
to a temporary directory.

`validate(study_dir) -> dict` returns `{"available", "tool", "errors", "warnings", "messages"}` and never raises for
validation findings. `templates_from_bundle()` extracts the template data shipped in `data/mtbls_templates.json`
(regenerate with `python -m msdeposit_tools.validate_mtbls --extract-templates <bundle.tar.gz> <out.json>` when the
pinned bundle changes).
"""

import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

BUNDLE = "mtbls_validation_bundle.tar.gz"
OPA = "opa"
QUERY = "data.metabolights.validation.v2.report.complete_report"
LEVELS = {"ERROR": "error", "WARNING": "warning", "INFO": "info", "SUCCESS": "info"}
STUDY_ID = re.compile(r"^(MTBLS|REQ)\d+$")
# MS assay techniques of template version 1.0 (assayFileTypeMappings of the ms-mhd-enabled category)
MS_TECHNIQUES = ("LC-MS", "GC-MS", "GCxGC-MS", "CE-MS", "FIA-MS", "DI-MS", "MALDI-MS", "MS")


def tools_dir() -> Path | None:
    d = os.environ.get("MSDEPOSIT_TOOLS_DIR")
    return Path(d) if d else None


def find_tools(directory: Path | None = None) -> tuple[Path, Path] | None:
    """(bundle, opa) when both exist, else None."""
    directory = directory or tools_dir()
    if directory is None:
        return None
    bundle, opa = Path(directory) / BUNDLE, Path(directory) / OPA
    if bundle.is_file() and opa.is_file() and os.access(opa, os.X_OK):
        return bundle, opa
    return None


def bundle_data(bundle: Path) -> dict:
    with tarfile.open(bundle) as tar:
        member = next(m for m in tar.getmembers() if m.name.lstrip("/") == "data.json")
        return json.load(tar.extractfile(member))["metabolights"]["validation"]["v2"]


def opa_version(opa: Path) -> str:
    try:
        out = subprocess.run([str(opa), "version"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"Version:\s*(\S+)", out)
    return m.group(1) if m else ""


def investigation_study_id(study_dir: Path) -> str:
    inv = study_dir / "i_Investigation.txt"
    if inv.exists():
        for line in inv.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Study Identifier"):
                parts = line.split("\t")
                return parts[1].strip().strip('"') if len(parts) > 1 else ""
    return ""


def _result(available: bool, tool: str, messages: list[dict]) -> dict:
    return {"available": available, "tool": tool,
            "errors": sum(1 for m in messages if m["level"] == "error"),
            "warnings": sum(1 for m in messages if m["level"] == "warning"), "messages": messages}


def _message(v: dict) -> dict:
    text = v.get("violation") or v.get("description") or v.get("title") or ""
    msg = {"level": LEVELS.get(str(v.get("type", "")).upper(), "info"), "rule": v.get("identifier", ""),
           "message": f"{v.get('title', '')}: {text}".strip(": ") if v.get("title") else text,
           "file": v.get("sourceFile", "")}
    if v.get("sourceColumnHeader"):
        msg["column"] = v["sourceColumnHeader"]
    if v.get("values"):
        msg["values"] = [str(x) for x in v["values"][:10]]
    return msg


def validate(study_dir: Path, study_id: str | None = None, tools: Path | None = None, timeout: int = 600) -> dict:
    """Validate an assembled study folder (ISA files at the root, data files under FILES/)."""
    study_dir = Path(study_dir).resolve()
    found = find_tools(tools)
    if found is None:
        return _result(False, "MetaboLights validation bundle + OPA", [
            {"level": "info", "rule": "", "message": "validation bundle or OPA not found in $MSDEPOSIT_TOOLS_DIR"}])
    bundle, opa = found
    try:
        bundle_version = bundle_data(bundle)["configuration"]["version"]
    except (KeyError, StopIteration, tarfile.TarError, json.JSONDecodeError):
        bundle_version = "unknown"
    tool = f"MetaboLights validation bundle {bundle_version}, OPA {opa_version(opa) or 'unknown'}"
    if not (study_dir / "i_Investigation.txt").exists():
        return _result(True, tool, [{"level": "error", "rule": "msdeposit_missing_investigation",
                                     "message": "i_Investigation.txt not found in the study folder", "file": ""}])
    sid = study_id or investigation_study_id(study_dir)
    sid = sid if STUDY_ID.match(sid or "") else "MTBLS1"   # provisional id when no accession is known yet

    from metabolights_utils.provider.local_folder_metadata_collector import LocalFolderMetadataCollector
    from metabolights_utils.provider.study_provider import MetabolightsStudyProvider

    provider = MetabolightsStudyProvider(db_metadata_collector=None,
                                         folder_metadata_collector=LocalFolderMetadataCollector())
    model = provider.load_study(sid, study_path=str(study_dir), connection=None, load_assay_files=True,
                                load_sample_file=True, load_maf_files=True, load_folder_metadata=True,
                                calculate_data_folder_size=False, calculate_metadata_size=False,
                                data_files_path=str(study_dir / "FILES"), data_files_mapping_folder_name="FILES")
    with tempfile.TemporaryDirectory(prefix="msdeposit-mtbls-") as tmp:
        input_path = Path(tmp) / "input.json"
        input_path.write_text(json.dumps(model.model_dump(by_alias=True)), encoding="utf-8")
        cmd = [str(opa), "eval", "--data", str(bundle), QUERY, "-i", str(input_path)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return _result(True, tool, [{"level": "error", "rule": "msdeposit_validator_failed",
                                         "message": f"OPA evaluation timed out after {timeout} s", "file": ""}])
    try:
        if proc.returncode != 0:
            raise ValueError(f"OPA exited with {proc.returncode}")
        value = json.loads(proc.stdout)["result"][0]["expressions"][0]["value"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        return _result(True, tool, [{"level": "error", "rule": "msdeposit_validator_failed",
                                     "message": f"validator output could not be read ({e})", "file": ""}])
    messages = [_message(v) for v in value.get("violations", [])]
    messages.sort(key=lambda m: ({"error": 0, "warning": 1}.get(m["level"], 2), m["rule"], m["file"]))
    return _result(True, tool, messages)


# ---- template extraction (maintenance) -------------------------------------------------------------------------

def _term(t: dict | None) -> list[str]:
    t = t or {}
    return [t.get("term", ""), t.get("termSourceRef", ""), t.get("termAccessionNumber", "")]


def _headers(template: dict) -> list[list]:
    return [[h["columnHeader"], h["columnStructure"], bool(h["required"]), h["defaultValue"]]
            for h in template["headers"]]


def templates_from_bundle(bundle: Path, template_version: str = "1.0") -> dict:
    """The parts of the validation bundle the ISA writer needs: column structures of the MS assay and sample
    templates, protocol definitions, assay types and the ontology source references."""
    d = bundle_data(bundle)
    t = d["templates"]
    conf = t["configuration"]
    version = conf["versions"][template_version]

    def pick(entries: list[dict]) -> dict:
        return next(e for e in entries if e.get("version") == template_version)

    techniques = [x for x in version["assayFileTypeMappings"]["ms-mhd-enabled"] if x in t["assayFileHeaderTemplates"]]
    protocols = {}
    for tech in techniques:
        p = pick(t["protocolTemplates"][tech])
        protocols[tech] = [{"name": n, "type": _term(p["protocolDefinitions"][n]["type"]),
                            "parameters": p["protocolDefinitions"][n]["parameters"]} for n in p["protocols"]]
    sources = {**t["ontologySourceReferenceTemplates"], **t["otherSourceReferenceTemplates"]}
    return {
        "source": f"MetaboLights validation bundle {conf.get('version') or d['configuration']['version']}, "
                  f"template version {template_version} (extracted by msdeposit_tools.validate_mtbls)",
        "bundle_version": d["configuration"]["version"],
        "template_version": template_version,
        "assay": {tech: _headers(pick(t["assayFileHeaderTemplates"][tech])) for tech in techniques},
        "assay_types": {tech: _term(version["activeAssayFileTemplates"][tech]["ontologyTerm"]) for tech in techniques},
        "sample": {name: _headers(pick(t["sampleFileHeaderTemplates"][name]))
                   for name in version["activeSampleFileTemplates"]},
        "protocols": protocols,
        "study_categories": sorted(version["activeStudyCategories"]),
        "licenses": {k: conf["datasetLicenses"][k]["url"] for k in version["activeDatasetLicenses"]},
        "raw_file_extensions": version["rawFileExtensions"],
        "derived_file_extensions": version["derivedFileExtensions"],
        "ontology_sources": {k: [v.get("sourceFile", ""), v.get("sourceVersion", ""), v.get("sourceDescription", "")]
                             for k, v in sorted(sources.items())},
    }


def compact_json(data, indent: int = 0) -> str:
    """indent=1 JSON with lists of scalars (header rows, terms) on one line."""
    pad = " " * (indent + 1)
    if isinstance(data, dict) and data:
        items = [f"{pad}{json.dumps(k, ensure_ascii=False)}: {compact_json(v, indent + 1)}" for k, v in data.items()]
        return "{\n" + ",\n".join(items) + "\n" + " " * indent + "}"
    if isinstance(data, list) and any(isinstance(x, (dict, list)) for x in data):
        return "[\n" + ",\n".join(pad + compact_json(x, indent + 1) for x in data) + "\n" + " " * indent + "]"
    return json.dumps(data, ensure_ascii=False)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 3 and argv[0] == "--extract-templates":
        Path(argv[2]).write_text(compact_json(templates_from_bundle(Path(argv[1]))) + "\n", encoding="utf-8")
        return 0
    if len(argv) == 1:
        print(json.dumps(validate(Path(argv[0])), indent=1))
        return 0
    print("usage: python -m msdeposit_tools.validate_mtbls <study_dir> | "
          "--extract-templates <bundle.tar.gz> <out.json>", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
