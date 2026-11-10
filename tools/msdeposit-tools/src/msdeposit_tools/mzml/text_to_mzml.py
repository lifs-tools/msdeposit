"""text-to-mzml: rebuild an indexed mzML from a ProteoWizard text dump (`msconvert --text`) of chromatograms.

Used when only a text dump of a run exists (e.g. SRM data whose vendor file cannot be converted). Per chromatogram
the dump lists index, id, optional cvParams (chromatogram type, polarity, precursor/product isolation window targets,
collision energy) and the time and intensity arrays as text. Each chromatogram is written with 64-bit float,
zlib-compressed arrays; Q1, Q3 and collision energy come from the cvParams or, failing that, from the id
(`... Q1=<m/z> Q3=<m/z> [CE=<V>]`, e.g. `name##Q1=.. Q3=.. CE=..` or `SRM SIC Q1=.. Q3=..`) and are written as
precursor/product isolation window targets with a collision-induced dissociation activation (collision energy as
magnitude). Polarity is written only when given (--polarity or a polarity cvParam in the dump); it is never inferred
from the sign of the collision energy. Spectrum dumps are not supported.

Round-trip check (always): the mzML is re-read with lxml and the number of chromatograms, every id, the Q1/Q3 targets
and every array value are compared with the dump (values must be identical doubles). The output is written as
<out>.partial and renamed only when the check passes. Output: --out (indexed mzML with SHA-1 fileChecksum).
"""

import base64
import os
import re
import struct
import zlib
from pathlib import Path

from lxml import etree

from .. import __version__
from ..io import InputError, file_hashes, safe_name
from .index import IndexedWriter, escape

NS = "{http://psi.hupo.org/ms/mzml}"
Q_RE = {k: re.compile(rf"(?:^|[\s#]){k}=\s*(-?[\d.]+(?:[eE][-+]?\d+)?)") for k in ("Q1", "Q3", "CE")}
SRM = "selected reaction monitoring chromatogram"
CHROM_TYPES = {"total ion current chromatogram": "MS:1000235", SRM: "MS:1001473",
               "selected ion monitoring chromatogram": "MS:1001472", "basepeak chromatogram": "MS:1000628",
               "ion current chromatogram": "MS:1000810"}
POLARITY = {"positive": ("MS:1000130", "positive scan"), "negative": ("MS:1000129", "negative scan")}
TIME_UNITS = {"minute": ("UO:0000031", "minute"), "second": ("UO:0000010", "second")}
INTENSITY_UNITS = {"number of detector counts": ("MS", "MS:1000131", "number of detector counts"),
                   "counts per second": ("MS", "MS:1000814", "counts per second")}
VENDOR_MODEL = {"thermo": ("MS:1000483", "Thermo Fisher Scientific instrument model"),
                "sciex": ("MS:1000121", "SCIEX instrument model"), "waters": ("MS:1000126", "Waters instrument model"),
                "agilent": ("MS:1000490", "Agilent instrument model"),
                "bruker_tdf": ("MS:1000122", "Bruker Daltonics instrument model"),
                "bruker_baf": ("MS:1000122", "Bruker Daltonics instrument model"),
                "shimadzu": ("MS:1000124", "Shimadzu instrument model"), "unknown": ("MS:1000031", "instrument model")}


def _cv(acc: str, name: str, value: str = "", unit: tuple | None = None, indent: int = 10) -> str:
    u = f' unitCvRef="{unit[0]}" unitAccession="{unit[1]}" unitName="{unit[2]}"' if unit else ""
    ref = acc.split(":")[0]
    return f'{" " * indent}<cvParam cvRef="{ref}" accession="{acc}" name="{name}" value="{_x(value)}"{u}/>\n'


def _x(s: str) -> str:
    return escape(str(s).encode()).decode()


def _number(text: str, where: str) -> str:
    try:
        float(text)
    except ValueError:
        raise InputError(f"{where}: not a number: {text[:40]!r}") from None
    return text


