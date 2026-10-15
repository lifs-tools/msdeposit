"""mzml-index: normalise a foreign mzML to an indexed mzML with correct offsets and SHA-1 fileChecksum (streaming).

The input may be a plain <mzML> document, an indexedmzML with a missing or broken index or checksum, or either of
them gzip-compressed. A valid indexed file is passed through (hard link, else copy). Otherwise the <mzML> element is
copied byte for byte into a new indexedmzML wrapper with an index computed from the bytes written; the result is
re-scanned (offsets, checksum, element counts, binary payload hash) before it replaces --out.

This module also holds the indexedmzML primitives shared by the other mzML steps:
  iter_blocks()   read a stream in large blocks that never split a tag (every block but the last ends before a '<')
  Structure       one streaming pass: element offsets and ids, the offset index, indexListOffset, SHA-1 fileChecksum,
                  the SHA-256 over all <binary> texts (CONTRACTS.md `body_sha256`) and element counts
  IndexedWriter   writes an indexedmzML: records element offsets from the bytes actually written and appends the
                  index, indexListOffset and fileChecksum
Elements are expected in the default (unprefixed) mzML namespace, as written by all common converters.
"""

import gzip
import hashlib
import os
import re
import shutil
import tempfile
from array import array
from pathlib import Path

from ..io import InputError

CHUNK = 64 << 20
KINDS = ("spectrum", "chromatogram")
XML_DECL = b'<?xml version="1.0" encoding="utf-8"?>'
INDEXED_OPEN = (b'<indexedmzML xmlns="http://psi.hupo.org/ms/mzml" '
                b'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
                b'xsi:schemaLocation="http://psi.hupo.org/ms/mzml '
                b'http://psidev.info/files/ms/mzML/xsd/mzML1.1.2_idx.xsd">')
ATTRS = rb'(?:[^>"\']|"[^"]*"|\'[^\']*\')*'          # attribute text of a tag; '>' may occur inside quoted values
ATTR_RE = re.compile(rb'([\w:.-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')
ELEMENT_RE = re.compile(rb"<(spectrum|chromatogram)(?=[\s/>])(" + ATTRS + rb")>")
ROOT_RE = re.compile(rb"<([A-Za-z_][\w.-]*:)?([A-Za-z_][\w.-]*)")
MZML_OPEN_RE = re.compile(rb"<mzML(?=[\s>])")
INDEX_ITEM_RE = re.compile(rb"<index(?=[\s>])(" + ATTRS + rb")>|<offset(?=[\s>])(" + ATTRS + rb")>([^<]*)")
INDEX_LIST_RE = re.compile(rb"<indexList(?=[\s>])")
INDEX_OFFSET_RE = re.compile(rb"<indexListOffset>\s*(\d+)")
BINARY_RE = re.compile(rb"<binary>([^<]*)")
BDA_RE = re.compile(rb"<binaryDataArray(?=[\s>])")
CHECKSUM_TAG = b"<fileChecksum>"
_ENTITY_RE = re.compile(rb"&(#x[0-9a-fA-F]+|#[0-9]+|amp|lt|gt|quot|apos);")
_ENTITIES = {b"amp": b"&", b"lt": b"<", b"gt": b">", b"quot": b'"', b"apos": b"'"}


class MzMLError(ValueError):
    """The input is not a complete, well-structured mzML (a check result, not an internal error)."""


def unescape(value: bytes) -> bytes:
    """XML attribute/text unescaping (predefined entities and character references)."""
    if b"&" not in value:
        return value

    def sub(m):
        e = m.group(1)
        if e.startswith(b"#x"):
            return chr(int(e[2:], 16)).encode()
        if e.startswith(b"#"):
            return chr(int(e[1:])).encode()
        return _ENTITIES[e]
    return _ENTITY_RE.sub(sub, value)


def escape(value: bytes) -> bytes:
    return value.replace(b"&", b"&amp;").replace(b"<", b"&lt;").replace(b">", b"&gt;").replace(b'"', b"&quot;")


