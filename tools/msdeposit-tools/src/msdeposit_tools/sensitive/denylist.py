"""identifier-denylist: the per-study identifier denylist of the sensitive path (design §6, §8.2; CONTRACTS.md §5).

Sources, merged into one `denylist.tsv` (private: `token`, `kind`, `case_sensitive`, `origin`):
  user           --user-denylist tokens; trusted: never filtered (not by the generic list, the length rules, the
                 frequency filter or the allowlist)
  candidate      candidates.tsv of `vendor-metadata` (and serials / source file names of mzml-metadata JSONs)
  original_name  original raw/mzML file stems, companion names and path components of runs.normalised.csv
                 below the part they share with --base-dir (the run sheet's directory, chosen by the depositor;
                 its own components such as /data/projects are not identifying). The run and assay *keys*
                 themselves are not added, S7 checks them against this list instead
  variant        transliteration variants of all of the above (ü→ue/u, ß→ss, accents stripped, ue→ü)
Harvested tokens (candidate, original_name) pass the filters: minimum length 3, not digits/punctuation only,
not a GUID/hash, not in GENERIC (structural mzML/ISA/mzTab words, vendor software and instrument model names,
units, common lab and path words), not on the allowlist, optionally not a frequent English word
(`--frequency-filter zipf=<x>`, needs the optional `wordfreq` package).

Case and matching rules (shared with identifier-scan, check-sensitive and canary-scan via `Matcher`):
  short codes and initials (<= 4 characters, all caps or letters mixed with digits) are case-sensitive;
  every token of <= 4 characters is matched on alphanumeric word boundaries ("QMF" matches "HLB_QMF_7", not
  "QMFX"); longer tokens are matched anywhere ("Haldenbrook" matches "HaldenbrookProject"). Spaces in a token
  match any run of whitespace, "_" or "-".
"""

import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from ..io import InputError, as_bool, read_json, read_table, write_table
from ..plan import strip_data_ext as plan_strip_data_ext

FIELDS = ["token", "kind", "case_sensitive", "origin"]
KINDS = ("user", "candidate", "original_name", "variant", "canary")
SHORT_MAX = 4
MIN_LEN = 3
MAX_LEN = 120
WORD = re.compile(r"[^\W_]+")      # unicode letters and digits

