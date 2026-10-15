"""mzml-finalise (check C4): rename or anonymise one converted mzML for deposition, then verify it.

Modes (design §6 MZML_FINALISE, CONTRACTS.md §4):
  rename     mzML/@id and run/@id := deposited stem; sourceFile/@location loses its directories (`file:///`; for
             directory formats the vendor folder and its generic subfolder are kept, e.g. `file:///x.d/AcqData`).
             Nothing else changes.
  anonymise  header: instrument serial number (MS:1000529) removed; sourceFile ids SF1.., names <raw_id><ext>
             (generic inner file names of vendor folders are kept unless they hold a denylist token), locations
             neutral; sample ids/names and 'sample name' (MS:1000002) := plan sample; mzML/@id, run/@id := deposited
             stem; references updated; fileDescription/contact removed; header userParams removed except processing
             descriptions in dataProcessing/processingMethod with an empty or numeric value (e.g. ProteoWizard's
             'Thermo/Xcalibur peak picking'); denylist tokens in their names and in non-numeric header cvParam values
             replaced.
             Header element ids other than the above are not rewritten (S2 reports any token left there).
             body (--body-rewrite true, with --denylist): denylist tokens in spectrum/chromatogram ids and spotIDs,
             spectrumRef/externalSpectrumID attributes, userParam names and values and cvParam values are replaced by
             placeholders X1, X2.. (one per token, stable within the file). Numeric values and nativeID keys
             (`scan=`, `Q1=`, `name=`, ...) are never changed; body sourceFileRef attributes follow the SF ids.
  both       --timestamp-policy applies to run/@startTimeStamp: keep, date (`YYYY-MM-DDT00:00:00`: date precision,
             no time zone, still a valid xs:dateTime), drop (attribute removed).
Token matching mirrors the identifier scanner (sensitive.denylist): tokens of <= 4 characters only on alphanumeric
word boundaries, longer ones anywhere; separators (space, '_', '-') match any run of separators; case per token.

Streaming: the header (everything before <spectrumList>/<chromatogramList>) is rewritten in memory. The body is copied
in blocks that end at tag boundaries; a block is rewritten tag by tag only when it holds a token (or a renamed
sourceFileRef), otherwise it is copied verbatim (rename mode, body rewrite off or no token: a header-only rewrite).
Binary payloads are never touched. The offset index is rebuilt from the bytes written; the SHA-1 fileChecksum is new.

Verification (C4, always): binary payload hash and element counts equal in/out; every index offset points at its
element and the idRefs match the element ids; ids unique; indexListOffset and fileChecksum correct; mzML/@id and run/@id
are the deposited stem; an independent re-read with lxml (well-formed, counts, ids, payload hash) agrees. The output is
written as <name>.partial and renamed only when all of this passed.

Outputs in --outdir: <deposited_mzml>, finalise.json (public, counts only), finalise.private.json (replaced values,
old -> new), check_C4.<run>.json (+ .private.json with the problems).
"""

import hashlib
import os
import re
import unicodedata
from collections import Counter
from pathlib import Path

from lxml import etree

from ..io import InputError, as_bool, read_json, read_table, safe_name, write_check, write_json
from .index import (ATTR_RE, ATTRS, BINARY_RE, CHUNK, KINDS, MZML_OPEN_RE, IndexedWriter, MzMLError, Structure, attr,
                    escape, iter_blocks, scan, unescape, wrap_prolog)

NS = "{http://psi.hupo.org/ms/mzml}"
EXT = {"thermo": ".raw", "sciex": ".wiff", "agilent": ".d", "bruker_tdf": ".d", "bruker_baf": ".d", "waters": ".raw",
       "shimadzu": ".lcd", "unknown": ""}
DIR_VENDORS = {"agilent", "bruker_tdf", "bruker_baf", "waters"}
DATA_SUFFIXES = (".wiff.scan", ".wiff2", ".wiff", ".raw", ".lcd", ".d", ".baf", ".tdf", ".tsf", ".txt", ".mzml",
                 ".mzxml", ".mgf")
