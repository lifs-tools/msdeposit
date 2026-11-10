"""identifier-scan (checks S1, S2): scan files and file names for denylist tokens (design §8.2; CONTRACTS.md §5).

Every file is streamed in chunks, so multi-GB mzML is fine:
  mzML        only the payload of <binary> elements is blanked (text before and after it on the same line is
              scanned); everything else, including attribute values and the index, is scanned. Hits are located
              in the `header` (before <spectrumList>/<chromatogramList>) or the `body`.
  tabular     ISA (i_/s_/a_), MAF, mzTab, TSV/CSV: line by line, location `cell`. With `--allow-regions contacts`
              (default) the shared contact and author rows of i_Investigation.txt are not scanned (design §4.5).
  other       HTML, mzQC/JSON, logs and anything else as text, location `text`; XML/HTML entities and JSON \\u
              escapes are decoded, text data: URIs (SVG, JSON, HTML) are decoded and scanned, binary ones skipped.
  file names  the path relative to the scanned directory, location `name`.
Undecodable bytes are read as Latin-1, text is NFC-normalised. Tokens across chunk boundaries are found.

S1 self-test (`--self-test`): copies one file per type (mzML, ISA, other tabular, other text) into a temporary
directory under --out-dir (never the system temp dir: the copies may be identifying), plants random canary tokens
(an mzML header attribute, a short code in the run id, a chromatogram id, a userParam on the line of a </binary>,
ISA/table cells incl. a transliterated token, a text file, file names), adds them to the in-memory denylist and
requires that every one is found at its location by the same scan code. If S1 fails, S2 fails as well.

Outputs in --out-dir: hits.tsv (private: file, location, token, context; at most 20 rows per file, location and
token), check_S1.json, check_S2.json (public counts) and their .private.json details.
"""

import base64
import binascii
import codecs
import html
import os
import re
import secrets
import tempfile
import unicodedata
from collections import Counter
from pathlib import Path

from ..io import InputError, write_check, write_table
from .denylist import Entry, Matcher, read_denylist, variants

CHUNK = 8 << 20
CONTEXT = 40
MAX_ROWS = 20
WHOLE_MAX = 64 << 20          # non-mzML files up to this size are read at once (data: URIs decoded)
HIT_FIELDS = ["file", "location", "token", "context"]
TABULAR = (".tsv", ".csv", ".mztab", ".tab")
ALLOWED_I_ROWS = re.compile(r'^"?(?:Study|Investigation) (?:Person|Publication Author List)')
BODY_MARKERS = re.compile(r"<(?:\w+:)?(?:spectrumList|chromatogramList)\b")
TEXT_MIME = re.compile(r"(?i)^(?:text/|image/svg|application/(?:json|xml|xhtml|javascript))")
DATA_URI = re.compile(rb"data:([\w.+/-]*)((?:;[\w.=-]+)*);base64,([A-Za-z0-9+/=\s]*)")


def _latin1(err: UnicodeDecodeError):
    return err.object[err.start:err.end].decode("latin-1"), err.end


codecs.register_error("msdeposit-latin1", _latin1)


def file_type(path: Path) -> str:
    name = path.name.lower()
    if name.endswith((".mzml", ".mzml.gz")):
        return "mzml"
    if name.endswith(".txt") and name[:2] in ("i_", "s_", "a_"):
        return "isa"
    if name.endswith(TABULAR):
        return "tabular"
    return "text"


class BinaryBlanker:
    """Removes the payload of <binary>…</binary> from a byte stream; the tags and all other bytes are kept."""

    OPEN, CLOSE = b"<binary>", b"</binary>"

    def __init__(self):
        self.inside = False
        self.pending = b""

    def feed(self, data: bytes, final: bool = False) -> bytes:
        buf, self.pending, out, i = self.pending + data, b"", [], 0
        while i < len(buf):
            if self.inside:
                j = buf.find(self.CLOSE, i)
                if j < 0:
                    if not final:      # keep a possible partial close tag
                        self.pending = buf[max(i, len(buf) - len(self.CLOSE) + 1):]
                    break
                out.append(self.CLOSE)
                i, self.inside = j + len(self.CLOSE), False
            else:
                j = buf.find(self.OPEN, i)
                if j < 0:
                    end = len(buf) if final else max(i, len(buf) - len(self.OPEN) + 1)
                    out.append(buf[i:end])
                    self.pending = buf[end:]
                    break
                out.append(buf[i:j + len(self.OPEN)])
                i, self.inside = j + len(self.OPEN), True
        return b"".join(out)


