"""vendor-metadata: identifier candidates and the instrument-method dump of one vendor raw file or directory
(design §6 VENDOR_METADATA; CONTRACTS.md §5). Private outputs only.

Only metadata members are read, never spectra:
  thermo      .raw: the file header (creator/modifier logons) and UTF-16 strings of the first MAX_STRINGS bytes
              (sample info, paths, embedded instrument method)
  sciex       .wiff: UTF-16 and ASCII strings of the header region (first MAX_STRINGS bytes; spectra are in .wiff.scan)
  shimadzu    .lcd: as sciex (compound file with text streams)
  bruker_tdf  .d: analysis.tdf table GlobalMetadata (sqlite, opened read-only and immutable), SampleInfo.xml,
              *.m/*.method, *.m/*.xml
  bruker_baf  .d: SampleInfo.xml, *.m/*.method, *.m/*.xml
  waters      .raw directory: _HEADER.TXT, _extern.inf, _inlet.inf
  agilent     .d: AcqData/*.xml (incl. Devices.xml, sample info), AcqData/*.m/*.xml
Members larger than MAX_MEMBER bytes are skipped; members whose real path lies outside the raw path (symlinks)
are never read.

Candidates (`candidates.tsv`: token, field, member) are extracted from: Windows and UNC paths and their components,
DOMAIN\\account names, e-mail addresses, keyed fields (operator/user/creator → person, logon/account, computer/host,
serial numbers, instrument names, lab/institute/company, sample list/sequence/batch/project names, free-text
descriptions and comments), lab/institute names in free text, the raw file stem and method names. Filtering of
generic words happens in identifier-denylist, so candidates.tsv shows everything that was harvested.

Outputs: <outdir>/<raw_id>/candidates.tsv and <outdir>/<raw_id>/method/ (method_strings.txt for thermo/sciex/
shimadzu, copies of the method members otherwise, GlobalMetadata.tsv for bruker_tdf).
"""

import fnmatch
import re
import shutil
import sqlite3
import sys
from pathlib import Path
from urllib.request import pathname2url

from ..io import VENDORS, InputError, safe_name, write_table
from .denylist import words

MAX_STRINGS = 48 << 20
MAX_MEMBER = 50 << 20
MAX_MEMBERS = 500
MAX_VALUE = 80
THERMO_HEADER = 1356           # magic, signature, version and the two audit tags (creator/modifier) + description
CAND_FIELDS = ["token", "field", "member"]
# printable ASCII and Latin-1 letters ('ü' is 0xFC 0x00 in UTF-16LE)
UTF16 = re.compile(rb"(?:[\x20-\x7e\t\xa0-\xff]\x00){5,}")
UTF16_SHORT = re.compile(rb"(?:[\x20-\x7e\xa0-\xff]\x00){2,}")
ASCII = re.compile(rb"[\x20-\x7e\t\xa0-\xff]{6,}")

MEMBERS = {
    "bruker_tdf": ["SampleInfo.xml", "*.m/*.method", "*.m/*.xml"],
    "bruker_baf": ["SampleInfo.xml", "*.m/*.method", "*.m/*.xml"],
    "waters": ["_HEADER.TXT", "_extern.inf", "_inlet.inf"],
    "agilent": ["AcqData/*.xml", "AcqData/*.m/*.xml"],
}
STRING_VENDORS = {"thermo": False, "sciex": True, "shimadzu": True}     # value: also ASCII strings

KEY_FIELDS = [
    ("person", r"operator(name)?|user(name|id)?|analyst|acquiredby|createdby|modifiedby|lastmodifiedby|savedby|"
               r"author|creator|owner|submitter|signedby|reviewedby|approvedby|investigator|contact(name)?|"
               r"responsible|scientist|whocreated(id)?|whomodified(id)?"),
    ("account", r"logon|login|account(name)?|domain|whocreatedlogon|whomodifiedlogon|windowsuser"),
    ("host", r"computer(name)?|machine(name)?|host(name)?|workstation|pcname|nodename|acquisitioncomputer"),
    ("serial", r"(instrument|digitizer|device|system|module|detector)?serial(number|no)?|sn"),
    ("instrument_name", r"instrument(name|id)?|systemname|devicename"),
    ("organisation", r"lab(oratory)?(name)?|institut(e|ion)|company|organi[sz]ation|department|site|facility|"
                     r"customer"),
    ("sequence", r"samplelist|sequence(name|file)?|batch(name|file)?|project(name)?|study(name)?|jobcode|"
                 r"worklist|sampleid|samplename|acquiredname|datafile(name)?|filename|analysisname"),
    ("free_text", r"description|comments?|notes?|remarks?|info|usertext\d*|text\d*|sample(description|comment)"),
    ("path", r"(method|data|result|sequence|tune|processing|acquisition)?(path|directory|folder|dir|file)|"
             r"methodname|method"),
]
KEY_RX = [(f, re.compile(rx)) for f, rx in KEY_FIELDS]
WHOLE_VALUE_FIELDS = {"person", "account", "host", "serial", "instrument_name", "organisation", "sequence"}

