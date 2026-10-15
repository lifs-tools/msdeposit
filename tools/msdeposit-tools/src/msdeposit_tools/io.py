"""Shared helpers: tables, JSON artefacts, hashing, MetaboLights-safe names and check results (CONTRACTS.md §9)."""

import csv
import hashlib
import json
import re
from pathlib import Path

CHUNK = 64 << 20
POL_TOKEN = {"positive": "POS", "negative": "NEG", "alternating": "ALT"}
VENDORS = ("thermo", "sciex", "agilent", "bruker_tdf", "bruker_baf", "waters", "shimadzu", "unknown")
CHECK_STATUSES = ("pass", "warn", "fail", "skipped")


class InputError(Exception):
    """A usage or input problem; the CLI reports it and exits with 1."""


def safe_name(name: str) -> str:
    """MetaboLights allows only .-_A-Za-z0-9 in file names."""
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9.\-_]+", "_", str(name))).strip("_")


def read_table(path: Path, delimiter: str | None = None) -> list[dict]:
    """CSV or TSV (by extension unless given) as a list of dicts; cells stripped, BOM removed."""
    path = Path(path)
    if delimiter is None:
        delimiter = "\t" if path.suffix.lower() in (".tsv", ".txt") else ","
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return [{(k or "").strip(): (v or "").strip() for k, v in row.items()}
                for row in csv.DictReader(fh, delimiter=delimiter)]


def write_table(path: Path, rows: list[dict], fields: list[str] | None = None, delimiter: str = "\t") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = fields or (list(rows[0].keys()) if rows else [])
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, delimiter=delimiter, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def read_json(path: Path, schema: str | None = None) -> dict:
    """Load an artefact; with `schema` ("msdeposit/plan/1") the name and major version must match."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if schema is not None:
        got = data.get("schema", "") if isinstance(data, dict) else ""
        if got.rsplit("/", 1)[0] != schema.rsplit("/", 1)[0] or got.rsplit("/", 1)[-1] != schema.rsplit("/", 1)[-1]:
            raise InputError(f"{Path(path).name}: expected artefact {schema}, found {got or 'none'}")
    return data


def load_run_artefacts(paths: list[Path], schema: str) -> dict[str, dict]:
    """{run: artefact} from files or directories; other artefacts and *.private.json are ignored."""
    files = []
    for p in paths:
        files += sorted(Path(p).rglob("*.json")) if Path(p).is_dir() else [Path(p)]
    out = {}
    for f in files:
        if f.name.endswith(".private.json"):
            continue
        d = read_json(f)
        if not (isinstance(d, dict) and str(d.get("schema", "")).startswith(schema.rsplit("/", 1)[0] + "/")):
            continue
        read_json(f, schema)
        if d["run"] in out:
            raise InputError(f"{schema.split('/')[1]} for run '{d['run']}' given twice")
        out[d["run"]] = d
    return out


def load_metadata(paths: list[Path]) -> dict[str, dict]:
    return load_run_artefacts(paths, "msdeposit/mzml_metadata/1")


def load_finalise(paths: list[Path]) -> dict[str, dict]:
    return load_run_artefacts(paths, "msdeposit/finalise/1")


def write_json(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def file_hashes(path: Path, algorithms=("sha1", "sha256")) -> dict[str, str]:
    hs = {a: hashlib.new(a) for a in algorithms}
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            for h in hs.values():
                h.update(chunk)
    return {a: h.hexdigest() for a, h in hs.items()}


def write_check(outdir: Path, study: str, check: str, status: str, *, blocking: bool = True,
                counts: dict | None = None, summary: str = "", details: list | None = None,
                scope: str = "study", run: str | None = None) -> dict:
    """Write check_<ID>.json (public: numbers and a path-free summary) and, with details, check_<ID>.private.json."""
    if status not in CHECK_STATUSES:
        raise ValueError(f"unknown check status {status}")
    counts = counts or {}
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in counts.values()):
        raise ValueError("check counts must be numbers")
    result = dict(schema="msdeposit/check/1", study=study, check=check, scope=scope, run=run, status=status,
                  blocking=blocking, counts=counts, summary=summary)
    outdir = Path(outdir)
    suffix = f".{safe_name(run)}" if run else ""
    write_json(outdir / f"check_{check}{suffix}.json", result)
    if details:
        write_json(outdir / f"check_{check}{suffix}.private.json", dict(schema="msdeposit/check-details/1",
                                                                         study=study, check=check, run=run,
                                                                         details=details))
    return result


def as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("true", "1", "yes", "y", "on"):
        return True
    if s in ("false", "0", "no", "n", "off", ""):
        return False
    raise InputError(f"not a boolean: {value!r}")