class TextDecoder:
    """Bytes → NFC text: UTF-8 with Latin-1 fallback, optional entity / JSON-escape decoding (partial escapes at
    the end of a chunk are carried over)."""

    PARTIAL = re.compile(r"(?:&#?\w{0,10}|\\u?[0-9a-fA-F]{0,3}|\\)$")

    def __init__(self, entities: bool, json_escapes: bool):
        self.dec = codecs.getincrementaldecoder("utf-8")(errors="msdeposit-latin1")
        self.entities, self.json_escapes = entities, json_escapes
        self.pending = ""

    def feed(self, data: bytes, final: bool = False) -> str:
        text = self.pending + self.dec.decode(data, final)
        self.pending = ""
        if (self.entities or self.json_escapes) and not final:
            m = self.PARTIAL.search(text[-16:])
            if m and m.group(0):
                cut = len(text) - len(m.group(0))
                text, self.pending = text[:cut], text[cut:]
        if self.json_escapes and "\\u" in text:
            text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), text)
        if self.entities and "&" in text:
            text = html.unescape(text)
        return unicodedata.normalize("NFC", text)


class StreamScanner:
    """Finds tokens in a text stream fed in pieces. A match is reported once; the end of each window is kept and
    re-scanned with the next piece, so tokens across piece boundaries and look-around checks work."""

    def __init__(self, matcher: Matcher, body_markers: bool = False):
        self.m = matcher
        self.keep = matcher.max_len + CONTEXT + 2
        self.carry, self.offset, self.accepted = "", 0, 0
        self.body_start = None if body_markers else -1

    def feed(self, text: str, final: bool = False) -> list[tuple[str, Entry, str]]:
        """Returns (location, entry, context) with location 'header'/'body' (mzML) or ''."""
        window = self.carry + text
        if self.body_start is None:
            mk = BODY_MARKERS.search(window)
            if mk:
                self.body_start = self.offset + mk.start()
        limit = len(window) if final else max(0, len(window) - self.keep)
        found = []
        for s, e, entry in self.m.finditer(window):
            pos = self.offset + s
            if s < limit and pos >= self.accepted:
                loc = "" if self.body_start == -1 else (
                    "body" if self.body_start is not None and pos >= self.body_start else "header")
                ctx = window[max(0, s - CONTEXT): e + CONTEXT]
                found.append((loc, entry, re.sub(r"\s+", " ", ctx).strip()))
        self.accepted = max(self.accepted, self.offset + limit)
        start = max(0, limit - CONTEXT - 2)
        self.carry = window[start:]
        self.offset += start
        return found


class Hits:
    """Collects hits; keeps at most MAX_ROWS rows per (file, location, token) and counts all."""

    def __init__(self):
        self.rows: list[dict] = []
        self.counts: Counter = Counter()

    def add(self, file: str, location: str, token: str, context: str):
        key = (file, location, token)
        self.counts[key] += 1
        if self.counts[key] <= MAX_ROWS:
            self.rows.append(dict(file=file, location=location, token=token, context=context))

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def _decode_data_uris(data: bytes) -> bytes:
    def sub(m):
        if not TEXT_MIME.match(m[1].decode("ascii", "replace")):
            return b"data:" + m[1] + b";base64,"
        try:
            return b"data:" + m[1] + b"," + base64.b64decode(re.sub(rb"\s+", b"", m[3]), validate=False)
        except (binascii.Error, ValueError):
            return m[0]
    return DATA_URI.sub(sub, data)


def _open(path: Path):
    if path.name.lower().endswith(".gz"):
        import gzip
        return gzip.open(path, "rb")
    return open(path, "rb")


def scan_stream(path: Path, matcher: Matcher, kind: str):
    """Yields (location, entry, context) for mzML and text files."""
    mzml = kind == "mzml"
    blank = BinaryBlanker() if mzml else None
    name = path.name.lower()
    dec = TextDecoder(entities=mzml or name.endswith((".html", ".htm", ".xml", ".svg", ".mzqc")),
                      json_escapes=name.endswith((".json", ".mzqc", ".js")))
    sc = StreamScanner(matcher, body_markers=mzml)
    default_loc = "text"
    with _open(path) as fh:
        if not mzml and not name.endswith(".gz") and path.stat().st_size <= WHOLE_MAX:
            data = fh.read()
            chunks = [_decode_data_uris(data) if b"data:" in data else data]
        else:
            chunks = iter(lambda: fh.read(CHUNK), b"")
        for chunk in chunks:
            if blank:
                chunk = blank.feed(chunk)
            for loc, e, ctx in sc.feed(dec.feed(chunk)):
                yield loc or default_loc, e, ctx
    tail = dec.feed(blank.feed(b"", final=True) if blank else b"", final=True)
    for loc, e, ctx in sc.feed(tail, final=True):
        yield loc or default_loc, e, ctx