def iter_dump(path: Path, values: bool = True):
    """Yield one dict per chromatogram: index, id, type, polarity, q1, q3, ce (strings or ""), time, intensity
    (lists of floats, or lengths when `values` is False), time_unit, intensity_unit."""
    cur = arr = ctx = None
    with open(path, encoding="utf-8-sig") as fh:
        for lineno, line in enumerate(fh, 1):
            s = line.strip()
            key, _, rest = s.partition(":")
            key, rest = key.strip(), rest.strip()
            where = f"{path.name} line {lineno}"
            if key.startswith("spectrumList") or key == "spectrum":
                raise InputError(f"{where}: spectrum dumps are not supported (chromatograms only)")
            if key == "index":
                if cur:
                    yield _finish(cur, path.name)
                cur = dict(index=int(_number(rest, where).split(".")[0]), id="", type="", polarity="", q1="", q3="",
                           ce="", time=None, intensity=None, time_unit="", intensity_unit="", line=lineno)
                arr = ctx = None
            elif cur is None:
                continue
            elif key == "id":
                cur["id"] = rest
            elif key == "binaryDataArray":
                arr = {}
            elif key in ("precursor", "product"):
                ctx, arr = key, None
            elif key == "cvParam":
                parts = [p.strip() for p in rest.split(",")]
                name = parts[0]
                if arr is not None:
                    if name in ("time array", "intensity array"):
                        arr.update(kind=name.split()[0], unit=parts[1] if len(parts) > 1 else "")
                elif name in CHROM_TYPES:
                    cur["type"] = name
                elif name in ("positive scan", "negative scan"):
                    cur["polarity"] = name.split()[0]
                elif name == "isolation window target m/z" and ctx and len(parts) > 1:
                    cur["q1" if ctx == "precursor" else "q3"] = _number(parts[1], where)
                elif name == "collision energy" and len(parts) > 1:
                    cur["ce"] = _number(parts[1], where)
            elif key == "binary":
                m = re.match(r"\[(\d+)\]\s*(.*)$", rest)
                if not m or arr is None or "kind" not in arr:
                    raise InputError(f"{where}: unsupported or malformed binary array")
                n, items = int(m.group(1)), m.group(2).split()
                if len(items) != n:
                    raise InputError(f"{where}: {len(items)} values, header says {n}")
                data = n
                if values:
                    try:
                        data = [float(v) for v in items]
                    except ValueError:
                        raise InputError(f"{where}: unparsable array value") from None
                cur[arr["kind"]], cur[arr["kind"] + "_unit"] = data, arr["unit"]
                arr = None
    if cur:
        yield _finish(cur, path.name)


def _finish(c: dict, name: str) -> dict:
    where = f"{name}: chromatogram {c['index']}"
    if c["time"] is None or c["intensity"] is None:
        raise InputError(f"{where} lacks a time or intensity array")
    if (len(c["time"]) if isinstance(c["time"], list) else c["time"]) != \
            (len(c["intensity"]) if isinstance(c["intensity"], list) else c["intensity"]):
        raise InputError(f"{where}: time and intensity arrays differ in length")
    if not c["id"]:
        raise InputError(f"{where} has no id")
    for k in ("q1", "q3", "ce"):
        if not c[k] and (m := Q_RE[k.upper()].search(c["id"])):
            c[k] = m.group(1)
    if not c["type"]:
        c["type"] = (SRM if c["q1"] and c["q3"] else
                     "total ion current chromatogram" if c["id"] == "TIC" else "ion current chromatogram")
    return c


def _time_unit(text: str) -> tuple:
    t = text.lower().rstrip("s") or "minute"
    if t not in TIME_UNITS:
        raise InputError(f"unsupported time unit: {text[:30]!r}")
    return ("UO",) + TIME_UNITS[t]


def _array(values: list[float], acc: str, name: str, unit: tuple | None) -> str:
    raw = struct.pack(f"<{len(values)}d", *values)
    enc = base64.b64encode(zlib.compress(raw)).decode()
    return (f'            <binaryDataArray encodedLength="{len(enc)}">\n'
            + _cv("MS:1000523", "64-bit float", indent=14) + _cv("MS:1000574", "zlib compression", indent=14)
            + _cv(acc, name, unit=unit, indent=14) +
            f'              <binary>{enc}</binary>\n            </binaryDataArray>\n')