KV_LINE = re.compile(r"^[ \t]*(?:\$\$[ \t]*)?([A-Za-z][\w /().#-]{0,40}?)[ \t]*(?::|=|\t)[ \t]*(\S.*?)[ \t]*$",
                     re.MULTILINE)
WIN_PATH = re.compile(r"(?<![\w\\])[A-Za-z]:[\\/][^\"<>|*?\r\n\t;,=\x00]{1,260}")
UNC_PATH = re.compile(r"(?<![\w\\])\\\\([A-Za-z0-9._-]{2,63})\\([^\"<>|*?\r\n\t;,=\x00]{1,260})")
ACCOUNT = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9_-]{1,30})\\([A-Za-z][A-Za-z0-9_.-]{1,40})\b")
ACCOUNT_LINE = re.compile(r"(?i)creat|modif|\bby\b|user|computer|operator|logon|login|account|owner|author|domain")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
MAIL_PROVIDERS = {"gmail", "googlemail", "outlook", "hotmail", "yahoo", "web", "gmx", "mail", "icloud", "protonmail",
                  "live", "aol", "example"}
ORG = re.compile(r"\b((?:[A-ZÀ-Ý][\w&'.-]*[ \t]+){0,3}(?:Lab|Labs|Laboratory|Laboratories|Institute|Institut|"
                 r"University|Universität|Universitaet|Université|Universidad|Università|Hospital|Klinikum|Clinic|"
                 r"Faculty|Department|Dept|GmbH|Inc|Ltd|Corp)\b(?:[ \t]+(?:of|for|für|de|di)[ \t]+"
                 r"(?:[A-ZÀ-Ý][\w-]*[ \t]*){1,3})?)")
INSTRUMENT_ID = re.compile(r"#\s*([A-Za-z0-9][\w-]{2,40})")


def classify_key(key: str) -> str | None:
    k = re.sub(r"[^a-z0-9]", "", key.lower())
    if not k:
        return None
    return next((f for f, rx in KEY_RX if rx.fullmatch(k)), None)


class Candidates:
    def __init__(self):
        self.rows: dict[tuple[str, str, str], dict] = {}

    def add(self, token: str, field: str, member: str):
        t = re.sub(r"\s+", " ", (token or "").strip().strip(".:;,'\"()[]{}<>")).strip()
        numeric = re.fullmatch(r"[\W\d_]+", t)
        if len(t) < 2 or len(t) > 260 or numeric and not (field == "serial" and sum(c.isdigit() for c in t) >= 5):
            return
        self.rows.setdefault((t, field, member), dict(token=t, field=field, member=member))

    def add_value(self, value: str, field: str, member: str):
        """A field value: the whole value (if short) and its words."""
        v = re.sub(r"\s+", " ", value.strip())
        if field in WHOLE_VALUE_FIELDS and len(v) <= MAX_VALUE:
            self.add(v, field, member)
        if field == "serial":
            return
        for w in words(v):
            self.add(w, field, member)

    def add_path(self, path: str, member: str, field: str = "path"):
        parts = [x.strip() for x in re.split(r"[\\/]+", path.strip()) if x.strip()]
        parts = [x for x in parts if not re.fullmatch(r"[A-Za-z]:", x)]
        if parts:
            parts[-1] = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", parts[-1])
        for comp in parts:
            if len(comp) <= MAX_VALUE:
                self.add(comp, field, member)
            for w in words(comp):
                self.add(w, field, member)

    def result(self) -> list[dict]:
        return sorted(self.rows.values(), key=lambda r: (r["member"], r["field"], r["token"].lower()))


