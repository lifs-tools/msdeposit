"""isa: ISA-Tab investigation, sample and assay files of one study for MetaboLights (CONTRACTS.md §6).

Inputs: study.json, plan.json, the mzml-metadata JSON of the runs (public fields only, see `public_metadata`),
optional finalise.json of the runs and protocol text blocks (`--protocols <dir>`: `<study>/<key>.md` before
`<key>.md`). Outputs in --outdir, with the names from the plan:
  i_Investigation.txt   written with the metabolights-utils investigation writer
  s_<prefix>.txt        sample table (MetaboLights sample template of the study category)
  a_*.txt               one assay table per plan assay, rows in plan run order
Column structures follow the MetaboLights templates of the pinned validation bundle (data/mtbls_templates.json);
every table is checked against them before it is written. Controlled values (polarity, column type, mass analyzer)
follow --controlled-value-profile (data/mtbls_controlled_values.json). Required values that are not known stay empty
and unresolved text keys become `[[TODO-PROTOCOL:<key>]]` / `[[TODO-TEXT:<key>]]` placeholders (counted by C9);
values derived from fallbacks are listed in `Comment[Inferred values]`, drafted protocol text is marked as such.

Protocol text keys: a protocol takes the keys given for it in the study config (`protocols: {<Protocol name>: key or
[keys]}`) and the assay `protocol_keys` that belong to it (`<Protocol name>=<key>`, or a key equal to or starting with
the protocol slug, e.g. `chromatography_hilic` -> Chromatography); without any key the slug itself is used
(`sample_collection`, `extraction`, `chromatography`, `mass_spectrometry`, `data_transformation`, ...).
For LC/GC/CE assays the assay `injection_system` is written as the chromatography/CE instrument.
"""

import csv
import datetime
import json
import re
from collections import Counter
from functools import cache
from pathlib import Path

from metabolights_utils.isatab import Writer
from metabolights_utils.models.isa.investigation_file import (
    Assay, ExtendedOntologyAnnotation, Factor, Funder, Investigation, OntologyAnnotation, OntologySourceReference,
    ParameterDefinition, Person, Protocol, Publication, RelatedDataset, Study)

from ..io import InputError, read_json, safe_name
from ..io import load_metadata

DATA = Path(__file__).resolve().parents[1] / "data"
EMPTY = ("", "", "")
OBO = "http://purl.obolibrary.org/obo/"
CATEGORIES = ("ms-mhd-enabled", "ms-mhd-legacy", "other")
SAMPLE_TEMPLATE = {"ms-mhd-enabled": "mhd-enabled"}          # other categories use the 'minimum' sample template
PROTOCOL_ORDER = ["Sample collection", "Extraction", "Chromatography", "Capillary Electrophoresis",
                  "Flow Injection Analysis", "Direct infusion", "Mass spectrometry", "Data transformation",
                  "Metabolite identification"]
GENERIC_MODELS = {"MS:1000031"}                                # 'instrument model' (no specific model in the mzML)
CONVERSION_SOFTWARE = {"MS:1000615": "ProteoWizard msconvert", "MS:1002205": "ProteoWizard msconvert",
                       "MS:1003145": "ThermoRawFileParser"}
DRAFT = "[Drafted from the deposit metadata; please review]"
TIMESTAMP_POLICIES = ("keep", "date", "drop")


@cache
def templates() -> dict:
    return json.loads((DATA / "mtbls_templates.json").read_text(encoding="utf-8"))


@cache
def cv() -> dict:
    return json.loads((DATA / "mtbls_controlled_values.json").read_text(encoding="utf-8"))


def add_arguments(p):
    p.add_argument("--study-json", required=True, type=Path)
    p.add_argument("--plan", required=True, type=Path)
    p.add_argument("--metadata", nargs="*", default=[], type=Path,
                   help="mzml-metadata JSON files or directories containing them")
    p.add_argument("--finalise", nargs="*", default=[], type=Path,
                   help="finalise.json files or directories containing them (optional)")
    p.add_argument("--protocols", type=Path, help="directory with protocol text blocks <key>.md, <study>/<key>.md")
    p.add_argument("--controlled-value-profile", choices=("validator", "mhd"), default="validator")
    p.add_argument("--study-category", choices=CATEGORIES, default="ms-mhd-enabled")
    p.add_argument("--timestamp-policy", choices=TIMESTAMP_POLICIES, default="keep",
                   help="acquisition start times in the assay tables and protocol text: keep, date only, or drop")
    p.add_argument("--created-at", default="", help="Comment[Created At] (YYYY-MM-DD; default: today)")
    p.add_argument("--outdir", type=Path, default=Path("."))


# ---- terms -----------------------------------------------------------------------------------------------------

def term_url(source: str, accession: str) -> tuple[str, str]:
    """('NCBITaxon', 'NCBITaxon:9606') -> ('NCBITaxon', 'http://purl.obolibrary.org/obo/NCBITaxon_9606')."""
    source, accession = (source or "").strip(), (accession or "").strip()
    if not accession or accession.startswith(("http://", "https://")):
        return source, accession
    m = re.fullmatch(r"([A-Za-z][A-Za-z0-9.]*)[:_](.+)", accession)
    if not m:
        return source, accession
    prefix, local = m.group(1), m.group(2)
    base = cv()["accession_prefixes"].get(prefix.upper()) or cv()["accession_prefixes"].get(prefix)
    return source or prefix, (base + local) if base else f"{OBO}{prefix}_{local}"


