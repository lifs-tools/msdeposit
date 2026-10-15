"""Helpers for the mzML step tests: plan/denylist artefacts, damaged files, synthetic text dumps, XSD validation.

All data is synthetic (see synthetic.py); the planted identifiers are the ones in `synthetic.PLANTED`.
"""

import os
import re
from pathlib import Path

from lxml import etree

from msdeposit_tools.io import write_json, write_table
from msdeposit_tools.mzml import index
from synthetic import PLANTED

_SCHEMA = None


def plan_for(run="r001", deposited="QC_01_POS.mzML", sample="QC_01", raw_id="S1_RAW_001", vendor="thermo") -> dict:
    """The parts of a deposit plan (CONTRACTS.md §3) that mzml-finalise reads."""
    return {"schema": "msdeposit/plan/1", "study": "S1", "runs": [
        {"run": run, "assay": "POS", "sample": sample, "ms_assay_name": deposited[:-5], "deposited_mzml": deposited,
         "derived_path": f"FILES/DERIVED_FILES/{deposited}", "raw_id": raw_id, "raw_path": "", "polarity": "positive",
         "vendor": vendor, "nativeid_format": {"accession": "", "name": ""}, "source_sha1": ""}]}


def write_plan(path: Path, **kw) -> Path:
    write_json(path, plan_for(**kw))
    return Path(path)


def denylist_rows(*tokens, case_sensitive=False) -> list[dict]:
    return [dict(token=t, kind="user", case_sensitive=str(case_sensitive).lower(), origin="test") for t in tokens]


def write_denylist(path: Path, *tokens, case_sensitive=False) -> Path:
    write_table(path, denylist_rows(*tokens, case_sensitive=case_sensitive),
                ["token", "kind", "case_sensitive", "origin"])
    return Path(path)


def planted_left(data: bytes) -> set[str]:
    """Keys of PLANTED whose value (or, for the path, its URI spelling or any of its directories) is in `data`."""
    left = {k for k, v in PLANTED.items() if v.encode() in data}
    parts = [p for p in re.split(r"[\\/]", PLANTED["path"]) if len(p) > 3]
    if PLANTED["path"].replace("\\", "/").encode() in data or any(p.encode() in data for p in parts):
        left.add("path")
    return left


def plain(data: bytes) -> bytes:
    """The <mzML> document without the indexedmzML wrapper and index."""
    start = data.index(b"<mzML")
    end = data.index(b"</mzML>") + len(b"</mzML>")
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + data[start:end] + b"\n"


def bad_checksum(data: bytes) -> bytes:
    i = data.index(b"<fileChecksum>") + len(b"<fileChecksum>")
    return data[:i] + b"0" * 40 + data[i + 40:]


def bad_offset(data: bytes) -> bytes:
    """Shift the first spectrum offset by one byte (checksum recomputed, so only the index is wrong)."""
    import hashlib
    m = re.search(rb'(<offset idRef="[^"]*">)(\d+)(</offset>)', data)
    data = data[:m.start(2)] + str(int(m.group(2)) + 1).encode() + data[m.end(2):]
    head = data[: data.index(b"<fileChecksum>") + len(b"<fileChecksum>")]
    return head + hashlib.sha1(head).hexdigest().encode() + b"</fileChecksum>\n</indexedmzML>\n"


def edited(tmp_path: Path, data: bytes, name: str = "edited.mzML") -> Path:
    """Write `data` (edited, so offsets may be stale) and return a re-indexed copy."""
    raw = tmp_path / ("raw_" + name)
    raw.write_bytes(data)
    out = tmp_path / name
    index.reindex(raw, out)
    return out


def binaries(data: bytes) -> list[bytes]:
    return re.findall(rb"<binary>([^<]*)</binary>", data)


def xsd_valid(path: Path) -> tuple[bool, list[str]]:
    """Validate against the mzML 1.1 indexed schema shipped with psims (offline)."""
    global _SCHEMA
    if _SCHEMA is None:
        import psims
        xsd = Path(os.path.dirname(psims.__file__)) / "validation" / "xsd" / "mzML1.1.2_idx.xsd"
        _SCHEMA = etree.XMLSchema(etree.parse(str(xsd)))
    ok = _SCHEMA.validate(etree.parse(str(path)))
    return ok, [str(e) for e in _SCHEMA.error_log][:5]


def text_dump(n: int = 3, style: str = "plain", unit: str = "minutes") -> str:
    """A synthetic `msconvert --text` chromatogram dump.

    style "plain": flat lines, ids '<name>##Q1=.. Q3=.. CE=..' (as written by some vendor exports);
    style "nested": indented blocks with a TIC and SRM chromatograms carrying precursor/product cvParams."""
    if style == "plain":
        lines = ["﻿chromatogramList:", "cvParam: "]
        for i in range(n):
            t = " ".join(f"{0.000933 * k:.15g}" for k in range(6))
            y = " ".join(str(v) for v in (0, 3, 9, 21.5, 6.70000008540228E-05, 1e6 + i))
            lines += [f"   index: {i}", f"   id: PC 3{i}:1##Q1=7{i}0.6 Q3=184.07 CE=-14.999", "   binaryDataArray: ",
                      f"   cvParam: time array, {unit}", f"   binary: [6] {t}", "   binaryDataArray: ",
                      "   cvParam: intensity array, number of detector counts", f"   binary: [6] {y}"]
        return "\n".join(lines) + "\n"
    lines = [f"chromatogramList (2 chromatograms):",
             "  chromatogram:", "    index: 0", "    id: TIC", "    defaultArrayLength: 4",
             "    cvParam: total ion current chromatogram",
             "    binaryDataArray:", "      cvParam: 64-bit float", f"      cvParam: time array, {unit}",
             "      binary: [4] 1 2 3 4.5",
             "    binaryDataArray:", "      cvParam: intensity array, number of detector counts",
             "      binary: [4] 10 20 30 40",
             "  chromatogram:", "    index: 1", "    id: SRM SIC Q1=760.6 Q3=184.1 name=PC 34:1",
             "    defaultArrayLength: 3",
             "    cvParam: selected reaction monitoring chromatogram", "    cvParam: positive scan",
             "    precursor:", "      isolationWindow:", "        cvParam: isolation window target m/z, 760.6, m/z",
             "      activation:", "        cvParam: collision-induced dissociation",
             "        cvParam: collision energy, 35, electronvolt",
             "    product:", "      isolationWindow:", "        cvParam: isolation window target m/z, 184.1, m/z",
             "    binaryDataArray:", f"      cvParam: time array, {unit}", "      binary: [3] 1 2 3",
             "    binaryDataArray:", "      cvParam: intensity array, number of detector counts",
             "      binary: [3] 7 8 9"]
    return "\n".join(lines) + "\n"