def attr(attrs: bytes, name: bytes) -> bytes | None:
    """Unescaped value of one attribute in the attribute text of a tag, None when absent."""
    for m in ATTR_RE.finditer(attrs):
        if m.group(1) == name:
            return unescape(m.group(2) if m.group(2) is not None else m.group(3))
    return None


def open_mzml(path: Path):
    """Binary reader; gzip-compressed files are decompressed on the fly."""
    with open(path, "rb") as fh:
        gz = fh.read(2) == b"\x1f\x8b"
    return gzip.open(path, "rb") if gz else open(path, "rb")


def is_gzip(path: Path) -> bool:
    with open(path, "rb") as fh:
        return fh.read(2) == b"\x1f\x8b"


def iter_blocks(fh, chunk: int = CHUNK, base: int = 0):
    """Yield (offset, block): contiguous blocks covering the rest of the stream, each cut just before a '<' so that
    no tag (and no text node) is split; the last block holds whatever remains."""
    rest = b""
    while data := fh.read(chunk):
        buf = rest + data if rest else data
        cut = buf.rfind(b"<")
        if cut <= 0:
            rest = buf
            continue
        yield base, buf[:cut]
        base += cut
        rest = buf[cut:]
    if rest:
        yield base, rest


class Structure:
    """Streaming structural scan of an mzML. Feed (offset, block) pairs from `iter_blocks` in order."""

    def __init__(self, body_hash: bool = True, file_hashes: bool = False):
        self.root = ""
        self.mzml_start: int | None = None
        self.mzml_end: int | None = None
        self.elements = {k: array("q") for k in KINDS}
        self.element_ids = {k: hashlib.sha256() for k in KINDS}
        self._seen = {k: set() for k in KINDS}
        self.duplicate_ids = 0
        self.missing_ids = 0
        self.index = {k: array("q") for k in KINDS}
        self.index_ids = {k: hashlib.sha256() for k in KINDS}
        self._index_kind = None
        self.binary_arrays = 0
        self.body = hashlib.sha256() if body_hash else None
        self.hashes = {a: hashlib.new(a) for a in ("sha1", "sha256")} if file_hashes else {}
        self.bytes = 0
        self.index_list_pos: int | None = None
        self.index_list_ws: int | None = None       # start of the whitespace run before <indexList
        self.index_list_offset: int | None = None   # declared in <indexListOffset>
        self.checksum = hashlib.sha1()
        self.checksum_found = False
        self.checksum_declared = ""
        self._ws_start: int | None = None
        self._tail = b""

    def feed(self, base: int, block: bytes) -> None:
        for h in self.hashes.values():
            h.update(block)
        self.bytes = base + len(block)
        if not self.checksum_found:
            i = block.find(CHECKSUM_TAG)
            if i < 0:
                self.checksum.update(block)
            else:
                self.checksum.update(block[:i + len(CHECKSUM_TAG)])
                self.checksum_found = True
                self.checksum_declared = block[i + len(CHECKSUM_TAG):].split(b"<", 1)[0].strip().decode(
                    "ascii", "replace").lower()
        if not self.root and (m := ROOT_RE.search(block)):
            self.root = m.group(2).decode()
        if self.mzml_start is None and (m := MZML_OPEN_RE.search(block)):
            self.mzml_start = base + m.start()
        for m in ELEMENT_RE.finditer(block):
            kind = m.group(1).decode()
            id_ = attr(m.group(2), b"id")
            if id_ is None:
                self.missing_ids += 1
                id_ = b""
            self.elements[kind].append(base + m.start())
            self.element_ids[kind].update(id_ + b"\0")
            h = hash(id_)
            if h in self._seen[kind]:
                self.duplicate_ids += 1
            else:
                self._seen[kind].add(h)
        self.binary_arrays += len(BDA_RE.findall(block))
        if self.body is not None:
            for m in BINARY_RE.finditer(block):
                self.body.update(m.group(1))
        if self.mzml_end is None and (i := block.find(b"</mzML>")) >= 0:
            self.mzml_end = base + i + len(b"</mzML>")
        if self.mzml_end is not None:
            self._feed_index(base, block)
        self._tail = (self._tail + block[-256:])[-256:]
        stripped = len(block)
        while stripped and block[stripped - 1] in b" \t\r\n":
            stripped -= 1
        if stripped < len(block):
            self._ws_start = base + stripped if stripped or self._ws_start is None else self._ws_start
        else:
            self._ws_start = None

    def _feed_index(self, base: int, block: bytes) -> None:
        if self.index_list_pos is None and (m := INDEX_LIST_RE.search(block)):
            self.index_list_pos = base + m.start()
            j = m.start()
            while j > 0 and block[j - 1:j] in b" \t\r\n":
                j -= 1
            self.index_list_ws = base + j if j > 0 or self._ws_start is None else self._ws_start
        for m in INDEX_ITEM_RE.finditer(block):
            if m.group(1) is not None:
                self._index_kind = (attr(m.group(1), b"name") or b"").decode()
            elif self._index_kind in self.index:
                try:
                    self.index[self._index_kind].append(int(m.group(3).strip()))
                except ValueError:
                    self.index[self._index_kind].append(-1)
                self.index_ids[self._index_kind].update((attr(m.group(2), b"idRef") or b"") + b"\0")
        if self.index_list_offset is None and (m := INDEX_OFFSET_RE.search(block)):
            self.index_list_offset = int(m.group(1))

    # --- results ---
    @property
    def indexed_root(self) -> bool:
        return self.root == "indexedmzML"

    @property
    def complete(self) -> bool:
        return self._tail.rstrip().endswith(b"</indexedmzML>" if self.indexed_root else b"</mzML>")

    @property
    def index_ok(self) -> bool:
        """Every element is listed with the offset of its start tag and its id; indexListOffset is right."""
        if not (self.indexed_root and self.index_list_pos is not None and self.index_list_offset is not None):
            return False
        if not self.index_list_ws <= self.index_list_offset <= self.index_list_pos:
            return False
        return all(self.index[k] == self.elements[k] and
                   self.index_ids[k].digest() == self.element_ids[k].digest() for k in KINDS)

    @property
    def checksum_ok(self) -> bool:
        return self.checksum_found and self.checksum_declared == self.checksum.hexdigest()

    @property
    def valid(self) -> bool:
        return self.complete and self.index_ok and self.checksum_ok

    def counts(self) -> dict[str, int]:
        return dict(spectra=len(self.elements["spectrum"]), chromatograms=len(self.elements["chromatogram"]),
                    binary_arrays=self.binary_arrays)

    def body_sha256(self) -> str:
        return self.body.hexdigest() if self.body is not None else ""