GENERIC_INNER = re.compile(r"(?i)^_?[A-Za-z][A-Za-z0-9_.-]*\.(dat|idx|sts|inf|txt|xml|bin|cd|cg|tdf|tdf_bin|tsf|"
                           r"tsf_bin|baf|xsd|mcf_idx|mcf|ini|sqlite)$")
GENERIC_SUBDIRS = {"acqdata"}
HEADER_END_RE = re.compile(rb"<(?:spectrumList|chromatogramList)(?=[\s>])|</run>")
HTAG_RE = re.compile(rb"<(/?)([A-Za-z_][\w.:-]*)(" + ATTRS + rb")>")
BODY_TAG_RE = re.compile(rb"<(spectrum|chromatogram|cvParam|userParam|precursor|scan)(?=[\s/>])(" + ATTRS + rb")>")
# (element, attribute) -> how the value is rewritten
BODY_ATTRS = {(b"spectrum", b"id"): "id", (b"chromatogram", b"id"): "id", (b"spectrum", b"spotID"): "text",
              (b"precursor", b"spectrumRef"): "id", (b"scan", b"spectrumRef"): "id",
              (b"precursor", b"externalSpectrumID"): "id", (b"scan", b"externalSpectrumID"): "id",
              (b"cvParam", b"value"): "value", (b"userParam", b"name"): "text", (b"userParam", b"value"): "value"}
NATIVEID_KEY_RE = re.compile(r"((?:^|(?<=\s))[A-Za-z][A-Za-z0-9_]*=)")
SHORT_MAX = 4
BOUNDARY_BEFORE, BOUNDARY_AFTER = r"(?<![^\W_])", r"(?![^\W_])"
MAX_HEADER = 256 << 20
MAX_EXAMPLES = 200
HEADER_KINDS = ("serial_removed", "source_files_renamed", "source_locations_sanitised", "samples_renamed",
                "user_params_removed", "contacts_removed", "timestamps", "ids_set", "header_values_rewritten")


# --- denylist tokens ----------------------------------------------------------------------------------------------
def _nfc(s: str) -> str:
    if s.isascii():
        return s
    try:
        return unicodedata.normalize("NFC", s)
    except (TypeError, ValueError):
        return s


def _key(token: str, case_sensitive: bool) -> str:
    t = re.sub(r"[\s_\-]+", " ", _nfc(token).strip())
    return t if case_sensitive else t.lower()


def _token_rx(token: str) -> str:
    rx = r"[\s_\-]+".join(re.escape(p) for p in re.split(r"[\s_\-]+", token))
    return BOUNDARY_BEFORE + rx + BOUNDARY_AFTER if len(token) <= SHORT_MAX else rx


NUMBER_RE = re.compile(r"\s*[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?\s*")


def _is_number(s: str) -> bool:
    """Decimal numbers only ('nan', 'inf' and the like are text here)."""
    return NUMBER_RE.fullmatch(s) is not None