def scan_lines(path: Path, matcher: Matcher, allow_regions: bool):
    skip_rows = allow_regions and path.name.startswith("i_")
    with open(path, encoding="utf-8", errors="msdeposit-latin1", newline="") as fh:
        for n, line in enumerate(fh, 1):
            if skip_rows and ALLOWED_I_ROWS.match(line):
                continue
            line = unicodedata.normalize("NFC", line)
            for s, e, entry in matcher.finditer(line):
                ctx = re.sub(r"\s+", " ", line[max(0, s - CONTEXT): e + CONTEXT]).strip()
                yield "cell", entry, f"[line {n}] {ctx}"


def scan_file(path: Path, display: str, matcher: Matcher, hits: Hits, allow_regions: bool = True) -> None:
    for _, e, entry in matcher.finditer(unicodedata.normalize("NFC", display)):
        hits.add(display, "name", entry.token, display)
    kind = file_type(path)
    it = scan_lines(path, matcher, allow_regions) if kind in ("isa", "tabular") else scan_stream(path, matcher, kind)
    for loc, entry, ctx in it:
        hits.add(display, loc, entry.token, ctx)


def iter_files(paths: list[Path], exclude: list[Path] = ()) -> list[tuple[Path, str]]:
    """(file, display name): files given directly → their name; directories → path relative to the directory.
    Symlinks are followed (Nextflow stages inputs as links); excluded directories are skipped."""
    excl = {os.path.realpath(x) for x in exclude}
    out, seen = [], set()
    for p in paths:
        p = Path(p)
        if p.is_file():
            out.append((p, p.name))
            continue
        if not p.is_dir():
            raise InputError(f"not found: {p.name}")
        for dirpath, dirnames, filenames in os.walk(p, followlinks=True):
            real = os.path.realpath(dirpath)
            if real in seen or real in excl:
                dirnames[:] = []
                continue
            seen.add(real)
            dirnames[:] = sorted(d for d in dirnames if os.path.realpath(os.path.join(dirpath, d)) not in excl)
            for f in sorted(filenames):
                fp = Path(dirpath) / f
                if fp.is_file():
                    out.append((fp, fp.relative_to(p).as_posix()))
    return out


def _scan_one(job) -> Hits:
    path, display, matcher, allow_regions = job
    hits = Hits()
    scan_file(path, display, matcher, hits, allow_regions)
    return hits


def scan_each(files: list[tuple[Path, str]], matcher: Matcher, allow_regions: bool = True,
              jobs: int = 1) -> list[Hits]:
    """One Hits per file, in order; with jobs > 1 the files are scanned in parallel processes."""
    tasks = [(p, d, matcher, allow_regions) for p, d in files]
    if jobs > 1 and len(files) > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(min(jobs, len(files))) as ex:
            return list(ex.map(_scan_one, tasks))
    return [_scan_one(t) for t in tasks]


def scan_files(files: list[tuple[Path, str]], matcher: Matcher, allow_regions: bool = True, jobs: int = 1) -> Hits:
    hits = Hits()
    for h in scan_each(files, matcher, allow_regions, jobs):
        hits.rows += h.rows
        hits.counts.update(h.counts)
    return hits


# --- S1 self-test --------------------------------------------------------------------------------------------------
def _canary(prefix: str = "CANARY") -> str:
    return prefix + secrets.token_hex(5).upper()


def _short_code() -> str:
    """4 characters, letters with a digit: a case-sensitive short code."""
    letters = "".join(secrets.choice("BCDFGHJKLMNPQRSTVWXZ") for _ in range(3))
    return letters[:2] + secrets.choice("23456789") + letters[2]