def ms_term(name: str, accession: str) -> tuple:
    return (name, *term_url("MS", accession)) if accession else (name, "", "")


def as_term(x) -> tuple:
    """A term from a string, a [term, source, accession] list or a {term|value|name, source, accession} dict."""
    if not x:
        return EMPTY
    if isinstance(x, str):
        return (x.strip(), "", "")
    if isinstance(x, (list, tuple)):
        x = list(x) + ["", ""]
        return (str(x[0] or ""), *term_url(str(x[1] or ""), str(x[2] or "")))
    if isinstance(x, dict):
        name = x.get("term") or x.get("value") or x.get("name") or ""
        src = x.get("source") or x.get("term_source_ref") or ""
        acc = x.get("accession") or x.get("term_accession_number") or ""
        return (str(name), *term_url(str(src), str(acc)))
    return (str(x), "", "")


def oa(t: tuple, cls=OntologyAnnotation, **kw):
    return cls(term=t[0], term_source_ref=t[1], term_accession_number=t[2], **kw)


def cell(v) -> str:
    """ISA cells: no tabs or line breaks."""
    return re.sub(r"\s+", " ", str(v if v is not None else "")).strip()


def sentence(bits: list[str]) -> str:
    text = "; ".join(b for b in bits if b)
    return (text[:1].upper() + text[1:] + ".") if text else ""


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


# ---- inputs ----------------------------------------------------------------------------------------------------

def public_metadata(meta: dict, timestamp_policy: str) -> dict:
    """Only the public fields of an mzml-metadata artefact (CONTRACTS.md §2); never serial, source files or run
    info. The acquisition start is added as `acquisition_start` according to the timestamp policy."""
    keys = ("run", "nativeid_format", "file_content", "software", "counts", "chromatograms", "polarity", "mz_window",
            "resolving_powers", "activations", "collision_energies", "status")
    out = {k: meta.get(k) for k in keys}
    inst = meta.get("instrument") or {}
    out["instrument"] = {"model": inst.get("model", ""), "model_accession": inst.get("model_accession", ""),
                         "configurations": inst.get("configurations") or []}
    start = str((meta.get("run_info") or {}).get("start") or "")
    out["acquisition_start"] = {"keep": start, "date": start[:10], "drop": ""}[timestamp_policy]
    return out


def load_artefacts(paths: list[Path], schema: str) -> dict[str, dict]:
    files = []
    for p in paths:
        files += sorted(p.rglob("*.json")) if p.is_dir() else [p]
    out = {}
    for f in files:
        d = read_json(f)
        if not str(d.get("schema", "")).startswith(schema.rsplit("/", 1)[0] + "/"):
            continue
        read_json(f, schema)
        out[d["run"]] = d
    return out


class Texts:
    """Text blocks `<dir>/<study>/<key>.md` (first) or `<dir>/<key>.md`; whitespace collapsed for ISA cells."""

    def __init__(self, directory: Path | None, study: str):
        self.dirs = [directory / study, directory] if directory else []
        if directory and not directory.is_dir():
            raise InputError("--protocols: not a directory")

    def get(self, key: str) -> str | None:
        name = f"{key}.md" if not key.endswith(".md") else key
        if "/" in key or "\\" in key or key.startswith("."):
            raise InputError(f"protocol key '{key}' must be a plain name")
        for d in self.dirs:
            p = d / name
            if p.is_file():
                text = " ".join(p.read_text(encoding="utf-8").split())
                if text:
                    return text
        return None

    def resolve(self, key: str, kind: str = "PROTOCOL") -> str:
        return self.get(key) or f"[[TODO-{kind}:{key}]]"


# ---- MS values from the metadata --------------------------------------------------------------------------------

def components(meta: dict, kind: str, first_only: bool = False) -> list[tuple[str, str]]:
    """[(name, accession)] of the source/analyzer/detector components (first configuration or all)."""
    configs = meta.get("instrument", {}).get("configurations") or []
    out = []
    for conf in configs[:1] if first_only else configs:
        for c in sorted(conf.get("components", []), key=lambda c: c.get("order") or 0):
            if c.get("type") == kind:
                out += [(t[1], t[0]) for t in c.get("terms", []) if "inlet" not in t[1]][:1]
    return out


def instrument_class(analyzers: list[str], srm: bool = False, ion_mobility: bool = False) -> tuple:
    """MS instrument class term (ms-mhd-enabled 'Mass analyzer') from the analyzer chain of all configurations."""
    names = [a.lower() for a in analyzers]
    has = lambda s: any(s in n for n in names)  # noqa: E731
    quad = sum(1 for n in names if n == "quadrupole" or "quadrupole" in n and "ion trap" not in n)
    trap = has("ion trap") or has("linear trap")
    if has("orbitrap"):
        key = ("quadrupole ion trap orbitrap" if quad and trap else "quadrupole orbitrap" if quad else
               "ion trap orbitrap" if trap else "orbitrap")
    elif has("time-of-flight"):
        key = ("quadrupole ion mobility time-of-flight" if quad and ion_mobility else
               "quadrupole time-of-flight" if quad else "ion trap time-of-flight" if trap else
               "tandem time-of-flight" if sum(1 for n in names if "time-of-flight" in n) > 1 else "time-of-flight")
    elif quad:
        key = "triple quadrupole" if quad >= 2 or srm else "quadrupole"
    elif trap:
        key = "ion trap"
    else:
        return EMPTY
    return tuple(cv()["instrument_class"][key])