def text_candidates(c: Candidates, text: str, member: str, keyed: bool = True):
    """Paths, accounts, e-mails, keyed fields and lab/institute names in free text."""
    for m in WIN_PATH.finditer(text):
        c.add_path(m.group(0).rstrip(" ."), member)
    for m in UNC_PATH.finditer(text):
        c.add(m[1], "host", member)
        c.add_path(m[2].rstrip(" ."), member)
    for line in text.splitlines():
        if "\\" in line and ACCOUNT_LINE.search(line):
            for dom, acc in ACCOUNT.findall(line):
                if not re.fullmatch(r"[A-Za-z]", dom):
                    c.add(dom, "host", member)
                    c.add_value(acc, "account", member)
    for m in EMAIL.finditer(text):
        email = m.group(0).strip(".")
        c.add(email, "email", member)
        local, _, domain = email.partition("@")
        for w in words(local):
            c.add(w, "email", member)
        for label in domain.split(".")[:-1]:
            if label.lower() not in MAIL_PROVIDERS:
                c.add(label, "email", member)
    if keyed:
        for m in KV_LINE.finditer(text):
            key_value(c, m[1], m[2], member)
    for m in ORG.finditer(text):
        c.add_value(m[1], "organisation", member)


def key_value(c: Candidates, key: str, value: str, member: str):
    field = classify_key(key)
    value = (value or "").strip()
    if not field or not value:
        return
    if field == "path":
        c.add_path(value, member)
    elif field == "free_text":
        text_candidates(c, value, member, keyed=False)
    else:
        if "\\" in value and field in ("person", "account"):
            dom, _, acc = value.rpartition("\\")
            c.add(dom, "host", member)
            value = acc
        c.add_value(value, field, member)
        if field == "instrument_name":
            for sid in INSTRUMENT_ID.findall(value):
                c.add(sid, "serial", member)
        if re.search(r"[\\/]", value) and field != "person":
            c.add_path(value, member)


# --- members -------------------------------------------------------------------------------------------------------
def ci_glob(base: Path, pattern: str) -> list[Path]:
    """Case-insensitive glob of 'a/*.b' patterns without following directory symlinks outside `base`."""
    level = [base]
    for part in pattern.split("/"):
        nxt = []
        for d in level:
            try:
                entries = sorted(d.iterdir())
            except (NotADirectoryError, FileNotFoundError, PermissionError):
                continue
            nxt += [e for e in entries if fnmatch.fnmatch(e.name.lower(), part.lower())]
        level = nxt
    return level


def inside(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except (ValueError, OSError):
        return False


def read_head(path: Path, limit: int) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(limit)


def binary_strings(data: bytes, ascii_too: bool) -> list[str]:
    """UTF-16LE (and ASCII/Latin-1) strings in order of appearance; only directly repeated lines are collapsed,
    so repeated settings of later scan events stay visible."""
    found = [(m.start(), m.group().decode("utf-16-le")) for m in UTF16.finditer(data)]
    if ascii_too:
        found += [(m.start(), m.group().decode("latin-1")) for m in ASCII.finditer(data)]
    lines: list[str] = []
    for _, s in sorted(found):
        s = s.strip()
        if len(s) >= 5 and (not lines or lines[-1] != s) and not re.fullmatch(r"[\W\d_]+", s):
            lines.append(s)
    return lines


def thermo_header(data: bytes) -> list[str]:
    """UTF-16 strings of the Thermo file header (creator and modifier ids/logons, file description)."""
    if not (data[:2] == b"\x01\xa1" and b"F\x00i\x00n\x00n\x00i\x00g\x00a\x00n\x00" in data[:24]):
        return []
    return [s for m in UTF16_SHORT.finditer(data[24:THERMO_HEADER])
            if (s := m.group().decode("utf-16-le").strip()) and s != "Finnigan"]


def xml_pairs(data: bytes) -> tuple[list[tuple[str, str]], str] | None:
    """(key, value) pairs and the joined text of an XML member; None when it is not XML."""
    from lxml import etree
    if not data.lstrip()[:1] == b"<":
        return None
    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, huge_tree=False)
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError:
        return None
    if root is None:
        return None
    pairs, texts = [], []
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        tag = etree.QName(el).localname
        attrs = {etree.QName(k).localname.lower(): v for k, v in el.attrib.items()}
        texts += [v for v in attrs.values() if v]
        text = (el.text or "").strip()
        if text:
            texts.append(text)
            if len(el) == 0:
                pairs.append((tag, text))
        key = next((attrs[k] for k in ("permname", "name", "key", "displayname", "label", "id") if attrs.get(k)), "")
        val = next((attrs[k] for k in ("value", "val", "text") if attrs.get(k)), "")
        if key and val:
            pairs.append((key, val))
        pairs += [(k, v) for k, v in attrs.items() if k not in ("value", "val", "text")]
        kids = {etree.QName(ch).localname.lower(): (ch.text or "").strip() for ch in el if isinstance(ch.tag, str)}
        if kids.get("name") and kids.get("value"):
            pairs.append((kids["name"], kids["value"]))
    return pairs, "\n".join(texts)