# Never identifying on their own. Only generic vocabulary: no study, lab or person names.
GENERIC = set("""
a an and are as at be by for from in into is it of on or the to with without via per not no yes all any new old
none null true false nan n/a na unknown default other others none_ und der die das von zu mit fuer
data datas file files folder folders dir directory directories path paths raw rawdata method methods sequence
sequences sample samples list lists batch batches project projects study studies result results report reports
backup archive archives copy export exports import imports temp tmp test tests testing dummy demo example examples
document documents desktop users user public program programs programdata windows system system32 appdata local
roaming settings application applications service services shared share shares home vol mnt media opt srv var usr
net drive network work workdir launch launchdir output outputs input inputs final draft version versions
pipeline pipelines nextflow nf core nfcore msdeposit lifs tools tool docker singularity apptainer conda wine
mzml mzxml mzdata mgf mzml_ mztab mzqc isa isatab metabolights mtbls maf json xml html tsv csv txt yaml yml zip
indexedmzml cvlist cv filedescription filecontent sourcefilelist sourcefile contact referenceableparamgrouplist
referenceableparamgroup referenceableparamgroupref samplelist softwarelist software softwareref
instrumentconfigurationlist instrumentconfiguration componentlist source analyzer detector dataprocessinglist
dataprocessing processingmethod run spectrumlist spectrum scanlist scan scanwindowlist scanwindow precursorlist
precursor isolationwindow selectedionlist selectedion activation productlist product binarydataarraylist
binarydataarray binary chromatogramlist chromatogram indexlist index offset indexlistoffset filechecksum cvparam
userparam sourcefileref targetlist target scansettingslist scansettings idref
investigation protocol protocols characteristics factor factors value values term accession comment comments
assay assays extract labeled label normalization transformation parameter parameters unit units name names
person persons publication publications descriptor descriptors design contacts role roles affiliation address
email phone fax orcid title description submission release identifier identifiers source sources ref
mtd sml smf sme prt psh psm cv_ ms_run assay_ study_variable abundance database reliability adduct formula
smiles inchi chemical modification modifications retention time charge exp theoretical uri
thermo thermofisher finnigan scientific xcalibur tracefinder chromeleon freestyle foundation compound discoverer
sciex absciex analyst multiquant sciexos chipsoft masshunter agilent mass hunter openlab chemstation bruker
daltonics compass otofcontrol hystar dataanalysis tasq waters masslynx unifi targetlynx quanlynx empower
shimadzu labsolutions insight skyline proteowizard pwiz msconvert thermorawfileparser lipidsearch lipidxplorer
msdial ms-dial lipostar mzmine xcms openms
orbitrap exactive exploris fusion lumos eclipse astral velos elite ltq tsq altis quantiva quantum endura vantage
ascend qtrap tripletof zenotof triple quad quadrupole xevo synapt vion cyclic tims timstof impact maxis compact
solarix scimax amazon esquire microtof ultraflex rapiflex vanquish dionex ultimate accela acquity uplc hplc uhplc
infinity nexera exionlc exion shimadzu lcms gcms nanomate triversa advion nanoflow
ion ions mode modes positive negative pos neg polarity spectra spectral full fullms dda dia ddms msms ms ms1 ms2
msn srm mrm dmrm smrm sim tsim tmsms profile centroid centroided lock masses lockmass calibration calibrant
tune tuning hesi esi apci appi maldi nano spray electrospray source capillary sheath aux sweep gas heater
voltage current temperature pressure flow rate gradient column columns solvent solvents buffer mobile phase
injection inject injector autosampler vial vials tray trays plate plates well wells rack position positions
oven pump pumps valve detector detectors resolution agc maxit microscans isolation collision energy nce hcd cid
precursor fragment fragments transition transitions dwell window segment segments experiment experiments period
lipid lipids lipidomic lipidomics metabolite metabolites metabolomic metabolomics plasma serum blood urine tissue
cell cells blank blanks qc qcs pool pooled nist srm reference standard standards istd internal external mix
spike spiked control controls extraction extracted folch mtbe bligh dyer hilic rp c18 c8 c30 sfc lc gc fia di
quant quan qual quantification quantitation targeted untargeted lipidomics_ screening acquisition acq acqdata
minute minutes min second seconds sec ms_ hour hours volt volts kv ev electronvolt kelvin celsius degree degrees
bar psi pascal mpa ml ul µl nl mg ug µg ng pg mol mmol umol µmol nmol pmol fmol percent ppm ppb mz dalton da
version major minor revision build release licence license copyright
lab labs laboratory laboratories institute institut university universitaet universität hospital clinic klinikum
faculty department dept gmbh inc ltd corp group team center centre facility core division
plus pro max ultra duo flex hf hfx tof qtof ims xs xl hd hr ii iii iv uhr esi-l nanoelute evosep
pc pe pi ps pg pa lpc lpe lpi lps lpg lpa sm cer hexcer hex2cer lacer cl sph s1p tg tag dg dag mg mag ce chol st
fa ffa car acar lipidmaps swisslipids goslin hmdb chebi kegg pubchem
""".split())


@dataclass(frozen=True)
class Entry:
    token: str
    kind: str
    case_sensitive: bool
    origin: str = ""


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def is_short_code(token: str) -> bool:
    """Initials and short codes: <= 4 characters, all caps or letters mixed with digits."""
    t = token.strip()
    if len(t) > SHORT_MAX:
        return False
    letters = [c for c in t if c.isalpha()]
    return bool(letters) and (t.upper() == t or any(c.isdigit() for c in t))


def auto_case(token: str) -> bool:
    return is_short_code(token)


def camel_parts(word: str) -> list[str]:
    """'HaldenbrookProject' -> ['Haldenbrook', 'Project']; 'HTMLParser2' -> ['HTML', 'Parser', '2']."""
    parts, cur = [], ""
    for i, ch in enumerate(word):
        if cur:
            prev, nxt = cur[-1], word[i + 1] if i + 1 < len(word) else ""
            if (prev.isdigit() != ch.isdigit() or (prev.islower() and ch.isupper())
                    or (prev.isupper() and ch.isupper() and nxt.islower())):
                parts.append(cur)
                cur = ""
        cur += ch
    return parts + [cur] if cur else parts


def words(text: str) -> list[str]:
    """Letter/digit runs and, unless the run is a generic word, its CamelCase/digit parts as well."""
    out = []
    for w in WORD.findall(text):
        out.append(w)
        parts = camel_parts(w)
        if len(parts) > 1 and not is_generic(w):
            out += parts
    return out