def software_split(meta: dict) -> tuple[list[str], list[str]]:
    """(acquisition software, conversion software) as 'name version'."""
    acq, conv = [], []
    for s in meta.get("software") or []:
        terms = s.get("terms") or []
        acc = terms[0][0] if terms else ""
        name = terms[0][1] if terms else s.get("id", "")
        label = f"{CONVERSION_SOFTWARE.get(acc, name)} {s.get('version', '')}".strip()
        sid = str(s.get("id", "")).lower()
        if acc in CONVERSION_SOFTWARE or sid.startswith(("pwiz", "psims", "msdeposit", "thermorawfileparser")):
            conv.append(label)
        else:
            acq.append(label)
    return acq, conv


def ms_values(meta: dict | None, assay: dict, profile: str) -> dict:
    """Mass spectrometry parameter values of one run; fallbacks from the assay sheet are recorded in `inferred`."""
    meta = meta or {}
    out = dict(instrument=EMPTY, ion_source=EMPTY, analyzer=EMPTY, mz_range="", software="", start="", inferred=[])
    inst = meta.get("instrument") or {}
    if inst.get("model") and inst.get("model_accession") not in GENERIC_MODELS:
        out["instrument"] = ms_term(inst["model"], inst.get("model_accession", ""))
    elif assay.get("instrument_fallback"):
        out["instrument"] = (assay["instrument_fallback"], "", "")
        out["inferred"].append("instrument from the assay sheet")
    if meta.get("mz_window"):
        lo, hi = meta["mz_window"][:2]
        out["mz_range"] = f"{lo:g}-{hi:g}"
    sources = components(meta, "source", first_only=True)
    if sources:
        out["ion_source"] = ms_term(*sources[0])
    srm = bool((meta.get("chromatograms") or {}).get("srm"))
    if profile == "mhd":
        out["analyzer"] = instrument_class([n for n, _ in components(meta, "analyzer")], srm,
                                           bool((meta.get("counts") or {}).get("im_arrays")))
    else:
        analyzers = components(meta, "analyzer", first_only=True)
        srm_only = srm and not (meta.get("counts") or {}).get("spectra")
        if srm_only:
            # SRM uses the quadrupole mass filters, also on hybrid instruments (QTRAP: not the linear ion trap)
            out["analyzer"] = ms_term("quadrupole", "MS:1000081")
            if not analyzers:
                out["inferred"].append("mass analyzer from SRM chromatograms")
            elif analyzers[-1][0] != "quadrupole":
                out["inferred"].append("mass analyzer from SRM acquisition")
        elif analyzers:
            out["analyzer"] = ms_term(*analyzers[-1])     # last analyzer of the default configuration
        elif srm:
            out["analyzer"] = ms_term("quadrupole", "MS:1000081")
            out["inferred"].append("mass analyzer from SRM chromatograms")
    acq, _ = software_split(meta)
    out["software"] = "; ".join(acq)
    out["start"] = meta.get("acquisition_start", "")
    return out


def controlled(kind: str, value: str, profile: str) -> tuple:
    """Controlled value of a polarity or column type for the profile; unknown values pass through unchanged."""
    if not value:
        return EMPTY
    if kind == "column_type":
        key = cv()["column_type_aliases"].get(value.strip().lower(), value.strip())
        return tuple(cv()["column_type"][profile].get(key, (value, "", "")))
    return tuple(cv()[kind][profile].get(value, (value, "", "")))


# ---- tables ----------------------------------------------------------------------------------------------------

class Table:
    """Rows of (header, value, structure); ontology columns expand to value + Term Source REF + Term Accession
    Number, `UNIT` columns to value + Unit + Term Source REF + Term Accession Number."""

    def __init__(self):
        self.rows: list[list[tuple]] = []

    def add_row(self, cells: list[tuple]):
        if self.rows and [c[0] for c in cells] != [c[0] for c in self.rows[0]]:
            raise ValueError("ISA table rows with different columns")
        self.rows.append(cells)

    @staticmethod
    def _expand(header, value, structure) -> tuple[list[str], list[str]]:
        if structure == "ONTOLOGY_COLUMN":
            t = as_term(value) if not isinstance(value, tuple) else value
            return [header, "Term Source REF", "Term Accession Number"], [cell(x) for x in t]
        if structure == "UNIT":
            v, unit = value
            u = as_term(unit) if not isinstance(unit, tuple) else unit
            return [header, "Unit", "Term Source REF", "Term Accession Number"], [cell(v)] + [cell(x) for x in u]
        if structure == "SINGLE_COLUMN":
            return [header], [cell(value[0] if isinstance(value, tuple) else value)]
        raise ValueError(f"unsupported column structure {structure}")

    def header(self) -> list[str]:
        return [h for c in self.rows[0] for h in self._expand(*c)[0]] if self.rows else []

    def write(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, delimiter="\t", quoting=csv.QUOTE_ALL, lineterminator="\n")
            w.writerow(self.header())
            for row in self.rows:
                w.writerow([v for c in row for v in self._expand(*c)[1]])

    def sources(self) -> set[str]:
        hdr = self.header()
        idx = [i for i, h in enumerate(hdr) if h == "Term Source REF"]
        out = set()
        for row in self.rows:
            values = [v for c in row for v in self._expand(*c)[1]]
            out |= {values[i] for i in idx if values[i]}
        return out