def scan(path: Path, chunk: int = CHUNK, **kw) -> Structure:
    st = Structure(**kw)
    with open_mzml(path) as fh:
        for base, block in iter_blocks(fh, chunk):
            st.feed(base, block)
    return st


class IndexedWriter:
    """Writes an indexedmzML. Callers write the document up to and including </mzML> with `write` (element offsets
    are taken from the written bytes) and then call `finish` for index, indexListOffset and fileChecksum."""

    def __init__(self, fh):
        self.fh = fh
        self.pos = 0
        self.sha1 = hashlib.sha1()
        self.counts = dict.fromkeys(KINDS, 0)
        self._index = {k: tempfile.TemporaryFile() for k in KINDS}

    def write(self, data: bytes, scan: bool = True) -> None:
        if scan:
            for m in ELEMENT_RE.finditer(data):
                id_ = attr(m.group(2), b"id")
                if id_ is None:
                    raise MzMLError(f"{m.group(1).decode()} element without id")
                self.add(m.group(1).decode(), id_, self.pos + m.start())
        self.fh.write(data)
        self.sha1.update(data)
        self.pos += len(data)

    def add(self, kind: str, id_: bytes, offset: int) -> None:
        self._index[kind].write(b'      <offset idRef="%s">%d</offset>\n' % (escape(id_), offset))
        self.counts[kind] += 1

    def finish(self) -> None:
        kinds = [k for k in KINDS if self.counts[k]] or ["spectrum"]
        self.write(b"\n", scan=False)
        offset = self.pos     # like ProteoWizard: the offset of the indentation of <indexList>
        self.write(b'  <indexList count="%d">\n' % len(kinds), scan=False)
        for k in kinds:
            self.write(b'    <index name="%s">\n' % k.encode(), scan=False)
            tmp = self._index[k]
            tmp.seek(0)
            while data := tmp.read(CHUNK):
                self.write(data, scan=False)
            self.write(b"    </index>\n", scan=False)
        self.write(b"  </indexList>\n  <indexListOffset>%d</indexListOffset>\n  " % offset, scan=False)
        self.write(CHECKSUM_TAG, scan=False)
        self.fh.write(self.sha1.hexdigest().encode() + b"</fileChecksum>\n</indexedmzML>\n")
        self.close()

    def close(self) -> None:
        for tmp in self._index.values():
            tmp.close()