class Replacer:
    """Replaces denylist tokens by placeholders X1, X2, ... (stable per token within one file)."""

    def __init__(self, rows: list[dict]):
        self.tokens: dict[tuple[bool, str], str] = {}
        for r in rows:
            tok = _nfc(str(r.get("token") or "").strip())
            if tok:
                cs = as_bool(r.get("case_sensitive") or "false")
                self.tokens.setdefault((cs, _key(tok, cs)), tok)
        alts, checks = [], []
        for (cs, _), tok in sorted(self.tokens.items(), key=lambda kv: (-len(kv[1]), kv[1])):
            wrap = (lambda rx: rx) if cs else (lambda rx: f"(?i:{rx})")
            alts.append(wrap(_token_rx(tok)))
            esc = escape(tok.encode()).decode().replace("'", "&apos;")
            checks += [wrap(_token_rx(tok))] + ([wrap(re.escape(esc))] if esc != tok else [])
        self.regex = re.compile("|".join(alts)) if alts else None
        self.check = re.compile("|".join(checks)) if checks else None
        self.placeholders: dict[tuple[bool, str], str] = {}
        self.counts = Counter()
        self._n = 0

    def __bool__(self):
        return self.regex is not None

    def _placeholder(self, text: str) -> str:
        key = (True, _key(text, True))
        if key not in self.tokens:
            key = (False, _key(text, False))
        if key not in self.placeholders:
            while True:
                self._n += 1
                ph = f"X{self._n}"
                if not self.regex.search(ph):
                    break
            self.placeholders[key] = ph
        self.counts[key] += 1
        return self.placeholders[key]

    def sub(self, text: str) -> tuple[str, int]:
        if self.regex is None:
            return text, 0
        norm, total = _nfc(text), 0
        for _ in range(5):     # a replacement can join neighbours into a new match; repeat a few times
            norm, n = self.regex.subn(lambda m: self._placeholder(m.group(0)), norm)
            if not n:
                break
            total += n
        return (norm if total else text), total

    def sub_id(self, text: str) -> tuple[str, int]:
        """Like sub, but nativeID keys (`scan=`, `name=`, ...) and numeric parts stay untouched."""
        parts = NATIVEID_KEY_RE.split(text)
        total = 0
        for i in range(0, len(parts), 2):
            if parts[i].strip() and not _is_number(parts[i]):
                parts[i], n = self.sub(parts[i])
                total += n
        return ("".join(parts) if total else text), total

    def hits(self, block: bytes) -> bool:
        """Quick check of a raw block (binary payloads excluded) before rewriting it tag by tag."""
        text = _nfc(BINARY_RE.sub(b"<binary>", block).decode("utf-8", "surrogateescape"))
        return self.check.search(text) is not None

    def report(self) -> list[dict]:
        return [dict(token=self.tokens.get(k, k[1]), case_sensitive=k[0], placeholder=ph, count=self.counts[k])
                for k, ph in self.placeholders.items()]


def load_denylist(path: Path) -> list[dict]:
    rows = read_table(path, "\t")
    if rows and "token" not in rows[0]:
        raise InputError(f"{path.name}: expected a 'token' column")
    return rows


# --- header -------------------------------------------------------------------------------------------------------
def _dec(b: bytes | None) -> str:
    return "" if b is None else b.decode("utf-8", "replace")


def set_attr(tag: bytes, name: bytes, value: bytes | None) -> bytes:
    """Set (value None: remove) one attribute of a start tag; a missing attribute is added at the end."""
    for m in ATTR_RE.finditer(tag):
        if m.group(1) == name:
            if value is None:
                s = m.start()
                while s > 0 and tag[s - 1:s] in b" \t\r\n":
                    s -= 1
                return tag[:s] + tag[m.end():]
            return tag[:m.start()] + name + b'="' + escape(value) + b'"' + tag[m.end():]
    if value is None:
        return tag
    end = len(tag) - (2 if tag.endswith(b"/>") else 1)
    return tag[:end].rstrip() + b" " + name + b'="' + escape(value) + b'"' + tag[end:]


def _line_span(head: bytes, s: int, e: int) -> tuple[int, int]:
    """Extend [s, e) to the whole line when the element is alone on it."""
    ls = s
    while ls > 0 and head[ls - 1:ls] in b" \t":
        ls -= 1
    if ls == 0 or head[ls - 1:ls] == b"\n":
        if head[e:e + 2] == b"\r\n":
            return ls, e + 2
        if head[e:e + 1] == b"\n":
            return ls, e + 1
    return s, e


def _location_parts(loc: str) -> list[str]:
    loc = re.sub(r"^file:/*", "", loc, flags=re.I)
    return [p for p in re.split(r"[\\/]+", loc.replace("%20", " ")) if p]


def sanitise_location(loc: str) -> str:
    """file:/// plus, for directory formats, the vendor folder and what follows it (rename mode)."""
    parts = _location_parts(loc)
    i = max((k for k, p in enumerate(parts) if re.search(r"(?i)\.(d|raw)$", p)), default=None)
    return "file:///" + ("/".join(parts[i:]) if i is not None else "")