def _generic_word(w: str) -> bool:
    return w in GENERIC or (w.endswith("s") and w[:-1] in GENERIC)


def is_generic(token: str) -> bool:
    """Generic when the token, or every letter run of it, is generic ('Program Files', 'data1', 'Lipids')."""
    t = token.strip().lower()
    if _generic_word(t):
        return True
    runs = re.findall(r"[^\W\d_]+", t)
    return bool(runs) and all(len(w) == 1 or _generic_word(w) for w in runs)


def harvest_ok(token: str, numeric_ok: bool = False) -> bool:
    """Filters for harvested tokens (never applied to user tokens). `numeric_ok`: serial numbers may be digits only
    (at least 5 digits; they are matched on word boundaries)."""
    t = token.strip()
    if len(t) < MIN_LEN or len(t) > MAX_LEN or "\n" in t or "\t" in t:
        return False
    if re.fullmatch(r"[\d\W_]+", t):                                   # numbers, dates, punctuation
        return numeric_ok and sum(c.isdigit() for c in t) >= 5 and not re.fullmatch(r"\d{4}-\d\d-\d\d.*", t)
    if re.fullmatch(r"[0-9a-fA-F-]{16,}|\{?[0-9a-fA-F-]{36}\}?", t):    # hashes, GUIDs
        return False
    if re.fullmatch(r"[\d.]+[a-zA-Zµ]{1,3}", t):                        # 3.5kV, 10ul
        return False
    return not is_generic(t)


# --- transliteration variants -------------------------------------------------------------------------------------
GERMAN = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})
SPECIAL = str.maketrans({"ß": "ss", "æ": "ae", "Æ": "Ae", "ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "đ": "d",
                         "Đ": "D", "œ": "oe", "Œ": "Oe", "þ": "th", "Þ": "Th", "ð": "d", "ı": "i"})
REVERSE = (("ae", "ä"), ("oe", "ö"), ("ue", "ü"), ("Ae", "Ä"), ("Oe", "Ö"), ("Ue", "Ü"))


def strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.translate(SPECIAL))
    return "".join(c for c in s if not unicodedata.combining(c))


def variants(token: str) -> list[str]:
    """ü→ue and ü→u (accents stripped); for ASCII tokens with ae/oe/ue also the umlaut form."""
    t = nfc(token)
    out = [nfc(t.translate(GERMAN)), strip_accents(t)]
    if t.isascii() and re.search(r"(?<![qQ])[aouAOU]e", t):
        r = t
        for a, b in REVERSE:
            r = re.sub(r"(?<![qQ])" + a, b, r)
        out.append(r)
    return list(dict.fromkeys(v for v in out if v and v != t))


# --- matcher -------------------------------------------------------------------------------------------------------
SEP = r"[\s_\-]+"
BOUNDARY_BEFORE, BOUNDARY_AFTER = r"(?<![^\W_])", r"(?![^\W_])"


def _key(token: str, case_sensitive: bool) -> str:
    # lower(), not casefold(): re.IGNORECASE does simple case folding ('ß' does not match 'ss')
    t = re.sub(r"[\s_\-]+", " ", nfc(token).strip())
    return t if case_sensitive else t.lower()


def _trie_regex(tokens: list[str]) -> str:
    trie: dict = {}
    for t in tokens:
        node = trie
        for ch in t:
            node = node.setdefault(ch, {})
        node[""] = {}

    def emit(node) -> str:
        alts = [(SEP if ch == " " else re.escape(ch)) + emit(sub) for ch, sub in sorted(node.items()) if ch]
        if not alts:
            return ""
        body = alts[0] if len(alts) == 1 else "(?:" + "|".join(alts) + ")"
        if "" in node:
            return body + "?" if len(alts) == 1 and len(body) == 1 else f"(?:{body})?"
        return body

    return emit(trie)