def check_structure(header: list[str], template: list[list], what: str, extra_characteristics: set[str] = frozenset()):
    """Written columns must equal the official template: ontology columns followed by Term Source REF / Term
    Accession Number, single columns not. Comment and Factor Value columns and additional Characteristics columns
    (with their Unit/Term columns) are not part of the template."""
    cols = []
    for i, h in enumerate(header):
        if h in ("Unit", "Term Source REF", "Term Accession Number"):
            continue
        if h.startswith(("Comment[", "Factor Value[")) or h in extra_characteristics:
            continue
        onto = i + 1 < len(header) and header[i + 1] == "Term Source REF"
        cols.append([h, "ONTOLOGY_COLUMN" if onto else "SINGLE_COLUMN"])
    expected = [list(x[:2]) for x in template]
    if cols != expected:
        diff = [(c, t) for c, t in zip(cols, expected) if c != t][:3] or [(len(cols), len(expected))]
        raise ValueError(f"{what}: column structure differs from the MetaboLights template: {diff}")


def _value_columns(entries: list[dict]) -> tuple[list[str], set[str]]:
    """Ordered names over all samples and the names that carry a unit."""
    names, units = [], set()
    for e in entries:
        if e["name"] not in names:
            names.append(e["name"])
        if e.get("unit"):
            units.add(e["name"])
    return names, units


def sample_table(study: dict, sample_template: str, used_samples: set[str]) -> tuple[Table, list[str], set[str]]:
    """s_ table from the study samples (only samples with runs); missing template characteristics stay empty."""
    template = templates()["sample"][sample_template]
    tmpl_chars = {h[16:-1].lower(): h[16:-1] for h, *_ in template if h.startswith("Characteristics[")}
    samples = [s for s in study["samples"] if s["sample_name"] in used_samples]
    chars = [dict(c, name=tmpl_chars.get(c["name"].lower(), c["name"])) for s in samples for c in s["characteristics"]]
    char_names, char_units = _value_columns(chars)
    extra = [n for n in char_names if n.lower() not in tmpl_chars]
    factor_names = [f["name"] for f in study["config"].get("factors") or [] if isinstance(f, dict) and f.get("name")]
    fnames, funits = _value_columns([f for s in samples for f in s["factors"]])
    factor_names += [f for f in fnames if f not in factor_names]
    comment_names, _ = _value_columns([c for s in samples for c in s["comments"]])
    last_char = [h for h, *_ in template if h.startswith("Characteristics[")][-1]
    t = Table()
    for s in samples:
        given = {}
        for c in s["characteristics"]:          # names differing only in case merge; a value is never overwritten
            name = tmpl_chars.get(c["name"].lower(), c["name"])
            if c.get("value") or name not in given:
                given[name] = c
        factors = {f["name"]: f for f in s["factors"]}
        comments = {c["name"]: c["value"] for c in s["comments"]}
        row = []
        for header, structure, _, default in template:
            if header == "Source Name":
                row.append((header, s["source_name"], structure))
            elif header == "Sample Name":
                row.append((header, s["sample_name"], structure))
            elif header == "Protocol REF":
                row.append((header, default or "Sample collection", structure))
            elif header.startswith("Characteristics["):
                c = given.get(header[16:-1])
                row.append((header, as_term(c) if c and c.get("value") else EMPTY, structure))
            else:
                row.append((header, "", structure))
            if header == last_char:
                for n in extra:
                    c = given.get(n) or {}
                    value = as_term(c) if c.get("value") else EMPTY
                    row.append((f"Characteristics[{n}]", (value[0], c.get("unit", "")), "UNIT") if n in char_units
                               else (f"Characteristics[{n}]", value, "ONTOLOGY_COLUMN"))
        for n in factor_names:
            f = factors.get(n) or {}
            value = as_term(f) if f.get("value") else EMPTY
            row.append((f"Factor Value[{n}]", (value[0], f.get("unit", "")), "UNIT") if n in funits
                       else (f"Factor Value[{n}]", value, "ONTOLOGY_COLUMN"))
        row += [(f"Comment[{n}]", comments.get(n, ""), "SINGLE_COLUMN") for n in comment_names]
        t.add_row(row)
    return t, factor_names, {f"Characteristics[{n}]" for n in extra}


def nth(text: str, header: str) -> str:
    """'Column model 2' (GCxGC) takes the second ';'-separated part of the assay sheet value."""
    m = re.search(r" (\d+)\]$", header)
    if not m:
        return (text or "").strip()
    parts = [x.strip() for x in (text or "").split(";")]
    i = int(m.group(1)) - 1
    return parts[i] if i < len(parts) else ""


def parameter_value(header: str, assay: dict, run: dict, ms: dict, profile: str):
    name = header[len("Parameter Value["):-1]
    if name in ("Chromatography Instrument", "CE Instrument", "DI Instrument", "FIA Instrument"):
        return (assay.get("injection_system", ""), "", "")
    if name.startswith("Column model"):
        return nth(assay.get("column_model", ""), header)
    if name.startswith("Column type"):
        return controlled("column_type", nth(assay.get("column_type", ""), header), profile)
    return {"Scan polarity": controlled("polarity", run.get("polarity", ""), profile),
            "Scan m/z range": ms["mz_range"], "Instrument": ms["instrument"], "Ion source": ms["ion_source"],
            "Mass analyzer": ms["analyzer"]}.get(name, "")