def _anon_source(name: str, loc: str, n: int, raw_id: str, vendor: str, replacer) -> tuple[str, str]:
    ext = EXT.get(vendor, "")
    if vendor in DIR_VENDORS:
        parts = _location_parts(loc)
        sub = f"/{parts[-1]}" if parts and parts[-1].lower() in GENERIC_SUBDIRS else ""
        keep = GENERIC_INNER.match(name) and not (replacer and replacer.regex.search(_nfc(name)))
        return (name if keep else f"file{n}"), f"file:///{raw_id}{ext}{sub}"
    low = name.lower()
    suffix = next((name[len(name) - len(s):] for s in DATA_SUFFIXES if low.endswith(s) and len(name) > len(s)), ext)
    return f"{raw_id}{suffix}", "file:///"


def _close_end(head: bytes, name: bytes, pos: int) -> int:
    m = re.compile(rb"</" + re.escape(name) + rb"\s*>").search(head, pos)
    if not m:
        raise MzMLError(f"unclosed <{name.decode()}> in the mzML header")
    return m.end()


def _kept_user_param(attrs: bytes, stack: list[bytes]) -> bool:
    """The only header userParams kept: processing descriptions written by converters (e.g. 'Thermo/Xcalibur peak
    picking') inside dataProcessing/processingMethod, with an empty or numeric value."""
    value = _dec(attr(attrs, b"value")).strip()
    return bool(stack) and stack[-1] == b"processingMethod" and (not value or _is_number(value))