def _excerpt_mzml(src: Path, limit: int = 16 << 20) -> bytes:
    """A large mzML shortened to its head and tail, cut at element boundaries outside <binary> payloads."""
    with _open(src) as fh:
        if not src.name.lower().endswith(".gz") and src.stat().st_size > 2 * limit:
            head = fh.read(limit)
            fh.seek(src.stat().st_size - limit)
            tail = fh.read()
        else:
            head, tail = fh.read(limit), b""
            while chunk := fh.read(limit):
                tail = (tail + chunk)[-limit:]
    if len(tail) < limit:
        return head + tail
    cut = head.rfind(b"</binary>")
    head = head[: cut + len(b"</binary>")] if cut >= 0 else head[: head.rfind(b">") + 1]
    starts = [i for i in (tail.find(b"<chromatogram "), tail.find(b"<spectrum "), tail.find(b"<indexList"))
              if i >= 0]
    return head + b"\n" + (tail[min(starts):] if starts else b"")


def _sub_first(pattern: bytes, data: bytes, insert: bytes) -> bytes | None:
    m = re.search(pattern, data)
    return None if m is None else data[: m.end()] + insert + data[m.end():]


def _plant_cells(text: str, is_investigation: bool, first: str, last: str) -> str | None:
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if i == 0 and not is_investigation or not line.strip():
            continue
        if is_investigation and (ALLOWED_I_ROWS.match(line) or "\t" not in line):
            continue
        cells = line.split("\t")

        def put(c, tok):
            return f'"{tok} {c[1:]}' if c.startswith('"') else f"{tok} {c}"
        cells[0] = put(cells[0], first)
        cells[-1] = put(cells[-1], last) if len(cells) > 1 else f"{cells[-1]} {last}"
        lines[i] = "\t".join(cells)
        return "\n".join(lines)
    return None


def self_test(files: list[tuple[Path, str]], entries: list[Entry], workdir: Path,
              allow_regions: bool = True) -> tuple[bool, list[dict], dict]:
    """Plants canaries in copies of one file per type, scans the copies and returns (ok, plants, counts)."""
    by_type: dict[str, Path] = {}
    for path, _ in sorted(files, key=lambda f: f[0].stat().st_size):
        by_type.setdefault(file_type(path), path)
    plants: list[dict] = []
    canaries: list[Entry] = []

    def canary(kind_loc: str, ftype: str, planted: str, token: str | None = None, cs: bool = False) -> str:
        token = token or planted
        canaries.append(Entry(token, "canary", cs, "S1"))
        plants.append(dict(type=ftype, location=kind_loc, planted=planted, token=token, file=None, found=False))
        return planted

    with tempfile.TemporaryDirectory(prefix="s1_", dir=workdir) as tmp:
        tmp = Path(tmp)
        copies: list[tuple[Path, str]] = []
        for ftype, src in sorted(by_type.items()):
            start = len(plants)
            name_tok = canary("name", ftype, _canary())
            suffix = src.suffix
            if ftype == "mzml":
                data, suffix = _excerpt_mzml(src), ".mzML"     # a .mzML.gz is planted and scanned uncompressed
            else:
                data = src.read_bytes()
            stem = re.sub(r"(?i)\.mzml\.gz$|\.[^.]*$", "", src.name)
            dst = tmp / f"{stem}_{name_tok}{suffix}"
            if ftype == "mzml":
                long_h = canary("header", ftype, _canary(), cs=False)
                short = canary("header", ftype, _short_code(), cs=True)
                long_c = canary("body", ftype, _canary())
                long_u = canary("body", ftype, _canary())
                steps = [(rb'<mzML\b[^>]*?\sid="', long_h.encode() + b"_"),
                         (rb'<run\b[^>]*?\sid="', short.encode() + b"_"),
                         (rb'<chromatogram\b[^>]*?\sid="' if b"<chromatogram " in data else rb'<spectrum\b[^>]*?\sid="',
                          long_c.encode() + b" "),
                         (rb"</binary>" if b"</binary>" in data else rb"<run\b[^>]*>",
                          f'<userParam name="note" value="{long_u}" type="xsd:string"/>'.encode())]
                for k, (pat, ins) in enumerate(steps, 1):
                    new = _sub_first(pat, data, ins)
                    if new is None:
                        plants[start + k]["location"] = "not_planted"
                    else:
                        data = new
                # the token in the denylist is lower case, the planted one upper case: case-insensitive matching
                canaries[start + 1] = Entry(long_h.lower(), "canary", False, "S1")
                plants[start + 1]["token"] = long_h.lower()
            elif ftype in ("isa", "tabular"):
                tok = _canary()
                variant = "Kanarue" + secrets.token_hex(5).upper()
                canary("cell", ftype, tok)
                canary("cell", ftype, variant, token=variant.replace("Kanarue", "Kanarü", 1))
                text = data.decode("utf-8", "msdeposit-latin1")
                new = _plant_cells(text, src.name.startswith("i_"), variant, tok)
                if new is None:
                    new = text + f"\n{variant}\t{tok}\n"
                data = new.encode()
            else:
                tok = canary("text", ftype, _canary())
                m = re.search(rb'[>"]', data[:65536])
                k = m.end() if m else 0
                data = data[:k] + tok.encode() + b" " + data[k:]
            dst.write_bytes(data)
            copies.append((dst, dst.name))
            for p in plants[start:]:
                p["file"] = dst.name
        # variants as identifier-denylist adds them (the transliterated cell canary is found through one)
        extra = [Entry(v, "canary", e.case_sensitive, "S1") for e in canaries for v in variants(e.token)]
        hits = scan_files(copies, Matcher(list(entries) + canaries + extra), allow_regions)
    found = {(r["file"], r["location"], r["token"].lower()) for r in hits.rows}
    for p in plants:
        if p["location"] == "not_planted":
            continue
        tokens = {p["token"].lower()} | {v.lower() for v in variants(p["token"])}
        p["found"] = any((p["file"], p["location"], t) in found for t in tokens)
    tested = [p for p in plants if p["location"] != "not_planted"]
    ok = bool(tested) and all(p["found"] for p in tested)
    counts = dict(file_types=len(by_type), planted=len(tested), found=sum(p["found"] for p in tested),
                  not_planted=len(plants) - len(tested))
    return ok, plants, counts


