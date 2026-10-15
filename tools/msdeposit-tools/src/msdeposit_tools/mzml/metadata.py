"""mzml-metadata (check C2): streaming metadata and run-level QC metrics of one mzML (multi-GB safe).

One pass over the file (no binary decoding) collects
  - integrity: whole-file SHA-1, complete, indexed (offset index valid), SHA-1 fileChecksum
  - file description: file content terms; source files with id, name, location, SHA-1, file and nativeID format
  - instrument: model, serial number, components per configuration, software versions
  - run: id, start timestamp, sample and instrument configuration refs; per spectrum ms level, polarity, peak type,
    RT, TIC, base peak, resolving power, filter string, scan window, ion injection time, activation;
    chromatogram kinds and SRM transitions (id, Q1, Q3, polarity, collision energy)
and derives run-level QC metrics (`_metrics`; the mzQC writer depends on its keys). Chromatogram-only runs take the
RT range from the TIC chromatogram, the only binary array ever decoded.

Outputs: --out metadata.json (msdeposit/mzml_metadata/1, private; CONTRACTS.md §2) and check_C2.<run>.json in
--check-dir (default: the directory of --out). C2 fails when the file is unreadable, incomplete, not indexed, has an
invalid index or checksum, holds neither spectra nor chromatograms, or contradicts --expected-polarity;
`profile spectra present` and `polarity unknown` are reported as issues without failing.
"""

import base64
import hashlib
import re
import struct
import zlib
from collections import Counter, defaultdict
from pathlib import Path

from ..io import InputError, write_check, write_json
from .index import ATTRS, CHUNK, ATTR_RE, Structure, iter_blocks, unescape

TAG_RE = re.compile(rb"<(/?)(cvParam|userParam|spectrum|chromatogram|run|software|instrumentConfiguration|source|"
                    rb"analyzer|detector|sourceFile|fileContent|activation|precursor|product|"
                    rb"referenceableParamGroup)(?=[\s/>])(" + ATTRS + rb")>")
DISSOCIATION_HINT = re.compile(r"dissociation|activation|fragmentation")
# children of MS:1000767 'native spectrum identifier format'; names are read from the file
NATIVEID_ACCESSIONS = {
    "MS:1000768", "MS:1000769", "MS:1000770", "MS:1000771", "MS:1000772", "MS:1000773", "MS:1000774", "MS:1000775",
    "MS:1000776", "MS:1000777", "MS:1000823", "MS:1000824", "MS:1000929", "MS:1001480", "MS:1001508", "MS:1001526",
    "MS:1001528", "MS:1001531", "MS:1001532", "MS:1001559", "MS:1001562", "MS:1001530", "MS:1002303", "MS:1002532",
    "MS:1002818", "MS:1002898", "MS:1003282"}
NOT_MODEL = {"MS:1000529", "MS:1000032"}   # serial number, customization
CHROM_KIND = {"MS:1001473": "srm", "MS:1000235": "tic", "MS:1000628": "bpc", "MS:1001472": "sim"}
FAILING = ("unreadable", "incomplete", "not indexed", "index invalid", "checksum mismatch",
           "no spectra or chromatograms", "polarity mismatch")
POLARITIES = ("positive", "negative", "alternating")


def _attrs(raw: bytes) -> dict[str, str]:
    out = {}
    for k, v1, v2 in ATTR_RE.findall(raw):
        v = v1 or v2
        out[k.decode()] = (unescape(v) if b"&" in v else v).decode("utf8", "replace")
    return out