def rewrite_header(head: bytes, *, mode: str, stem: str, run_plan: dict, policy: str,
                   replacer: Replacer | None) -> tuple[bytes, Counter, dict, dict]:
    """Rewrite the header from <mzML> up to the run content. Returns (header, change counts, private details,
    sourceFile id map old -> new)."""
    anonymise = mode == "anonymise"
    sample = run_plan.get("sample") or stem
    raw_id = run_plan.get("raw_id") or stem
    vendor = run_plan.get("vendor") or "unknown"
    changes = Counter(dict.fromkeys(HEADER_KINDS, 0))
    private = dict(ids={}, source_files=[], samples=[], serials=[], contacts=[], user_params=[], timestamp=None,
                   values=[])
    sf_map: dict[bytes, bytes] = {}
    sample_map: dict[bytes, bytes] = {}
    edits: list[tuple[int, int, bytes]] = []
    stack: list[bytes] = []
    skip_to = n_sf = n_sample = 0
    for m in HTAG_RE.finditer(head):
        if m.start() < skip_to:
            continue
        closing, name, attrs = m.group(1) == b"/", m.group(2), m.group(3)
        if closing:
            if stack and stack[-1] == name:
                stack.pop()
            continue
        selfclosing = attrs.rstrip().endswith(b"/")
        tag = new = m.group(0)
        if anonymise and (name == b"contact" or (name == b"userParam" and not _kept_user_param(attrs, stack))):
            end = m.end() if selfclosing else _close_end(head, name, m.end())
            edits.append((*_line_span(head, m.start(), end), b""))
            skip_to = end
            if name == b"contact":
                changes["contacts_removed"] += 1
                private["contacts"].append([[_dec(attr(t.group(3), b"name")), _dec(attr(t.group(3), b"value"))]
                                            for t in HTAG_RE.finditer(head, m.start(), end)
                                            if t.group(2) != b"contact"])
            else:
                changes["user_params_removed"] += 1
                private["user_params"].append(dict(section=_dec(stack[-1]) if stack else "",
                                                   name=_dec(attr(attrs, b"name")), value=_dec(attr(attrs, b"value"))))
            continue
        if name == b"cvParam" and anonymise:
            acc, value = attr(attrs, b"accession"), attr(attrs, b"value")
            if acc == b"MS:1000529":
                end = m.end() if selfclosing else _close_end(head, name, m.end())
                edits.append((*_line_span(head, m.start(), end), b""))
                skip_to = end
                changes["serial_removed"] += 1
                private["serials"].append(_dec(value))
                continue
            if acc == b"MS:1000002":
                new = set_attr(new, b"value", sample.encode())
            elif replacer and value and not _is_number(_dec(value)):
                text, n = replacer.sub(value.decode("utf-8", "surrogateescape"))
                if n:
                    new = set_attr(new, b"value", text.encode("utf-8", "surrogateescape"))
                    changes["header_values_rewritten"] += n
                    private["values"].append(dict(field="cvParam/@value", old=_dec(value), new=text))
        elif name == b"userParam" and anonymise and replacer:
            text, n = replacer.sub(_dec(attr(attrs, b"name")))
            if n:
                new = set_attr(new, b"name", text.encode())
                changes["header_values_rewritten"] += n
                private["values"].append(dict(field="userParam/@name", old=_dec(attr(attrs, b"name")), new=text))
        elif name == b"sourceFile":
            old_id, sname, loc = (_dec(attr(attrs, k)) for k in (b"id", b"name", b"location"))
            if anonymise:
                n_sf += 1
                new_id = f"SF{n_sf}"
                sf_map[old_id.encode()] = new_id.encode()
                new_name, new_loc = _anon_source(sname, loc, n_sf, raw_id, vendor, replacer)
                for k, v in ((b"id", new_id), (b"name", new_name), (b"location", new_loc)):
                    new = set_attr(new, k, v.encode())
                changes["source_files_renamed"] += 1
            else:
                new_id, new_name, new_loc = old_id, sname, sanitise_location(loc)
                if new_loc != loc:
                    new = set_attr(new, b"location", new_loc.encode())
                    changes["source_locations_sanitised"] += 1
            private["source_files"].append(dict(old_id=old_id, new_id=new_id, old_name=sname, new_name=new_name,
                                                old_location=loc, new_location=new_loc))
        elif name == b"sample" and anonymise:
            n_sample += 1
            new_s = (sample if n_sample == 1 else f"{sample}_{n_sample}").encode()
            old = attr(attrs, b"id") or b""
            sample_map[old] = new_s
            new = set_attr(set_attr(new, b"id", new_s), b"name", new_s)
            changes["samples_renamed"] += 1
            private["samples"].append(dict(old_id=_dec(old), old_name=_dec(attr(attrs, b"name")), new=_dec(new_s)))
        elif name in (b"mzML", b"run"):
            old = attr(attrs, b"id")
            if old != stem.encode():
                new = set_attr(new, b"id", stem.encode())
                changes["ids_set"] += 1
                private["ids"][name.decode()] = _dec(old)
            if name == b"run":
                if anonymise and (ref := attr(attrs, b"sampleRef")) in sample_map:
                    new = set_attr(new, b"sampleRef", sample_map[ref])
                ts = attr(attrs, b"startTimeStamp")
                if ts is not None and policy != "keep":
                    d = re.match(rb"\d{4}-\d{2}-\d{2}", ts.strip())
                    new_ts = d.group(0) + b"T00:00:00" if policy == "date" and d else None
                    new = set_attr(new, b"startTimeStamp", new_ts)
                    changes["timestamps"] += 1
                    private["timestamp"] = dict(old=_dec(ts), new=_dec(new_ts) if new_ts else None)
        if anonymise and sf_map:
            for a in (b"sourceFileRef", b"defaultSourceFileRef") + ((b"ref",) if name == b"sourceFileRef" else ()):
                if (ref := attr(new, a)) in sf_map:
                    new = set_attr(new, a, sf_map[ref])
        if new != tag:
            edits.append((m.start(), m.end(), new))
        if not selfclosing:
            stack.append(name)
    out, pos = [], 0
    for s, e, rep in sorted(edits):
        out += [head[pos:s], rep]
        pos = e
    out.append(head[pos:])
    return b"".join(out), changes, private, sf_map