def assay_table(plan: dict, pa: dict, assay: dict, runs: list[dict], metas: dict, profile: str,
                timestamp_policy: str) -> Table:
    tech = pa["technique"]
    template = templates()["assay"].get(tech)
    if template is None:
        raise InputError(f"assay '{pa['assay']}': no MetaboLights assay template for technique '{tech}' "
                         f"(available: {', '.join(templates()['assay'])})")
    sensitive = plan.get("mode") == "sensitive"
    t = Table()
    for r in runs:
        ms = ms_values(metas.get(r["run"]), assay, profile)
        fixed = {"Sample Name": r["sample"], "Extract Name": r["sample"], "MS Assay Name": r["ms_assay_name"],
                 "Raw Spectral Data File": "" if sensitive else r.get("raw_path", ""),
                 "Derived Spectral Data File": r["derived_path"], "Metabolite Assignment File": pa["maf"]}
        row = []
        for header, structure, _, default in template:
            if header == "Protocol REF":
                value = default or ""
            elif header.startswith("Parameter Value["):
                value = parameter_value(header, assay, r, ms, profile)
            else:
                value = fixed.get(header, "")
            row.append((header, value, structure))
        row.append(("Comment[Raw data file identifier]", r["raw_id"], "SINGLE_COLUMN"))
        if timestamp_policy != "drop":
            row.append(("Comment[Acquisition start time]", ms["start"], "SINGLE_COLUMN"))
        row.append(("Comment[Acquisition software]", ms["software"], "SINGLE_COLUMN"))
        row.append(("Comment[Inferred values]", "; ".join(ms["inferred"]), "SINGLE_COLUMN"))
        t.add_row(row)
    check_structure(t.header(), template, pa["file"])
    return t


# ---- protocols -------------------------------------------------------------------------------------------------

def protocol_list(techniques: list[str]) -> list[dict]:
    seen = {}
    for tech in techniques:
        for p in templates()["protocols"][tech]:
            seen.setdefault(p["name"], p)
    order = PROTOCOL_ORDER + [n for n in seen if n not in PROTOCOL_ORDER]
    return [seen[n] for n in order if n in seen]


def known_protocol_names() -> dict[str, str]:
    return {slug(p["name"]): p["name"] for ps in templates()["protocols"].values() for p in ps}


def protocol_keys(study: dict, names: list[str], assays: list[dict]) -> tuple[dict, dict]:
    """Study-level keys {protocol: [keys]} and assay keys {protocol: {key: [assay keys]}}."""
    known = known_protocol_names()
    by_slug = {slug(n): n for n in names}
    study_keys = {}
    for name, keys in (study["config"].get("protocols") or {}).items():
        if slug(name) not in known:
            raise InputError(f"studies: unknown protocol '{name}' (MetaboLights protocols: {sorted(known.values())})")
        if slug(name) in by_slug:
            study_keys[by_slug[slug(name)]] = [keys] if isinstance(keys, str) else list(keys or [])
    assay_keys: dict[str, dict[str, list[str]]] = {}
    for a in assays:
        for k in a.get("protocol_keys") or []:
            if "=" in k:
                pname, key = (x.strip() for x in k.split("=", 1))
                s = slug(pname) if slug(pname) in known else None
            else:
                key = k
                s = next((s for s in sorted(known, key=len, reverse=True) if k == s or k.startswith(s + "_")), None)
            if s is None:
                raise InputError(f"assay '{a['assay']}': protocol key '{k}' matches no MetaboLights protocol; "
                                 f"use '<Protocol name>=<key>' or a key starting with the protocol slug")
            if s in by_slug:     # keys of protocols the study's techniques do not use are ignored
                assay_keys.setdefault(by_slug[s], {}).setdefault(key, []).append(a["assay"])
    return study_keys, assay_keys


def protocol_text(name: str, texts: Texts, study_keys: dict, assay_keys: dict, n_assays: int, drafted: str) -> str:
    parts = [f"{DRAFT} {drafted}"] if drafted else []
    keys = study_keys.get(name, [])
    akeys = {k: v for k, v in assay_keys.get(name, {}).items() if k not in keys}
    for k in keys:
        parts.append(texts.resolve(k))
    for k, assays in akeys.items():
        text = texts.resolve(k)
        parts.append(text if len(assays) == n_assays else f"{', '.join(assays)}: {text}")
    if not keys and not akeys:
        key = slug(name)
        text = texts.get(key)
        if text:
            parts.append(text)
        elif not (drafted and name == "Data transformation"):
            parts.append(f"[[TODO-PROTOCOL:{key}]]")
    return " ".join(parts)


def _group(sentences: dict[str, str]) -> str:
    """{assay: sentence} -> 'A, B: sentence. C: other.' (all assays: sentence only)."""
    by = {}
    for a, s in sentences.items():
        if s:
            by.setdefault(s, []).append(a)
    if len(by) == 1 and len(next(iter(by.values()))) == len(sentences):
        return next(iter(by))
    return " ".join(f"{', '.join(a)}: {s}" for s, a in by.items())


def separation_draft(assays: list[dict]) -> str:
    out = {}
    for a in assays:
        bits = []
        if a.get("column_model") or a.get("column_type"):
            bits.append(f"column {a.get('column_model') or 'n/a'}" + (f" ({a['column_type']})"
                                                                        if a.get("column_type") else ""))
        if a.get("injection_system"):
            bits.append(f"instrument {a['injection_system']}")
        out[a["assay"]] = sentence(bits)
    return _group(out)