class Matcher:
    """Compiled denylist. `finditer(text)` yields (start, end, entry) for every match."""

    def __init__(self, entries: list[Entry]):
        self.entries = list(entries)
        self.lookup: dict[tuple[bool, str], Entry] = {}
        groups: dict[tuple[bool, bool], list[str]] = {}
        for e in self.entries:
            tok = re.sub(r"\s+", " ", nfc(e.token).strip())
            if not tok:
                continue
            key = _key(tok, e.case_sensitive)
            self.lookup.setdefault((e.case_sensitive, key), e)
            pat = tok if e.case_sensitive else tok.lower()
            bounded = len(tok) <= SHORT_MAX or not any(c.isalpha() for c in tok)
            groups.setdefault((e.case_sensitive, bounded), []).append(pat)
        self.max_len = max((len(e.token) for e in self.entries), default=0)
        # case-insensitive groups are matched against text.lower() (several times faster than re.IGNORECASE);
        # the IGNORECASE variant is used only when lower() changes the length of the text
        self.patterns: list[tuple[bool, re.Pattern, re.Pattern | None]] = []
        for (cs, short), toks in sorted(groups.items()):
            rx = "(?:" + _trie_regex(sorted(set(toks))) + ")"
            if short:
                rx = BOUNDARY_BEFORE + rx + BOUNDARY_AFTER
            self.patterns.append((cs, re.compile(rx), None if cs else re.compile(rx, re.IGNORECASE)))

    def __bool__(self):
        return bool(self.patterns)

    def finditer(self, text: str):
        low = None
        for cs, rx, rx_icase in self.patterns:
            target = text
            if not cs:
                if low is None:
                    low = text.lower()
                    low = low if len(low) == len(text) else ""
                target, rx = (low, rx) if low or not text else (text, rx_icase)
            for m in rx.finditer(target):
                m_text = text[m.start():m.end()]
                e = self.lookup.get((cs, _key(m_text, cs)))
                yield m.start(), m.end(), e or Entry(m_text, "unknown", cs)

    def search(self, text: str) -> list[Entry]:
        return [e for _, _, e in self.finditer(nfc(text))]


# --- reading and writing ------------------------------------------------------------------------------------------
def read_denylist(path: Path) -> list[Entry]:
    path = Path(path)
    if not path.exists():
        raise InputError(f"denylist not found: {path.name}")
    rows = read_table(path, "\t")
    if rows and "token" not in rows[0]:
        raise InputError(f"{path.name}: expected a 'token' column")
    return [Entry(nfc(r["token"]), r.get("kind") or "user", as_bool(r.get("case_sensitive") or "false"),
                  r.get("origin", "")) for r in rows if r.get("token")]


def write_denylist(path: Path, entries: list[Entry]) -> None:
    order = {k: i for i, k in enumerate(KINDS)}
    rows = [dict(token=e.token, kind=e.kind, case_sensitive=str(e.case_sensitive).lower(), origin=e.origin)
            for e in sorted(entries, key=lambda e: (order.get(e.kind, 9), e.token.casefold(), e.token))]
    write_table(path, rows, FIELDS)