# --- body ---------------------------------------------------------------------------------------------------------
class BodyRewriter:
    """Block transform: rewrites tokens in selected attributes of body tags; binary payloads are never touched."""

    def __init__(self, replacer: Replacer | None, sf_map: dict[bytes, bytes]):
        self.replacer = replacer or None
        self.sf_map = sf_map
        self.count = 0
        self.fields = Counter()
        self.examples: list[dict] = []
        self._tokens = False

    def __bool__(self):
        return bool(self.replacer) or bool(self.sf_map)

    def __call__(self, block: bytes) -> bytes:
        self._tokens = self.replacer is not None and self.replacer.hits(block)
        if not self._tokens and not (self.sf_map and b"sourceFileRef=" in block):
            return block
        return BODY_TAG_RE.sub(self._tag, block)

    def _tag(self, m) -> bytes:
        tag, name = m.group(0), m.group(1)
        edits = []
        for a in ATTR_RE.finditer(tag):
            an = a.group(1)
            how = BODY_ATTRS.get((name, an)) or ("sfref" if an == b"sourceFileRef" else None)
            if how is None or (how != "sfref" and not self._tokens):
                continue
            old = unescape(a.group(2) if a.group(2) is not None else a.group(3))
            if how == "sfref":
                if (new := self.sf_map.get(old)) is None:
                    continue
            else:
                text = old.decode("utf-8", "surrogateescape")
                if how == "value" and (not text.strip() or _is_number(text)):
                    continue
                text, n = (self.replacer.sub_id if how == "id" else self.replacer.sub)(text)
                if not n:
                    continue
                new = text.encode("utf-8", "surrogateescape")
                field = f"{name.decode()}/@{an.decode()}"
                self.count += n
                self.fields[field] += n
                if len(self.examples) < MAX_EXAMPLES:
                    self.examples.append(dict(field=field, old=_dec(old), new=_dec(new)))
            edits.append((a.start(), a.end(), an + b'="' + escape(new) + b'"'))
        for s, e, rep in reversed(edits):
            tag = tag[:s] + rep + tag[e:]
        return tag


# --- writing and verification -------------------------------------------------------------------------------------
def read_header(fh, chunk: int = CHUNK) -> tuple[bytes, int]:
    """Bytes before the first <spectrumList>/<chromatogramList> (or </run> of an empty run) and their length."""
    parts = []
    for base, block in iter_blocks(fh, chunk):
        if m := HEADER_END_RE.search(block):
            parts.append(block[:m.start()])
            return b"".join(parts), base + m.start()
        parts.append(block)
        if base + len(block) > MAX_HEADER:
            break
    raise MzMLError("no spectrumList or chromatogramList found")


def write_finalised(src: Path, dst: Path, *, mode: str, stem: str, run_plan: dict, policy: str, body_rewrite: bool,
                    replacer: Replacer | None, chunk: int = CHUNK) -> dict:
    with open(src, "rb") as fi:
        head, hdr_end = read_header(fi, chunk)
        if not (m := MZML_OPEN_RE.search(head)):
            raise MzMLError("no <mzML> element")
        new_head, changes, hdr_private, sf_map = rewrite_header(head[m.start():], mode=mode, stem=stem,
                                                               run_plan=run_plan, policy=policy, replacer=replacer)
        body = BodyRewriter(replacer if body_rewrite else None, sf_map)
        source = Structure()
        fi.seek(hdr_end)
        with open(dst, "wb") as fo:
            w = IndexedWriter(fo)
            try:
                w.write(wrap_prolog(head[:m.start()]) + new_head, scan=False)
                for base, block in iter_blocks(fi, chunk, base=hdr_end):
                    end = block.find(b"</mzML>")
                    if end >= 0:
                        block = block[:end + len(b"</mzML>")]
                    source.feed(base, block)
                    w.write(body(block) if body else block)
                    if end >= 0:
                        break
                else:
                    raise MzMLError("input mzML is incomplete (no closing </mzML>)")
                w.finish()
            finally:
                w.close()
    return dict(source=source, changes=changes, header=hdr_private, body=body)