def ms_draft(plan: dict, metas: dict, timestamp_policy: str) -> str:
    sentences = {}
    for pa in plan["assays"]:
        ms = [metas[r] for r in pa["runs"] if r in metas]
        bits = []
        model = pa.get("instrument_model", {}).get("name") or ""
        if model:
            bits.append(f"instrument {model}")
        sources = list(dict.fromkeys(n for m in ms for n, _ in components(m, "source")))
        analyzers = list(dict.fromkeys(n for m in ms[:1] for n, _ in components(m, "analyzer")))
        if sources:
            bits.append(f"ion source {', '.join(sources)}")
        if analyzers:
            bits.append(f"mass analyzer(s) {', '.join(analyzers)}")
        pols = sorted({m["polarity"] for m in ms if m.get("polarity")})
        if pols:
            bits.append(f"{'/'.join(pols)} polarity")
        windows = Counter(f"{m['mz_window'][0]:g}-{m['mz_window'][1]:g}" for m in ms if m.get("mz_window"))
        if windows:
            bits.append("scan m/z range " + ", ".join(w for w, _ in windows.most_common(3)))
        res = Counter(str(r) for m in ms for r in (m.get("resolving_powers") or []))
        if res:
            bits.append("resolving power " + ", ".join(r for r, _ in res.most_common(3)))
        ms2 = sum((m.get("counts") or {}).get("ms2", 0) or 0 for m in ms)
        act = Counter(str(a) for m in ms for a in (m.get("activations") or []))
        if ms2:
            bits.append(f"{ms2} MS/MS spectra in {len(ms)} runs" + (f" ({', '.join(a for a, _ in act.most_common(2))})"
                                                                     if act else ""))
        srm = max(((m.get("chromatograms") or {}).get("srm", 0) or 0 for m in ms), default=0)
        if srm:
            bits.append(f"selected reaction monitoring with up to {srm} transitions per run")
        software = sorted({s for m in ms for s in software_split(m)[0]})
        if software:
            bits.append(f"acquisition software {', '.join(software)}")
        if pa.get("acquisition"):
            bits.append(f"acquisition {pa['acquisition']}")
        starts = sorted(m["acquisition_start"][:10] for m in ms if m.get("acquisition_start"))
        if starts and timestamp_policy != "drop":
            bits.append(f"acquired {starts[0]}" + (f" to {starts[-1]}" if starts[-1] != starts[0] else ""))
        sentences[pa["assay"]] = sentence(bits)
    return _group(sentences)


def transformation_draft(study: dict, plan: dict, metas: dict, finals: dict, timestamp_policy: str) -> str:
    fmt = Counter(r.get("format", "") for r in study["runs"])
    conv = sorted({c for m in metas.values() for c in software_split(m)[1]})
    parts = []
    if fmt.get("vendor_raw"):
        parts.append("Vendor raw data were converted to indexed mzML" + (f" with {', '.join(conv)}" if conv else "")
                     + ".")
    if fmt.get("pwiz_text"):
        parts.append("mzML files were reconstructed from ProteoWizard text exports.")
    if fmt.get("mzml"):
        parts.append("mzML files provided by the submitters were deposited as indexed mzML.")
    modes = {f.get("mode") for f in finals.values()} or {"anonymise" if plan.get("mode") == "sensitive" else "rename"}
    if "anonymise" in modes:
        stamp = {"keep": "", "date": " Acquisition timestamps were reduced to the date.",
                 "drop": " Acquisition timestamps were removed."}[timestamp_policy]
        parts.append("For deposition the mzML files were anonymised: instrument serial numbers, source file names "
                     "and locations, contact details and free-text user parameters were removed, and file and run "
                     "identifiers were set to the deposited names; spectra and chromatograms are unchanged "
                     "(verified by a hash over all binary data arrays)." + stamp)
    elif "rename" in modes:
        parts.append("The mzML file and run identifiers were set to the deposited file names; spectra and "
                     "chromatograms are unchanged.")
    if not plan.get("deposit_raw", True):
        parts.append("Only mzML files are deposited. The vendor raw data files are retained by the submitters and "
                     "are referenced by Comment[Raw data file identifier] in the assay tables.")
    elif any(x.get("packaging") == "zip" for x in plan.get("raw_files", [])):
        parts.append("Vendor raw data in directory formats are deposited as ZIP archives.")
    return " ".join(parts)


# ---- investigation ---------------------------------------------------------------------------------------------

def person(c: dict) -> Person:
    roles = []
    for r in c.get("roles") or []:
        roles.append(oa(tuple(cv()["person_roles"].get(r.strip().lower(), (r.strip(), "", "")))))
    return Person(last_name=c.get("last_name", ""), first_name=c.get("first_name", ""),
                  mid_initials=c.get("mid_initials", ""), email=c.get("email", ""), phone=c.get("phone", ""),
                  address=c.get("address", ""), affiliation=c.get("affiliation", ""),
                  affiliation_ror_id=c.get("affiliation_ror_id", "") or c.get("ror", ""), orcid=c.get("orcid", ""),
                  roles=roles)


def publications(study: dict) -> list[Publication]:
    src = study["config"].get("publications") or study["config"].get("publication") or study.get("publication")
    if isinstance(src, dict) and "title" not in src and study["study"] in src:
        src = src[study["study"]]
    out = []
    for p in ([src] if isinstance(src, dict) else src or []):
        authors = p.get("author_list") or p.get("authors") or ""
        status = p.get("status") or ""
        st = (tuple(cv()["publication_status"].get(status.lower(), (status, "", ""))) if isinstance(status, str)
              else as_term(status))
        out.append(Publication(title=str(p.get("title", "")), doi=str(p.get("doi", "")),
                               pub_med_id=str(p.get("pubmed_id") or p.get("pub_med_id") or ""),
                               author_list=", ".join(authors) if isinstance(authors, list) else str(authors),
                               status=oa(st)))
    return out