def read_token_file(path: Path) -> list[dict]:
    """One token per line ('#' comments) or a TSV/CSV with 'token' and optional 'case_sensitive', 'note'."""
    path = Path(path)
    if not path.exists():
        raise InputError(f"token file not found: {path.name}")
    text = path.read_text(encoding="utf-8-sig")
    first = text.splitlines()[0].strip().lower() if text.strip() else ""
    if path.suffix.lower() in (".tsv", ".csv") or first.split("\t")[0].split(",")[0] == "token":
        delim = "," if path.suffix.lower() == ".csv" else "\t"
        rows = read_table(path, delim)
        if rows and "token" not in rows[0]:
            raise InputError(f"{path.name}: expected a 'token' column")
        return [dict(token=nfc(r["token"]), case_sensitive=r.get("case_sensitive", ""), note=r.get("note", ""))
                for r in rows if r.get("token")]
    return [dict(token=nfc(line.strip()), case_sensitive="", note="") for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


# --- building ------------------------------------------------------------------------------------------------------
class Builder:
    """Collects entries; key = token (case-sensitive) or lower-cased token. User entries win over harvested ones."""

    PRIORITY = {"user": 0, "candidate": 1, "original_name": 2, "variant": 3}

    def __init__(self, allow: set[str], zipf: float | None):
        self.entries: dict[tuple[bool, str], Entry] = {}
        self.allow = allow
        self.allow_cf = {a.casefold() for a in allow}   # harvested tokens are allowed regardless of case
        self.zipf = zipf
        self.freq = None
        self.dropped = {"generic_or_short": 0, "allowlist": 0, "frequent": 0}
        self.warnings: list[str] = []
        if zipf is not None:
            try:
                from wordfreq import zipf_frequency
                self.freq = zipf_frequency
            except ImportError:
                self.warnings.append("frequency filter requested but the optional package 'wordfreq' is not "
                                     "installed; filter not applied")

    def _put(self, e: Entry):
        k = (e.case_sensitive, _key(e.token, e.case_sensitive))
        old = self.entries.get(k)
        if old is None:
            self.entries[k] = e
            return
        first, other = (e, old) if self.PRIORITY.get(e.kind, 9) < self.PRIORITY.get(old.kind, 9) else (old, e)
        origins = list(dict.fromkeys(o for x in (first, other) for o in x.origin.split("; ") if o))
        self.entries[k] = Entry(first.token, first.kind, first.case_sensitive, "; ".join(origins[:20]))

    def add_user(self, token: str, case_sensitive: bool | None, origin: str = "user"):
        token = nfc(token.strip())
        if not token:
            return
        cs = auto_case(token) if case_sensitive is None else case_sensitive
        if token.casefold() in self.allow_cf:
            self.warnings.append("a user denylist token is also on the allowlist; the denylist wins")
        self._put(Entry(token, "user", cs, origin))
        for v in variants(token):
            self._put(Entry(v, "variant", cs, "variant of user token"))

    def add_harvested(self, token: str, kind: str, origin: str, keep_common: bool = False, numeric_ok: bool = False):
        token = nfc(re.sub(r"\s+", " ", token.strip().strip(".:;,'\"()[]{}<>")))
        if not harvest_ok(token, numeric_ok):
            self.dropped["generic_or_short"] += 1
            return
        if token.casefold() in self.allow_cf:
            self.dropped["allowlist"] += 1
            return
        cs = auto_case(token)
        if self.freq is not None and not cs and " " not in token and self.freq(token.lower(), "en") >= self.zipf:
            if not keep_common:
                self.dropped["frequent"] += 1
                return
            cs = True       # a person name that is also a common word (e.g. 'Rook'): keep, case-sensitive
        self._put(Entry(token, kind, cs, origin))
        for v in variants(token):
            if v.casefold() not in self.allow_cf:
                self._put(Entry(v, "variant", cs, f"variant of {kind}"))

    def result(self) -> list[Entry]:
        return list(self.entries.values())


PERSON_FIELDS = {"person", "account", "email"}


def add_candidates(b: Builder, path: Path):
    rows = read_table(path, "\t")
    if rows and not {"token", "field"} <= rows[0].keys():
        raise InputError(f"{path.name}: expected columns token, field, member")
    raw_id = path.parent.name
    for r in rows:
        tok, field = r.get("token", ""), r.get("field", "")
        b.add_harvested(tok, "candidate", f"vendor:{raw_id}:{field}", keep_common=field in PERSON_FIELDS,
                        numeric_ok=field == "serial")


def strip_data_ext(name: str) -> str:
    for ext in (".wiff.scan", ".wiff2.scan"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
    return plan_strip_data_ext(name)


def path_tokens(path: str) -> list[str]:
    """Components of a POSIX, Windows or file:// path; the last one without data extensions."""
    p = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://+", "", path.strip())   # file://, s3://, ... (the bucket stays a component)
    parts = [x for x in re.split(r"[\\/]+", p) if x and not re.fullmatch(r"[A-Za-z]:", x)]
    if parts:
        parts[-1] = strip_data_ext(parts[-1])
    return parts


def relative_parts(path: str, base: str | None) -> list[str]:
    """Components of `path` after the leading components it shares with `base`."""
    parts = path_tokens(path)
    if not base:
        return parts
    base_parts = path_tokens(base) if base.strip() else []
    n = 0
    while n < min(len(parts) - 1, len(base_parts)) and parts[n] == base_parts[n]:
        n += 1
    return parts[n:]


def word_ok(w: str, skip: set[str]) -> bool:
    """Word parts of original names are kept, even short lower-case ones (initials such as 'qmf'), unless they are
    part of a deposited name or key; generic words are dropped by harvest_ok."""
    return w.casefold() not in skip


def deposited_words(rows: list[dict], study: str) -> set[str]:
    """Words of sample names, run/assay keys and deposited names: deposited or checked by S7 by design."""
    rows = [r for r in rows if not study or r.get("study") == study]
    return {w.casefold() for r in rows for k in ("sample", "run", "assay", "deposited_name") for w in words(r.get(k, ""))}


def add_original_names(b: Builder, rows: list[dict], study: str, base: str | None = None):
    """Original file stems and path components below the part shared with `base` (whole and split into words);
    words that are part of a sample name or of a run/assay key of the study are skipped (they are deposited or
    checked by S7 by design)."""
    skip = deposited_words(rows, study)
    rows = [r for r in rows if not study or r.get("study") == study]
    for r in rows:
        paths = [r.get("path", "")] + [c for c in (r.get("companions") or "").split(";") if c]
        for p in paths:
            for comp in relative_parts(p, base):
                b.add_harvested(comp, "original_name", "run sheet path")
                for w in words(comp):
                    if word_ok(w, skip):
                        b.add_harvested(w, "original_name", "run sheet path")


def add_metadata(b: Builder, path: Path, skip: set[str] = frozenset()):
    """mzml-metadata JSON (private): instrument serial and source file names/locations of the converted mzML."""
    d = read_json(path, "msdeposit/mzml_metadata/1")
    serial = (d.get("instrument") or {}).get("serial") or ""
    if serial:
        b.add_harvested(serial, "candidate", "mzML:serial", keep_common=True, numeric_ok=True)
    for sf in d.get("source_files") or []:
        for v in (sf.get("name") or "", sf.get("location") or ""):
            for comp in path_tokens(v):
                b.add_harvested(comp, "original_name", "mzML:source file")
                for w in words(comp):
                    if word_ok(w, skip):
                        b.add_harvested(w, "original_name", "mzML:source file")


def find_files(paths: list[Path], name: str | None = None, pattern: str = "*") -> list[Path]:
    out = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            out += sorted(x for x in p.rglob(name or pattern) if x.is_file())
        elif p.exists():
            out.append(p)
        else:
            raise InputError(f"not found: {p.name}")
    return out


def parse_frequency_filter(value: str) -> float | None:
    v = (value or "off").strip().lower()
    if v in ("off", "none", "false", ""):
        return None
    m = re.fullmatch(r"zipf\s*=\s*([0-9.]+)", v)
    if not m:
        raise InputError("--frequency-filter must be 'off' or 'zipf=<number>' (e.g. zipf=3.3)")
    return float(m[1])


def add_arguments(p):
    p.add_argument("--study", required=True)
    p.add_argument("--candidates", nargs="*", type=Path, default=[],
                   help="candidates.tsv files or directories containing them (vendor-metadata output)")
    p.add_argument("--user-denylist", type=Path, help="user tokens (never filtered)")
    p.add_argument("--allowlist", type=Path, help="tokens never added from harvested sources (exact match)")
    p.add_argument("--run-keys", type=Path, help="runs.normalised.csv: original file stems and path components")
    p.add_argument("--base-dir", help="directory of the run sheet: path components shared with it are not harvested")
    p.add_argument("--metadata", nargs="*", type=Path, default=[],
                   help="mzml-metadata JSON files or directories: serial numbers and source file names")
    p.add_argument("--frequency-filter", default="off", help="'off' (default) or 'zipf=<x>' to drop frequent "
                                                             "English words from harvested tokens")
    p.add_argument("--out", type=Path, default=Path("denylist.tsv"))


def build(args) -> tuple[list[Entry], Builder]:
    allow = {r["token"] for r in read_token_file(args.allowlist)} if args.allowlist else set()
    b = Builder(allow, parse_frequency_filter(args.frequency_filter))
    if args.user_denylist:
        for r in read_token_file(args.user_denylist):
            cs = as_bool(r["case_sensitive"]) if r["case_sensitive"] else None
            b.add_user(r["token"], cs)
    for f in find_files(args.candidates, "candidates.tsv"):
        add_candidates(b, f)
    run_rows = read_table(args.run_keys, ",") if args.run_keys else []
    skip = deposited_words(run_rows, args.study)
    for f in find_files(args.metadata, pattern="*.json"):
        if str(read_json(f).get("schema", "")).startswith("msdeposit/mzml_metadata/"):
            add_metadata(b, f, skip)
    if run_rows:
        add_original_names(b, run_rows, args.study, args.base_dir)
    return b.result(), b


def run(args) -> int:
    entries, b = build(args)
    write_denylist(args.out, entries)
    for w in dict.fromkeys(b.warnings):
        print(f"WARNING: {w}", file=sys.stderr)
    kinds = {k: sum(1 for e in entries if e.kind == k) for k in KINDS if k != "canary"}
    print(f"denylist for {args.study}: {len(entries)} tokens " + ", ".join(f"{k}={n}" for k, n in kinds.items())
          + "; dropped " + ", ".join(f"{k}={n}" for k, n in b.dropped.items()))
    return 0