def member_candidates(c: Candidates, data: bytes, member: str):
    parsed = xml_pairs(data)
    if parsed is not None:
        pairs, text = parsed
        for k, v in pairs:
            key_value(c, k, v, member)
        text_candidates(c, text, member, keyed=False)
    else:
        text_candidates(c, data.decode("utf-8", "replace") if not data.startswith((b"\xff\xfe", b"\xfe\xff"))
                        else data.decode("utf-16", "replace"), member)


def bruker_global_metadata(tdf: Path) -> list[tuple[str, str]]:
    uri = f"file:{pathname2url(str(tdf.resolve()))}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    try:
        return [(str(k), "" if v is None else str(v)) for k, v in con.execute("SELECT Key, Value FROM GlobalMetadata")]
    finally:
        con.close()


def extract(raw: Path, vendor: str, outdir: Path) -> dict:
    """Writes candidates.tsv and method/ into `outdir`; returns counts."""
    if vendor not in VENDORS:
        raise InputError(f"unknown vendor '{vendor}'")
    if not raw.exists():
        raise InputError(f"raw path not found: {raw.name}")
    base = raw.resolve()
    method = outdir / "method"
    method.mkdir(parents=True, exist_ok=True)
    c = Candidates()
    stats = dict(members=0, skipped_outside=0, skipped_large=0)
    stem = re.sub(r"(?i)\.(raw|d|wiff2?|lcd)$", "", raw.name)
    c.add_value(stem, "file_name", "(name)")
    if vendor in STRING_VENDORS:
        if not base.is_file():
            raise InputError(f"{vendor}: expected a file")
        data = read_head(base, MAX_STRINGS)
        stats["members"] = 1
        for s in thermo_header(data) if vendor == "thermo" else []:
            c.add_value(s, "account", "(file header)")
        lines = binary_strings(data, STRING_VENDORS[vendor])
        (method / "method_strings.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        text_candidates(c, "\n".join(lines), "(file strings)")
    elif vendor in MEMBERS or vendor == "bruker_tdf":
        if not base.is_dir():
            raise InputError(f"{vendor}: expected a directory")
        if vendor == "bruker_tdf":
            tdf = base / "analysis.tdf"
            if tdf.is_file() and inside(tdf, base):
                try:
                    rows = bruker_global_metadata(tdf)
                except sqlite3.Error as e:
                    print(f"WARNING: analysis.tdf: GlobalMetadata not readable ({e.__class__.__name__})",
                          file=sys.stderr)
                    rows = []
                write_table(method / "GlobalMetadata.tsv", [dict(key=k, value=v) for k, v in rows], ["key", "value"])
                for k, v in rows:
                    key_value(c, k, v, "analysis.tdf:GlobalMetadata")
                    if classify_key(k) is None:
                        text_candidates(c, v, "analysis.tdf:GlobalMetadata", keyed=False)
                stats["members"] += 1
        for d in ci_glob(base, "*.m"):
            if d.is_dir() and inside(d, base):
                c.add_value(d.name[:-2], "method_name", "(method directory)")
        seen = set()
        for pat in MEMBERS.get(vendor, []):
            for p in ci_glob(base, pat):
                if p in seen or not p.is_file():
                    continue
                seen.add(p)
                if not inside(p, base):
                    stats["skipped_outside"] += 1
                    continue
                if p.stat().st_size > MAX_MEMBER or stats["members"] >= MAX_MEMBERS:
                    stats["skipped_large"] += 1
                    continue
                rel = p.relative_to(base).as_posix()
                data = p.read_bytes()
                stats["members"] += 1
                (method / rel.replace("/", "__")).write_bytes(data)
                member_candidates(c, data, rel)
    rows = c.result()
    write_table(outdir / "candidates.tsv", rows, CAND_FIELDS)
    stats["candidates"] = len(rows)
    return stats


def add_arguments(p):
    p.add_argument("--raw", required=True, type=Path, help="vendor raw file or directory")
    p.add_argument("--vendor", choices=VENDORS, help="default: detected from the raw path")
    p.add_argument("--raw-id", required=True, help="public raw-file identifier (output subdirectory)")
    p.add_argument("--outdir", type=Path, default=Path("vendor_metadata"))


def run(args) -> int:
    from ..inputs import detect_vendor
    vendor = args.vendor or detect_vendor(args.raw.resolve())
    if vendor == "unknown":
        raise InputError("vendor could not be detected; pass --vendor")
    out = args.outdir / safe_name(args.raw_id)
    if out.exists():
        shutil.rmtree(out)
    stats = extract(args.raw, vendor, out)
    print(f"{args.raw_id}: {vendor}, " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    return 0