def _num(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class Scanner:
    def __init__(self):
        self.ctx: list[str] = []            # open element stack (only tracked tags)
        self.in_run = False
        self.instrument_configs: dict[str, dict] = {}
        self.cur_ic = None
        self.cur_component = None
        self.param_groups: dict[str, list] = {}
        self.cur_group = None
        self.software: dict[str, dict] = {}
        self.cur_software = None
        self.source_files: list[dict] = []
        self.file_content: list[list[str]] = []
        self.run: dict = {}
        self.spectra: list[tuple] = []      # (level, rt_s, tic, bpi, empty)
        self.cur = None                     # current spectrum
        self.ch = None                      # current chromatogram
        self.tic_offset: int | None = None
        self.transitions: list[dict] = []
        self.counts = Counter()
        self.filters: Counter = Counter()
        self.resolving_powers: Counter = Counter()
        self.activations: Counter = Counter()
        self.collision_energies: Counter = Counter()
        self.injection_times = defaultdict(list)
        self.windows: list[float] = []
        self.srm_precursors: list[float] = []
        self.chrom = Counter()

    def _end_spectrum(self):
        if self.cur is not None:
            c = self.cur
            self.spectra.append((c.get("level"), c.get("rt"), c.get("tic"), c.get("bpi"), c.get("dal") == 0))
            self.cur = None

    def _end_chromatogram(self):
        c = self.ch
        if c is not None and (c["kind"] == "srm" or (c["q1"] is not None and c["q3"] is not None)):
            self.transitions.append(dict(id=c["id"], q1=c["q1"], q3=c["q3"], polarity=c["polarity"], ce=c["ce"]))
        self.ch = None

    def feed(self, base: int, buf: bytes):
        for m in TAG_RE.finditer(buf):
            closing, tag, raw = m.group(1) == b"/", m.group(2).decode(), m.group(3)
            self_closing = raw.endswith(b"/")
            if closing:
                self._close(tag)
                continue
            if tag == "userParam":
                continue
            a = _attrs(raw)
            if tag == "cvParam":
                self._param(a)
                continue
            if tag == "run":
                self.in_run = True
                self.run = dict(id=a.get("id", ""), start=a.get("startTimeStamp", ""),
                                sample_ref=a.get("sampleRef", ""),
                                default_instrument_configuration_ref=a.get("defaultInstrumentConfigurationRef", ""))
            elif tag == "spectrum":
                self._end_spectrum()
                dal = _num(a.get("defaultArrayLength", "-1"))
                self.cur = {"dal": int(dal) if dal is not None else -1}
                self.counts["spectra"] += 1
            elif tag == "chromatogram":
                self._end_spectrum()
                self.chrom["n"] += 1
                self.ch = dict(id=a.get("id", ""), offset=base + m.start(), kind="", q1=None, q3=None, polarity="",
                               ce=None)
            elif tag == "instrumentConfiguration":
                self.cur_ic = self.instrument_configs.setdefault(a.get("id", ""), {"params": [], "components": []})
            elif tag in ("source", "analyzer", "detector") and self.cur_ic is not None:
                self.cur_component = {"type": tag, "order": a.get("order", ""), "terms": []}
                self.cur_ic["components"].append(self.cur_component)
            elif tag == "software":
                self.cur_software = self.software.setdefault(a.get("id", ""),
                                                             {"version": a.get("version", ""), "terms": []})
            elif tag == "referenceableParamGroup":
                self.cur_group = self.param_groups.setdefault(a.get("id", ""), [])
            elif tag == "sourceFile":
                self.source_files.append(dict(id=a.get("id", ""), name=a.get("name", ""),
                                              location=a.get("location", ""), sha1="", format=["", ""],
                                              nativeid=["", ""]))
            if self_closing:
                self._close(tag)
            else:
                self.ctx.append(tag)

    def _close(self, tag: str):
        if tag == "instrumentConfiguration":
            self.cur_ic = None
        elif tag in ("source", "analyzer", "detector"):
            self.cur_component = None
        elif tag == "software":
            self.cur_software = None
        elif tag == "referenceableParamGroup":
            self.cur_group = None
        elif tag == "spectrum":
            self._end_spectrum()
        elif tag == "chromatogram":
            self._end_chromatogram()
        if self.ctx and self.ctx[-1] == tag:
            self.ctx.pop()

    def _param(self, a: dict):
        acc, name, value = a.get("accession", ""), a.get("name", ""), a.get("value", "")
        top = self.ctx[-1] if self.ctx else ""
        if self.cur is not None:
            self._spectrum_param(acc, name, value, top, a)
        elif self.ch is not None:
            self._chromatogram_param(acc, name, value, top)
        elif not self.in_run:
            if self.cur_group is not None:
                self.cur_group.append([acc, name, value])
            elif self.cur_component is not None:
                self.cur_component["terms"].append([acc, name])
            elif self.cur_ic is not None:
                self.cur_ic["params"].append([acc, name, value])
            elif self.cur_software is not None:
                self.cur_software["terms"].append([acc, name])
            elif top == "sourceFile" and self.source_files:
                sf = self.source_files[-1]
                if acc == "MS:1000569":
                    sf["sha1"] = value.lower()
                elif acc in NATIVEID_ACCESSIONS or "nativeid format" in name.lower():
                    sf["nativeid"] = [acc, name]
                elif name.lower().endswith("format") and not sf["format"][0]:
                    sf["format"] = [acc, name]
            elif top == "fileContent":
                self.file_content.append([acc, name])

    def _chromatogram_param(self, acc: str, name: str, value: str, top: str):
        c = self.ch
        if acc == "MS:1000827" and top in ("precursor", "product"):
            v = _num(value)
            if top == "precursor":
                c["q1"] = v
                if v is not None:
                    self.srm_precursors.append(v)
            else:
                c["q3"] = v
        elif acc in ("MS:1000130", "MS:1000129"):
            self.counts["pos" if acc == "MS:1000130" else "neg"] += 1
            c["polarity"] = "positive" if acc == "MS:1000130" else "negative"
        elif acc in CHROM_KIND:
            c["kind"] = CHROM_KIND[acc]
            self.chrom[c["kind"]] += 1
            if acc == "MS:1000235" and self.tic_offset is None:
                self.tic_offset = c["offset"]
        elif acc == "MS:1000045":
            self.collision_energies[value] += 1
            c["ce"] = _num(value)
        elif top == "activation" and DISSOCIATION_HINT.search(name):
            self.activations[name] += 1

    def _spectrum_param(self, acc: str, name: str, value: str, top: str, a: dict):
        c = self.cur
        if acc == "MS:1000511":
            level = _num(value)
            c["level"] = int(level) if level is not None else None
            key = {1: "ms1", 2: "ms2"}.get(c["level"], "msn" if c["level"] and c["level"] > 2 else "")
            if key:
                self.counts[key] += 1
        elif acc == "MS:1000130":
            self.counts["pos"] += 1
        elif acc == "MS:1000129":
            self.counts["neg"] += 1
        elif acc == "MS:1000127":
            self.counts["centroid"] += 1
        elif acc == "MS:1000128":
            self.counts["profile"] += 1
        elif acc == "MS:1000016":
            rt = _num(value)
            if rt is not None:
                c["rt"] = rt * (60.0 if a.get("unitAccession", "") == "UO:0000031" else 1.0)
        elif acc == "MS:1000285":
            c["tic"] = _num(value)
        elif acc == "MS:1000505":
            c["bpi"] = _num(value)
        elif acc == "MS:1000800":
            self.resolving_powers[value] += 1
        elif acc == "MS:1000512":
            # strip the scan-specific precursor mass so similar scan events collapse
            self.filters[re.sub(r"\d+\.\d+@", "x@", value)] += 1
        elif acc in ("MS:1000501", "MS:1000500"):
            if (v := _num(value)) is not None:
                self.windows.append(v)
        elif acc == "MS:1000927":
            if (v := _num(value)) is not None:
                self.injection_times[c.get("level")].append(v)
        elif acc == "MS:1000045":
            self.collision_energies[value] += 1
        elif top == "activation" and DISSOCIATION_HINT.search(name):
            self.activations[name] += 1
        elif ("mobility" in name or "drift time" in name) and name.endswith("array"):
            self.counts["im_arrays"] += 1


def _metrics(sp: list[tuple]) -> dict:
    out = {}
    rts = [s[1] for s in sp if s[1] is not None]
    if rts:
        out["rt_range_s"] = [min(rts), max(rts)]
        out["ms_run_duration_s"] = max(rts) - min(rts)
    for lvl in (1, 2):
        pts = sorted((s[1], s[2]) for s in sp if s[0] == lvl and s[1] is not None and s[2] is not None)
        if len(pts) > 1:
            out[f"tic_auc_ms{lvl}"] = sum((t2 - t1) * (y1 + y2) / 2 for (t1, y1), (t2, y2) in zip(pts, pts[1:]))
        out[f"empty_ms{lvl}"] = sum(1 for s in sp if s[0] == lvl and (s[4] or s[2] == 0))
    ms1 = sorted(s[1] for s in sp if s[0] == 1 and s[1] is not None)
    diffs = [b - a for a, b in zip(ms1, ms1[1:]) if b > a]
    if diffs:
        out["ms1_freq_fastest_hz"] = 1.0 / min(diffs)
        out["ms1_freq_slowest_hz"] = 1.0 / max(diffs)
    tics = [s[2] for s in sorted((s for s in sp if s[0] == 1 and s[1] is not None and s[2] is not None),
                                 key=lambda s: s[1])]
    out["ms1_jump10"] = sum(1 for a, b in zip(tics, tics[1:]) if a > 0 and b / a > 10)
    out["ms1_fall10"] = sum(1 for a, b in zip(tics, tics[1:]) if a > 0 and b / a < 0.1)
    return out


def _tic_chromatogram_rt(path: Path, offset: int, limit: int = 256 << 20) -> dict:
    """RT range of chromatogram-only (SRM) runs from the time array of the TIC chromatogram starting at `offset`."""
    buf = bytearray()
    with open(path, "rb") as fh:
        fh.seek(offset)
        while b"</chromatogram>" not in buf[-(1 << 20) - 16:] and len(buf) < limit:
            data = fh.read(1 << 20)
            if not data:
                break
            buf += data
    elem = bytes(buf).split(b"</chromatogram>", 1)[0]
    for arr in re.finditer(rb"<binaryDataArray[\s>].*?</binaryDataArray>", elem, re.S):
        body = arr.group(0)
        if b'accession="MS:1000595"' not in body:
            continue
        if b'accession="MS:1000574"' not in body and b'accession="MS:1000576"' not in body:
            return {}       # other compressions (numpress, ...) are not decoded here
        if not (b64 := re.search(rb"<binary>([^<]*)</binary>", body)):
            return {}
        try:
            data = base64.b64decode(b64.group(1))
            if b'accession="MS:1000574"' in body:
                data = zlib.decompress(data)
        except (ValueError, zlib.error):
            return {}
        fmt, size = ("d", 8) if b'accession="MS:1000523"' in body else ("f", 4)
        t = struct.unpack(f"<{len(data) // size}{fmt}", data[: len(data) // size * size])
        if not t:
            return {}
        factor = 60.0 if b'unitAccession="UO:0000031"' in body else 1.0
        return {"rt_range_s": [min(t) * factor, max(t) * factor], "ms_run_duration_s": (max(t) - min(t)) * factor,
                "rt_source": "TIC chromatogram"}
    return {}


def scan(path: Path, chunk: int = CHUNK) -> tuple[dict, Structure]:
    """Metadata of one mzML (CONTRACTS.md §2 without run/issues/status) and its structural scan."""
    s = Scanner()
    st = Structure(body_hash=False)
    sha1 = hashlib.sha1()
    with open(path, "rb") as fh:
        for base, block in iter_blocks(fh, chunk):
            sha1.update(block)
            st.feed(base, block)
            s.feed(base, block)
    s._end_spectrum()
    s._end_chromatogram()
    instrument = {"model": "", "model_accession": "", "serial": "", "configurations": []}
    shared = [p for g in s.param_groups.values() for p in g]
    for ic_id, ic in s.instrument_configs.items():
        for acc, name, value in ic["params"] + shared:
            if acc == "MS:1000529":
                instrument["serial"] = value
            elif not instrument["model"] and acc.startswith("MS:") and acc not in NOT_MODEL and "format" not in name:
                instrument["model"], instrument["model_accession"] = name, acc
        instrument["configurations"].append({"id": ic_id, "components": ic["components"]})
    pol = ("alternating" if s.counts["pos"] and s.counts["neg"] else
           "positive" if s.counts["pos"] else "negative" if s.counts["neg"] else "")
    metrics = _metrics(s.spectra)
    if not s.spectra and s.tic_offset is not None:
        metrics.update(_tic_chromatogram_rt(path, s.tic_offset))
    nativeid = next((sf["nativeid"] for sf in s.source_files if sf["nativeid"][0] or sf["nativeid"][1]), ["", ""])
    counts = {k: s.counts.get(k, 0) for k in ("spectra", "ms1", "ms2", "msn", "centroid", "profile", "pos", "neg",
                                              "im_arrays")}
    return dict(
        bytes=path.stat().st_size, sha1=sha1.hexdigest(), complete=st.complete,
        indexed=st.indexed_root and st.index_ok, checksum_ok=st.checksum_ok,
        nativeid_format={"accession": nativeid[0], "name": nativeid[1]}, file_content=s.file_content,
        instrument=instrument, software=[{"id": k, **v} for k, v in s.software.items()],
        source_files=s.source_files,
        run_info={"id": s.run.get("id", ""), "start": s.run.get("start", ""), "sample_ref": s.run.get("sample_ref", ""),
                  "default_instrument_configuration_ref": s.run.get("default_instrument_configuration_ref", "")},
        counts=counts, chromatograms={k: s.chrom.get(k, 0) for k in ("n", "srm", "tic", "bpc", "sim")},
        polarity=pol,
        mz_window=([min(s.windows), max(s.windows)] if s.windows else
                   [min(s.srm_precursors), max(s.srm_precursors)] if s.srm_precursors else None),
        mz_window_source="scan window" if s.windows else "SRM precursor m/z" if s.srm_precursors else "",
        transitions=s.transitions,
        filter_strings=dict(s.filters.most_common(30)), resolving_powers=dict(s.resolving_powers),
        activations=dict(s.activations), collision_energies=dict(s.collision_energies.most_common(40)),
        injection_time_mean_ms={str(k): sum(v) / len(v) for k, v in s.injection_times.items() if v and k},
        metrics=metrics,
    ), st


def issues(m: dict, st: Structure, expected_polarity: str = "") -> list[str]:
    out = []
    if st.root not in ("mzML", "indexedmzML") or st.mzml_start is None:
        return ["unreadable"]
    if not m["complete"]:
        out.append("incomplete")
    elif not st.indexed_root or st.index_list_pos is None:
        out.append("not indexed")
    else:
        if not m["indexed"]:
            out.append("index invalid")
        if not m["checksum_ok"]:
            out.append("checksum mismatch")
    if not m["counts"].get("spectra") and not m["chromatograms"].get("n"):
        out.append("no spectra or chromatograms")
    if m["counts"].get("profile"):
        out.append("profile spectra present")
    if not m["polarity"]:
        out.append("polarity unknown")
    elif expected_polarity and m["polarity"] != expected_polarity:
        out.append("polarity mismatch")
    return out


def metadata(path: Path, run: str, expected_polarity: str = "", chunk: int = CHUNK) -> dict:
    m, st = scan(path, chunk)
    found = issues(m, st, expected_polarity)
    status = "fail" if any(i in FAILING for i in found) else "pass"
    return dict(schema="msdeposit/mzml_metadata/1", run=run, **m, issues=found, status=status)


def add_arguments(p):
    p.add_argument("--mzml", required=True, type=Path)
    p.add_argument("--run", required=True, help="run key (run sheet)")
    p.add_argument("--out", type=Path, default=Path("metadata.json"))
    p.add_argument("--study", default=None, help="study key, recorded in the check file")
    p.add_argument("--expected-polarity", choices=POLARITIES + ("",), default="",
                   help="polarity from the run sheet or assay; a different mzML polarity fails C2")
    p.add_argument("--check-dir", type=Path, default=None, help="directory of check_C2.<run>.json (default: --out's)")


def run(args) -> int:
    if not args.mzml.is_file():
        raise InputError(f"mzML not found: {args.mzml.name}")
    m = metadata(args.mzml, args.run, args.expected_polarity)
    write_json(args.out, m)
    c, ch = m["counts"], m["chromatograms"]
    write_check(args.check_dir or args.out.parent, args.study, "C2", m["status"], scope="run", run=args.run,
                counts=dict(spectra=c["spectra"], chromatograms=ch["n"], srm_chromatograms=ch["srm"],
                            issues=len(m["issues"])),
                summary=f"{c['spectra']} spectra, {ch['n']} chromatograms, polarity {m['polarity'] or 'unknown'}"
                        + (f"; issues: {', '.join(m['issues'])}" if m["issues"] else ""),
                details=[{"issue": i} for i in m["issues"]])
    return 0