def related(cfg: dict) -> list[RelatedDataset]:
    out = []
    for r in cfg.get("related_studies") or []:
        if isinstance(r, str):
            repo = "MetaboLights" if re.match(r"^MTBLS\d+$", r) else ""
            out.append(RelatedDataset(repository=repo, accession=r))
        elif isinstance(r, dict):
            out.append(RelatedDataset(repository=str(r.get("repository", "")), accession=str(r.get("accession", "")),
                                      url=str(r.get("url", ""))))
    return out


def funders(cfg: dict) -> list[Funder]:
    out = []
    for f in cfg.get("funders") or []:
        if isinstance(f, dict):
            grants = f.get("grant_ids") or f.get("grants") or []
            out.append(Funder(funder_name=str(f.get("name", "")), funder_id=str(f.get("ror_id") or f.get("id") or ""),
                              grant_ids=[grants] if isinstance(grants, str) else [str(g) for g in grants]))
    return out


def design_descriptors(cfg: dict, plan: dict, assays: dict) -> list[ExtendedOntologyAnnotation]:
    terms = []
    for d in cfg.get("design_descriptors") or []:
        t = as_term(d)
        terms.append((t, d.get("category", "") if isinstance(d, dict) else ""))
    for a in plan["assays"]:
        mt = (assays.get(a["assay"], {}).get("measurement_type") or "").lower()
        auto = [cv()["profiling_design"].get(mt), cv()["technique_design"].get(a["technique"])]
        terms += [(tuple(t), "") for t in auto if t]
    out, seen = [], set()
    for t, cat in terms:
        if t[0] and t[0].lower() not in seen:
            seen.add(t[0].lower())
            out.append(oa(t, ExtendedOntologyAnnotation, category=cat))
    return out


def assay_entry(pa: dict, assay: dict, metas: dict) -> Assay:
    mt = (assay.get("measurement_type") or "").lower()
    measurement = tuple(cv()["measurement_type"].get(mt, (assay.get("measurement_type", ""), "", "")))
    acq = re.split(r"[/;, ]+", (pa.get("acquisition") or assay.get("acquisition") or "").lower())
    descriptors = [t for a in acq for t in cv()["acquisition_descriptors"].get(a, [])]
    descriptors = [oa(tuple(t), ExtendedOntologyAnnotation, category="acquisition-method")
                   for t in {tuple(x): None for x in descriptors}]
    counts = [metas[r].get("counts") or {} for r in pa["runs"] if r in metas]
    for kind in ("centroid", "profile"):
        if any(c.get(kind) for c in counts):
            descriptors.append(oa(tuple(cv()["spectrum_representation"][kind]), ExtendedOntologyAnnotation))
    model = (pa.get("instrument_model") or {}).get("name", "")
    return Assay(file_name=pa["file"], measurement_type=oa(measurement),
                 technology_type=oa(tuple(cv()["technology_type"])),
                 technology_platform=f"{pa['technique']} - {model}" if model else pa["technique"],
                 assay_identifier=pa["file"].removesuffix(".txt"),
                 assay_type=oa(tuple(templates()["assay_types"].get(pa["technique"], EMPTY))),
                 omics_type=oa(EMPTY), assay_descriptors=descriptors)


def build_investigation(study: dict, plan: dict, metas: dict, finals: dict, texts: Texts, args,
                        factor_names: list[str]) -> Investigation:
    cfg = study["config"]
    assays = {a["assay"]: a for a in study["assays"]}
    techniques = list(dict.fromkeys(pa["technique"] for pa in plan["assays"]))
    title = str(cfg.get("title") or "[[TODO-TEXT:title]]")
    description = str(cfg.get("description") or texts.resolve("study_description", "TEXT"))
    release = str(cfg.get("release_date") or "")
    study_m = Study(
        identifier=str(plan.get("accession") or ""), title=title, description=description, submission_date="",
        public_release_date=release, file_name=plan["sample_file"], study_category=args.study_category,
        template_version=templates()["template_version"],
        sample_template=SAMPLE_TEMPLATE.get(args.study_category, "minimum"), study_template="minimum",
        dataset_license=str(cfg.get("license") or ""),
        created_at=args.created_at or datetime.date.today().isoformat(), related_datasets=related(cfg),
        funders=funders(cfg))
    study_m.study_design_descriptors.design_types = design_descriptors(cfg, plan, assays)
    pubs = publications(study)
    study_m.study_publications.publications = pubs
    types = {f["name"]: as_term(f.get("type")) for f in cfg.get("factors") or [] if isinstance(f, dict)}
    study_m.study_factors.factors = [Factor(name=n, type=oa(types.get(n, EMPTY))) for n in factor_names]
    omics = cv()["omics_type"].get(str(cfg.get("omics_type") or "metabolomics").lower())
    for pa in plan["assays"]:
        a = assay_entry(pa, assays.get(pa["assay"], {}), metas)
        a.omics_type = oa(tuple(omics) if omics else (str(cfg.get("omics_type")), "", ""))
        study_m.study_assays.assays.append(a)

    protos = protocol_list(techniques)
    names = [p["name"] for p in protos]
    plan_assays = [assays.get(pa["assay"], {"assay": pa["assay"]}) for pa in plan["assays"]]
    study_keys, assay_keys = protocol_keys(study, names, plan_assays)
    drafted = {"Mass spectrometry": ms_draft(plan, metas, args.timestamp_policy),
               "Data transformation": transformation_draft(study, plan, metas, finals, args.timestamp_policy)}
    for n in ("Chromatography", "Capillary Electrophoresis", "Direct infusion", "Flow Injection Analysis"):
        sep = [a for a, pa in zip(plan_assays, plan["assays"])
               if any(p["name"] == n for p in templates()["protocols"][pa["technique"]])]
        drafted[n] = separation_draft(sep)
    for p in protos:
        ptype = tuple(cv()["protocol_type"].get(p["name"], p["type"]))
        study_m.study_protocols.protocols.append(Protocol(
            name=p["name"], protocol_type=oa(ptype),
            description=protocol_text(p["name"], texts, study_keys, assay_keys, len(plan_assays),
                                      drafted.get(p["name"], "")),
            parameters=[ParameterDefinition(term=x) for x in p["parameters"]]))
    study_m.study_contacts.people = [person(c) for c in study.get("contacts") or []]
    inv = Investigation(identifier=str(plan.get("accession") or ""), title=title, description=description,
                        submission_date="", public_release_date=release, studies=[study_m])
    inv.investigation_publications.publications = list(pubs)
    inv.investigation_contacts.people = [person(c) for c in study.get("contacts") or []]
    return inv