def reread(path: Path) -> dict:
    """Independent re-read with lxml: well-formedness, element counts, id sequences, binary payload hash."""
    counts = dict(spectra=0, chromatograms=0, binary_arrays=0)
    ids = {k: hashlib.sha256() for k in KINDS}
    body = hashlib.sha256()
    doc = {}
    tags = [NS + t for t in ("mzML", "run", "spectrum", "chromatogram", "binaryDataArray", "binary")]
    try:
        for event, el in etree.iterparse(str(path), events=("start", "end"), tag=tags, huge_tree=True):
            local = el.tag[len(NS):]
            if event == "start":
                if local in ("mzML", "run"):
                    doc[local] = el.get("id")
                continue
            if local == "binary":
                body.update((el.text or "").encode())
            elif local == "binaryDataArray":
                counts["binary_arrays"] += 1
            elif local in KINDS:
                counts["spectra" if local == "spectrum" else "chromatograms"] += 1
                ids[local].update((el.get("id") or "").encode() + b"\0")
                el.clear(keep_tail=True)
                parent = el.getparent()
                while el.getprevious() is not None:
                    del parent[0]
    except (etree.XMLSyntaxError, OSError) as e:
        return dict(ok=False, error=f"{e.__class__.__name__}: {e}")
    return dict(ok=True, counts=counts, ids={k: h.digest() for k, h in ids.items()}, body_sha256=body.hexdigest(),
                mzml_id=doc.get("mzML"), run_id=doc.get("run"))


def verify(path: Path, source: Structure, stem: str, chunk: int = CHUNK) -> tuple[dict, list[str], list[str]]:
    """C4 checks of the written file. Returns (results, public issues, private details)."""
    out = scan(path, chunk, file_hashes=True)
    rr = reread(path)
    issues, details = [], []
    if out.body_sha256() != source.body_sha256():
        issues.append("binary payload hash differs")
    if out.counts() != source.counts():
        issues.append("element counts differ")
    if not out.complete:
        issues.append("output incomplete")
    if not out.index_ok:
        issues.append("index invalid")
    if not out.checksum_ok:
        issues.append("checksum invalid")
    # ids duplicated already in the input (e.g. msconvert 'SIM SIC' chromatograms) are reported, not failed;
    # only collisions created by the rewrite fail
    if out.duplicate_ids > source.duplicate_ids or out.missing_ids > source.missing_ids:
        issues.append("duplicate or missing spectrum/chromatogram ids created by the rewrite")
    elif out.duplicate_ids:
        details.append(f"{out.duplicate_ids} duplicate spectrum/chromatogram ids already in the input (kept)")
    if not rr["ok"]:
        issues.append("re-read failed (not well-formed)")
        details.append(rr["error"])
    else:
        if rr["counts"] != out.counts() or rr["body_sha256"] != out.body_sha256():
            issues.append("re-read differs (counts or binary payload)")
        if any(rr["ids"][k] != out.element_ids[k].digest() for k in KINDS):
            issues.append("re-read differs (element ids)")
        if rr["mzml_id"] != stem or rr["run_id"] != stem:
            issues.append("mzML/run id is not the deposited stem")
    res = dict(bytes=out.bytes, sha1=out.hashes["sha1"].hexdigest(), sha256=out.hashes["sha256"].hexdigest(),
               body_sha256_out=out.body_sha256(), element_counts_out=out.counts(), reread_ok=rr["ok"] and not any(
                   i.startswith("re-read") for i in issues), index_ok=out.index_ok, checksum_ok=out.checksum_ok,
               ids_unique=not (out.duplicate_ids or out.missing_ids))
    return res, issues, details