def chromatogram_xml(i: int, c: dict, polarity: str) -> str:
    pol = c["polarity"] or polarity
    out = [f'        <chromatogram index="{i}" id="{_x(c["id"])}" defaultArrayLength="{len(c["time"])}">\n',
           _cv(CHROM_TYPES[c["type"]], c["type"])]
    if pol:
        out.append(_cv(*POLARITY[pol]))
    if c["q1"]:
        ce = (_cv("MS:1000045", "collision energy", str(abs(float(c["ce"]))), ("UO", "UO:0000266", "electronvolt"),
                  14) if c["ce"] else "")
        out.append('          <precursor>\n            <isolationWindow>\n'
                   + _cv("MS:1000827", "isolation window target m/z", c["q1"], ("MS", "MS:1000040", "m/z"), 14)
                   + '            </isolationWindow>\n            <activation>\n'
                   + _cv("MS:1000133", "collision-induced dissociation", indent=14) + ce
                   + '            </activation>\n          </precursor>\n')
    if c["q3"]:
        out.append('          <product>\n            <isolationWindow>\n'
                   + _cv("MS:1000827", "isolation window target m/z", c["q3"], ("MS", "MS:1000040", "m/z"), 14)
                   + '            </isolationWindow>\n          </product>\n')
    out.append('          <binaryDataArrayList count="2">\n')
    out.append(_array(c["time"], "MS:1000595", "time array", _time_unit(c["time_unit"])))
    out.append(_array(c["intensity"], "MS:1000515", "intensity array", INTENSITY_UNITS.get(c["intensity_unit"])))
    out.append('          </binaryDataArrayList>\n        </chromatogram>\n')
    return "".join(out)


def header_xml(text: Path, run_id: str, n: int, types: list[str], model: tuple[str, str]) -> str:
    sha1 = file_hashes(text, ("sha1",))["sha1"]
    content = "".join(_cv(CHROM_TYPES[t], t, indent=8) for t in types)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<indexedmzML xmlns="http://psi.hupo.org/ms/mzml" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://psi.hupo.org/ms/mzml http://psidev.info/files/ms/mzML/xsd/mzML1.1.2_idx.xsd">\n'
        '  <mzML xmlns="http://psi.hupo.org/ms/mzml" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://psi.hupo.org/ms/mzml http://psidev.info/files/ms/mzML/xsd/mzML1.1.0.xsd" '
        f'id="{_x(run_id)}" version="1.1.0">\n'
        '    <cvList count="2">\n'
        '      <cv id="MS" fullName="Proteomics Standards Initiative Mass Spectrometry Ontology" version="4.1.200" '
        'URI="https://raw.githubusercontent.com/HUPO-PSI/psi-ms-CV/master/psi-ms.obo"/>\n'
        '      <cv id="UO" fullName="Unit Ontology" version="09:04:2014" '
        'URI="https://raw.githubusercontent.com/bio-ontology-research-group/unit-ontology/master/unit.obo"/>\n'
        '    </cvList>\n    <fileDescription>\n      <fileContent>\n' + content + '      </fileContent>\n'
        '      <sourceFileList count="1">\n'
        f'        <sourceFile id="SF1" name="{_x(text.name)}" location="file:///">\n'
        + _cv("MS:1000824", "no nativeID format") + _cv("MS:1000569", "SHA-1", sha1) +
        '          <userParam name="file format" value="ProteoWizard text dump (msconvert --text)" '
        'type="xsd:string"/>\n'
        '        </sourceFile>\n      </sourceFileList>\n    </fileDescription>\n'
        '    <softwareList count="2">\n'
        '      <software id="pwiz_text_export" version="unknown">\n'
        + _cv("MS:1000615", "ProteoWizard software", indent=8) +
        f'      </software>\n      <software id="msdeposit_tools" version="{__version__}">\n'
        + _cv("MS:1000799", "custom unreleased software tool", "msdeposit-tools text-to-mzml", indent=8) +
        '      </software>\n    </softwareList>\n'
        '    <instrumentConfigurationList count="1">\n      <instrumentConfiguration id="IC1">\n'
        + _cv(model[0], model[1], indent=8) +
        '      </instrumentConfiguration>\n    </instrumentConfigurationList>\n'
        '    <dataProcessingList count="1">\n      <dataProcessing id="DP1">\n'
        '        <processingMethod order="1" softwareRef="msdeposit_tools">\n'
        + _cv("MS:1000544", "Conversion to mzML") +
        '        </processingMethod>\n      </dataProcessing>\n    </dataProcessingList>\n'
        f'    <run id="{_x(run_id)}" defaultInstrumentConfigurationRef="IC1" defaultSourceFileRef="SF1">\n'
        f'      <chromatogramList count="{n}" defaultDataProcessingRef="DP1">\n')


def _decode(bda) -> tuple[str, list[float]]:
    accs = {cv.get("accession") for cv in bda.iter(NS + "cvParam")}
    data = zlib.decompress(base64.b64decode(bda.findtext(NS + "binary") or ""))
    kind = "time" if "MS:1000595" in accs else "intensity" if "MS:1000515" in accs else ""
    return kind, list(struct.unpack(f"<{len(data) // 8}d", data))