def term_sources(obj) -> set[str]:
    if isinstance(obj, dict):
        out = {obj["term_source_ref"]} if obj.get("term_source_ref") else set()
        return out.union(*(term_sources(v) for v in obj.values()))
    if isinstance(obj, list):
        return set().union(*(term_sources(v) for v in obj)) if obj else set()
    return set()


def source_references(names: set[str]) -> list[OntologySourceReference]:
    known = {k.lower(): (k, v) for k, v in templates()["ontology_sources"].items()}
    out = []
    for n in sorted(names, key=str.lower):
        k, (file, version, desc) = known.get(n.lower(), (n, ("", "", "")))
        out.append(OntologySourceReference(source_name=n, source_file=file, source_version=version,
                                           source_description=desc))
    return out


# ---- main ------------------------------------------------------------------------------------------------------

def write_investigation(inv: Investigation, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    result = Writer.get_investigation_file_writer().write(inv, str(path), values_in_quotation_mark=True,
                                                          verify_file_after_update=True)
    report = getattr(result, "parser_report", None)
    errors = [m for m in (report.messages if report else []) if str(m.type).upper().endswith(("ERROR", "CRITICAL"))]
    if errors:
        raise RuntimeError(f"{path.name}: investigation file does not re-read cleanly: {errors[:3]}")


def build(study: dict, plan: dict, metadata: dict, finals: dict, texts: Texts, args) -> dict:
    """All ISA files of the study as {file name: Table | Investigation}."""
    if plan["study"] != study["study"]:
        raise InputError(f"plan is for study '{plan['study']}', study.json for '{study['study']}'")
    for f in finals.values():
        planned = next((r for r in plan["runs"] if r["run"] == f["run"]), None)
        if planned and f.get("deposited_mzml") and f["deposited_mzml"] != planned["deposited_mzml"]:
            raise InputError(f"finalise.json of run '{f['run']}' does not match the plan's deposited name")
    metas = {r: public_metadata(m, args.timestamp_policy) for r, m in metadata.items()
             if r in {x["run"] for x in plan["runs"]}}
    assays = {a["assay"]: a for a in study["assays"]}
    runs = {r["run"]: r for r in plan["runs"]}
    sample_template = SAMPLE_TEMPLATE.get(args.study_category, "minimum")
    st, factor_names, extra = sample_table(study, sample_template, {r["sample"] for r in plan["runs"]})
    check_structure(st.header(), templates()["sample"][sample_template], plan["sample_file"], extra)
    files: dict = {plan["sample_file"]: st}
    for pa in plan["assays"]:
        files[pa["file"]] = assay_table(plan, pa, assays.get(pa["assay"], {}), [runs[r] for r in pa["runs"]], metas,
                                        args.controlled_value_profile, args.timestamp_policy)
    inv = build_investigation(study, plan, metas, finals, texts, args, factor_names)
    used = term_sources(inv.model_dump()) | set().union(*(t.sources() for t in files.values()))
    inv.ontology_source_references.references = source_references(used)
    files[plan.get("investigation_file") or "i_Investigation.txt"] = inv
    return files


def run(args) -> int:
    study = read_json(args.study_json, "msdeposit/study/1")
    plan = read_json(args.plan, "msdeposit/plan/1")
    metadata = load_metadata(args.metadata) if args.metadata else {}
    finals = load_artefacts(args.finalise, "msdeposit/finalise/1") if args.finalise else {}
    texts = Texts(args.protocols, study["study"])
    files = build(study, plan, metadata, finals, texts, args)
    for name, obj in files.items():
        if safe_name(name) != name:
            raise InputError(f"ISA file name '{name}' contains characters MetaboLights does not allow")
        if isinstance(obj, Table):
            obj.write(args.outdir / name)
        else:
            write_investigation(obj, args.outdir / name)
    todo = sum((args.outdir / n).read_text(encoding="utf-8").count("[[TODO-") for n in files)
    print(f"{study['study']}: {len(files)} ISA files, {len(plan['runs'])} assay rows, {todo} TODO placeholders")
    return 0