# --- CLI ----------------------------------------------------------------------------------------------------------
def add_arguments(p):
    p.add_argument("--denylist", required=True, type=Path)
    p.add_argument("--files", required=True, nargs="+", type=Path, help="files or directories (upload set)")
    p.add_argument("--out-dir", type=Path, default=Path("."))
    p.add_argument("--study", required=True)
    p.add_argument("--self-test", action="store_true", help="run the S1 scanner self-test first")
    p.add_argument("--allow-regions", choices=("contacts", "none"), default="contacts",
                   help="'contacts': skip the shared contact/author rows of i_Investigation.txt (default)")
    p.add_argument("--jobs", type=int, default=1, help="files scanned in parallel (default 1)")


def run(args) -> int:
    entries = read_denylist(args.denylist)
    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    files = iter_files(args.files, exclude=[out])
    allow = args.allow_regions == "contacts"
    s1_ok = True
    if args.self_test:
        s1_ok, plants, counts = self_test(files, entries, out, allow)
        write_check(out, args.study, "S1", "pass" if s1_ok else "fail", counts=counts,
                    summary=f"scanner self-test: {counts['found']} of {counts['planted']} canaries found in "
                            f"{counts['file_types']} file types",
                    details=plants)
    matcher = Matcher(entries)
    hits = scan_files(files, matcher, allow, jobs=args.jobs)
    write_table(out / "hits.tsv", hits.rows, HIT_FIELDS)
    by_loc = Counter()
    for (_, loc, _), n in hits.counts.items():
        by_loc[loc] += n
    counts = dict(files=len(files), tokens=len(entries), hits=hits.total,
                  distinct_tokens=len({t.lower() for _, _, t in hits.counts}),
                  files_with_hits=len({f for f, _, _ in hits.counts}),
                  **{f"hits_{k}": by_loc.get(k, 0) for k in ("name", "header", "body", "cell", "text")})
    problems = []
    if not s1_ok:
        problems.append("scanner self-test (S1) failed")
    if not files:
        problems.append("no files to scan")
    if not entries:
        problems.append("denylist is empty")
    status = "fail" if problems or hits.total else "pass"
    summary = (f"{len(files)} files scanned; {hits.total} hits of {counts['distinct_tokens']} distinct tokens in "
               f"{counts['files_with_hits']} files") + ("; " + "; ".join(problems) if problems else "")
    details = [dict(file=f, location=loc, token=t, count=n) for (f, loc, t), n in sorted(hits.counts.items())]
    write_check(out, args.study, "S2", status, counts=counts, summary=summary, details=details)
    print(summary)
    return 0