def _target(el, where: str) -> str:
    node = el.find(f"{NS}{where}/{NS}isolationWindow/{NS}cvParam[@accession='MS:1000827']")
    return node.get("value") if node is not None else ""


def round_trip(text: Path, mzml: Path) -> int:
    """Compare the mzML with the dump; returns the number of values compared, raises RuntimeError on a mismatch."""
    dump = iter_dump(text)
    n = values = 0
    for _, el in etree.iterparse(str(mzml), events=("end",), tag=NS + "chromatogram", huge_tree=True):
        c = next(dump, None)
        if c is None:
            raise RuntimeError("round trip: more chromatograms in the mzML than in the dump")
        got = dict(_decode(b) for b in el.iter(NS + "binaryDataArray"))
        pack = lambda v: struct.pack(f"<{len(v)}d", *v)  # noqa: E731  (bitwise comparison, NaN-safe)
        if el.get("id") != c["id"]:
            raise RuntimeError(f"round trip: chromatogram {n} id differs")
        for k in ("time", "intensity"):
            if pack(got.get(k, [])) != pack(c[k]):
                raise RuntimeError(f"round trip: chromatogram {n} {k} values differ")
        for k, where in (("q1", "precursor"), ("q3", "product")):
            if (float(c[k]) if c[k] else None) != (float(t) if (t := _target(el, where)) else None):
                raise RuntimeError(f"round trip: chromatogram {n} {k.upper()} differs")
        n += 1
        values += len(c["time"]) + len(c["intensity"])
        el.clear(keep_tail=True)
        while el.getprevious() is not None:
            del el.getparent()[0]
    if next(dump, None) is not None:
        raise RuntimeError("round trip: fewer chromatograms in the mzML than in the dump")
    return values


def convert(text: Path, out: Path, *, run_id: str = "", vendor: str = "unknown", polarity: str = "",
            model: tuple[str, str] | None = None) -> dict:
    text, out = Path(text), Path(out)
    meta = list(iter_dump(text, values=False))
    if not meta:
        raise InputError(f"{text.name}: no chromatograms found")
    types = list(dict.fromkeys(c["type"] for c in meta))
    run_id = run_id or safe_name(out.name[: -len(".mzML")] if out.name.lower().endswith(".mzml") else out.stem)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".partial")
    try:
        with open(tmp, "wb") as fo:
            w = IndexedWriter(fo)
            try:
                w.write(header_xml(text, run_id, len(meta), types, model or VENDOR_MODEL[vendor]).encode(), scan=False)
                for i, c in enumerate(iter_dump(text)):
                    w.write(chromatogram_xml(i, c, polarity).encode())
                w.write(b"      </chromatogramList>\n    </run>\n  </mzML>")
                w.finish()
            finally:
                w.close()
        values = round_trip(text, tmp)
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    return dict(chromatograms=len(meta), srm=sum(c["type"] == SRM for c in meta), values=values)


def add_arguments(p):
    p.add_argument("--text", required=True, type=Path, help="ProteoWizard text dump (msconvert --text)")
    p.add_argument("--out", required=True, type=Path, help="output mzML")
    p.add_argument("--run-id", default="", help="mzML/run id (default: the output file stem)")
    p.add_argument("--vendor", choices=sorted(VENDOR_MODEL), default="unknown",
                   help="original instrument vendor; selects the generic instrument model term")
    p.add_argument("--instrument-model", default="", help="instrument model term as 'MS:xxxxxxx|name'")
    p.add_argument("--polarity", choices=("positive", "negative", ""), default="",
                   help="polarity written to every chromatogram without one")


def run(args) -> int:
    if not args.text.is_file():
        raise InputError(f"text dump not found: {args.text.name}")
    model = None
    if args.instrument_model:
        acc, sep, name = args.instrument_model.partition("|")
        if not (sep and re.fullmatch(r"MS:\d{7}", acc.strip()) and name.strip()):
            raise InputError("--instrument-model must look like 'MS:1000xxx|model name'")
        model = (acc.strip(), name.strip())
    r = convert(args.text, args.out, run_id=args.run_id, vendor=args.vendor, polarity=args.polarity, model=model)
    print(f"text-to-mzml: {r['chromatograms']} chromatograms ({r['srm']} SRM), {r['values']} values; round trip ok")
    return 0