def finalise(src: Path, outdir: Path, plan: dict, run: str, *, mode: str = "rename", policy: str = "keep",
             body_rewrite: bool = True, denylist: list[dict] | None = None, chunk: int = CHUNK) -> tuple[dict, dict]:
    """Write the deposited mzML into `outdir`; returns (finalise.json, finalise.private.json) contents."""
    run_plan = next((r for r in plan.get("runs", []) if r.get("run") == run), None)
    if run_plan is None:
        raise InputError(f"run '{run}' is not in the plan")
    deposited = run_plan.get("deposited_mzml") or ""
    if not deposited.lower().endswith(".mzml") or safe_name(deposited) != deposited:
        raise InputError(f"run '{run}': unusable deposited_mzml in the plan")
    stem = deposited[: -len(".mzML")]
    replacer = Replacer(denylist or []) if mode == "anonymise" else None
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    dst, tmp = outdir / deposited, outdir / (deposited + ".partial")
    dst.unlink(missing_ok=True)
    public = dict(schema="msdeposit/finalise/1", study=plan.get("study"), run=run, deposited_mzml=deposited, mode=mode,
                  timestamp_policy=policy, body_rewrite=bool(body_rewrite and mode == "anonymise"),
                  denylist_tokens=len(replacer.tokens) if replacer else 0, bytes=None, sha1=None, sha256=None,
                  body_sha256_in="", body_sha256_out="", element_counts_in={}, element_counts_out={},
                  header_changes=dict.fromkeys(HEADER_KINDS, 0), body_rewrites=0, reread_ok=False, index_ok=False,
                  checksum_ok=False, ids_unique=False, issues=[], status="fail")
    private = dict(schema="msdeposit/finalise-private/1", study=plan.get("study"), run=run, source_mzml=src.name,
                   deposited_mzml=deposited, header={}, body={}, details=[])
    try:
        w = write_finalised(src, tmp, mode=mode, stem=stem, run_plan=run_plan, policy=policy,
                            body_rewrite=public["body_rewrite"], replacer=replacer, chunk=chunk)
        body: BodyRewriter = w["body"]
        public.update(body_sha256_in=w["source"].body_sha256(), element_counts_in=w["source"].counts(),
                      header_changes=dict(w["changes"]), body_rewrites=body.count)
        private["header"] = w["header"]
        private["body"] = dict(fields=dict(body.fields), tokens=replacer.report() if replacer else [],
                               source_files={_dec(k): _dec(v) for k, v in body.sf_map.items()},
                               examples=body.examples)
        res, issues, details = verify(tmp, w["source"], stem, chunk)
        public.update(res)
        public["issues"] += issues
        private["details"] += details
        if not issues:
            os.replace(tmp, dst)
            public["status"] = "pass"
    except MzMLError as e:
        public["issues"].append("input is not a complete mzML")
        private["details"].append(str(e))
    finally:
        tmp.unlink(missing_ok=True)
    if public["status"] != "pass":
        for k in ("bytes", "sha1", "sha256"):
            public[k] = None
    return public, private


def add_arguments(p):
    p.add_argument("--mzml", required=True, type=Path, help="converted (indexed) mzML")
    p.add_argument("--plan", required=True, type=Path, help="plan.json of the study")
    p.add_argument("--run", required=True, help="run key")
    p.add_argument("--mode", choices=("rename", "anonymise"), default="rename")
    p.add_argument("--timestamp-policy", choices=("keep", "date", "drop"), default="keep")
    p.add_argument("--body-rewrite", default="true", help="anonymise: replace denylist tokens in the body (true|false)")
    p.add_argument("--denylist", type=Path, help="denylist.tsv (token, kind, case_sensitive, origin)")
    p.add_argument("--outdir", type=Path, default=Path("."))


def run(args) -> int:
    if not args.mzml.is_file():
        raise InputError(f"mzML not found: {args.mzml.name}")
    plan = read_json(args.plan, "msdeposit/plan/1")
    denylist = load_denylist(args.denylist) if args.denylist else []
    public, private = finalise(args.mzml, args.outdir, plan, args.run, mode=args.mode, policy=args.timestamp_policy,
                               body_rewrite=as_bool(args.body_rewrite), denylist=denylist)
    write_json(args.outdir / "finalise.json", public)
    write_json(args.outdir / "finalise.private.json", private)
    c = public["element_counts_out"] or public["element_counts_in"]
    ok = public["status"] == "pass"
    write_check(args.outdir, plan.get("study"), "C4", public["status"], scope="run", run=args.run,
                counts=dict(spectra=c.get("spectra", 0), chromatograms=c.get("chromatograms", 0),
                            binary_arrays=c.get("binary_arrays", 0), body_rewrites=public["body_rewrites"],
                            issues=len(public["issues"])),
                summary=f"{args.mode}: {c.get('spectra', 0)} spectra, {c.get('chromatograms', 0)} chromatograms; "
                        + ("verified" if ok else "verification failed: " + ", ".join(public["issues"])),
                details=[{"issue": i} for i in public["issues"]] + [{"detail": d} for d in private["details"]])
    return 0