def wrap_prolog(prolog: bytes) -> bytes:
    """The bytes before <mzML>, with an XML declaration and an <indexedmzML> start tag when missing."""
    if b"<indexedmzML" in prolog:
        return prolog
    bom = b"\xef\xbb\xbf" if prolog.startswith(b"\xef\xbb\xbf") else b""
    rest = prolog[len(bom):].strip()
    if not rest.startswith(b"<?xml"):
        rest = XML_DECL + (b"\n" + rest if rest else b"")
    return bom + rest + b"\n" + INDEXED_OPEN + b"\n  "


def link_or_copy(src: Path, dst: Path) -> str:
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        os.link(src, dst)
        return "linked"
    except OSError:
        shutil.copyfile(src, dst)
        return "copied"


def reindex(src: Path, dst: Path, chunk: int = CHUNK) -> dict:
    """Write `dst` as a valid indexedmzML from `src`; returns {"action", "counts"}."""
    src, dst = Path(src), Path(dst)
    if src.resolve() == dst.resolve():
        raise InputError("--out must differ from --mzml")
    st = scan(src, chunk)
    if st.mzml_start is None or st.root not in ("mzML", "indexedmzML"):
        raise InputError("input is not an mzML document")
    if st.mzml_end is None:
        raise InputError("input is incomplete (no closing </mzML>)")
    if st.missing_ids:
        raise InputError(f"{st.missing_ids} spectrum/chromatogram elements without id")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if st.valid and not is_gzip(src):
        return dict(action=link_or_copy(src, dst), counts=st.counts())
    tmp = dst.with_name(dst.name + ".partial")
    try:
        with open_mzml(src) as fi, open(tmp, "wb") as fo:
            w = IndexedWriter(fo)
            try:
                w.write(wrap_prolog(fi.read(st.mzml_start)), scan=False)
                for base, block in iter_blocks(fi, chunk, base=st.mzml_start):
                    end = st.mzml_end - base
                    w.write(block[:end])
                    if end <= len(block):
                        break
                w.finish()
            finally:
                w.close()
        out = scan(tmp, chunk)
        problems = [what for what, bad in (
            ("index", not out.index_ok), ("checksum", not out.checksum_ok),
            ("element counts", out.counts() != st.counts()),
            ("binary payload hash", out.body_sha256() != st.body_sha256()), ("incomplete", not out.complete)) if bad]
        if problems:
            raise RuntimeError("re-indexed output failed verification: " + ", ".join(problems))
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    return dict(action="reindexed", counts=st.counts())


def add_arguments(p):
    p.add_argument("--mzml", required=True, type=Path, help="input mzML (plain, indexed, or gzip-compressed)")
    p.add_argument("--out", required=True, type=Path, help="output indexed mzML")


def run(args) -> int:
    if not args.mzml.is_file():
        raise InputError(f"mzML not found: {args.mzml.name}")
    result = reindex(args.mzml, args.out)
    c = result["counts"]
    print(f"mzml-index: {result['action']} ({c['spectra']} spectra, {c['chromatograms']} chromatograms)")
    return 0
