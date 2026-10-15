"""mztab: mzTab-M summary files (MTD + SML) per study, in each selected version (2.0.0-M, 2.1.0-M).

One version-independent model, two serialisations:
  - ms_run per plan run: location relative to the MetaboLights study root (`file:///FILES/DERIVED_FILES/...`), SHA-1
    of the deposited mzML (finalise.json), nativeID format and polarity from the plan; assay per run; sample per ISA
    sample; instrument per assay model (components from the mzML metadata)
  - study_variable per `study_variables` row; assay_refs = the runs of its member samples. Without study variables a
    single 'undefined' study variable references all assays (mzTab-M requires one)
  - SML per feature key: abundance_assay from per-run values, abundance_study_variable from study-variable values,
    abundance_variation_study_variable only where `variation_type` declares what the number is (`sd`, `cv_percent`,
    one type per study variable; `cv_percent` stays in percent, as the description says). Nothing is inferred from an
    undeclared variation column
  - quantification unit from the `unit` column of the values (one unit per file): 2.0 as NCIT unit (as required by
    the 2.0 summary CV mapping), 2.1 as the UO term
  - database identifiers from the features sheet, else SwissLipids ids / Goslin names of the lipid annotation; the
    SwissLipids entity URL goes into `uri`; a feature without identifier gets the mzTab-M 'no database' entry
    (`null:<name>`), because chemical_name must have as many entries as database_identifier. No `opt_` columns:
    jmzTab-m 1.0.6 aborts silently when a wide file has more than one opt column
  - reliability: lipid shorthand levels (MS:1003310; from the `level` column or the Goslin level of the name) or MSI
    levels 1-4 (MS:1002896), whichever the features use more; a missing level is written as the scheme's lowest level
small_molecule-identification_reliability is always declared (jmzTab-m 1.0.6 aborts without it). 2.1 additionally
declares the mzTab profile (M+S); 2.0 keeps the fields jmzTab-m 1.0.6 requires
(small_molecule_feature-quantification_unit, id_confidence_measure).

Output (CONTRACTS.md §6): <prefix>.mztab-<version>.mzTab per version.
"""

import gzip
import re
from collections import Counter
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version as pkg_version
from importlib.resources import files
from pathlib import Path

from .. import __version__
from ..io import InputError, read_json
from ..io import load_metadata
from .lipids import SWISSLIPIDS_ENTITY, load_annotations
from .maf import database_info, DATABASE_URIS
from ..io import load_finalise
from .mzqc import LOCATION_ROOT

VERSIONS = ("2.0.0-M", "2.1.0-M")
CVS = {"MS": ("PSI-MS controlled vocabulary", "4.2.2",
              "https://github.com/HUPO-PSI/psi-ms-CV/releases/download/v4.2.2/psi-ms.obo"),
       "UO": ("Units of Measurement Ontology", "2026-07-31", "http://purl.obolibrary.org/obo/uo.obo"),
       "NCIT": ("NCI Thesaurus", "25.09d", "http://purl.obolibrary.org/obo/ncit.owl"),
       "NCBITaxon": ("NCBI organismal classification", "2025-01-01", "http://purl.obolibrary.org/obo/ncbitaxon.owl"),
       "BTO": ("BRENDA tissue ontology", "2021-10-26", "http://purl.obolibrary.org/obo/bto.owl"),
       "STATO": ("Statistics Ontology", "2018-06-15", "http://purl.obolibrary.org/obo/stato.owl"),
       "PRIDE": ("PRIDE Controlled Vocabulary", "16:10:2023 11:38", "https://www.ebi.ac.uk/ols4/ontologies/pride")}
NCIT_UNITS = {"UO:0000062": ("NCIT:C48555", "Mole per Liter"), "UO:0000063": ("NCIT:C64387", "Millimole per Liter"),
              "UO:0000064": ("NCIT:C48508", "Micromole per Liter"), "UO:0000065": ("NCIT:C67432", "Nanomole per Liter"),
              "UO:0000066": ("NCIT:C67434", "Picomole per Liter")}
ARBITRARY_UNIT = ("PRIDE", "PRIDE:0000330", "Arbitrary quantification unit")
AVERAGE = {"median": ("MS", "MS:1002883", "median"), "mean": ("MS", "MS:1002962", "mean")}
VARIATION = {"cv_percent": ("MS", "MS:1002963", "variation coefficient"),
             "sd": ("STATO", "STATO:0000237", "standard deviation")}
NATIVE_ID_NONE = {"accession": "MS:1000824", "name": "no nativeID format"}
POLARITY = {"positive": [("MS", "MS:1000130", "positive scan")], "negative": [("MS", "MS:1000129", "negative scan")],
            "alternating": [("MS", "MS:1000130", "positive scan"), ("MS", "MS:1000129", "negative scan")]}
# Goslin level / free-text level -> value of MS:1003310 'Lipid shorthand identification confidence level'
LIPID_LEVELS = {"category": "Category", "class": "Class", "species": "Species",
                "phosphate-position": "Phosphate-position", "molecular species": "Molecular species",
                "molecule_species": "Molecular species", "sn-position": "sn-position", "sn_position": "sn-position",
                "dbe position": "DBE position", "structure defined": "Structure defined",
                "structure_defined": "Structure defined", "full structure": "Full structure",
                "full_structure": "Full structure", "complete structure": "Complete structure",
                "complete_structure": "Complete structure"}
# identification without a database: the mzTab-M 'no database' entry (prefix null) with the reported name as id
REPORTED_NAME = dict(database="no database", prefix="null")
ADDUCT = re.compile(r"^\[\d*M([+-][\w\d]+)*\]\d*[+-]$")


def add_arguments(p):
    p.add_argument("--study-json", required=True, type=Path)
    p.add_argument("--plan", required=True, type=Path)
    p.add_argument("--metadata", nargs="*", type=Path, default=[], help="metadata.json files or directories")
    p.add_argument("--finalise", nargs="*", type=Path, default=[], help="finalise.json files or directories")
    p.add_argument("--annotations", type=Path, help="lipid_annotation.tsv")
    p.add_argument("--versions", default="2.0.0-M", help="comma-separated: 2.0.0-M, 2.1.0-M")
    p.add_argument("--outdir", type=Path, default=Path("."))


def file_name(prefix: str, version: str) -> str:
    return f"{prefix}.mztab-{version}.mzTab"


def esc(s) -> str:
    """mzTab values: no tabs or line breaks; values with commas are quoted."""
    s = re.sub(r"[\t\r\n]+", " ", str(s)).strip()
    return f'"{s}"' if "," in s and not (s.startswith('"') and s.endswith('"')) else s


def cell(v) -> str:
    return re.sub(r"[\t\r\n]+", " ", str(v)).strip()


def name_id(name: str) -> str:
    """A reported name as identifier part ('|' separates alternatives in mzTab-M)."""
    return cell(name).replace("|", "/")


def num(v) -> str:
    if v in (None, ""):
        return "null"
    s = f"{float(v):.10f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def goslin_version() -> str:
    try:
        return pkg_version("pygoslin")
    except PackageNotFoundError:
        return ""


@lru_cache(maxsize=None)
def uo_names() -> dict[str, str]:
    """UO accession -> name from the Unit Ontology copy vendored with psims (offline)."""
    path = files("psims.controlled_vocabulary.vendor") / "unit.obo.gz"
    names, current = {}, None
    with gzip.open(str(path), "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("id: "):
                current = line[4:].strip()
            elif line.startswith("name: ") and current:
                names[current] = line[6:].strip()
                current = None
    return names


def _term(value: str, accession: str) -> tuple[str, str, str] | None:
    """('NCBITaxon', 'NCBITaxon:9606', value) from an ISA accession URI/CURIE, None when there is none."""
    m = re.search(r"([A-Za-z]+)[_:](\d+)$", accession or "")
    return (m[1], f"{m[1]}:{m[2]}", value) if m else None


def _characteristic(sample: dict, *names: str) -> tuple[str, str] | None:
    for c in sample.get("characteristics", []):
        if c.get("name", "").lower() in names and c.get("value"):
            return c["value"], c.get("accession", "")
    return None


class Model:
    """The version-independent content of the mzTab-M file of one study."""

    def __init__(self, study: dict, plan: dict, metadata: dict, finalise: dict, annotations: dict):
        self.study, self.plan, self.config = study, plan, study.get("config", {})
        self.warnings: list[str] = []
        self.annotations = annotations
        self.runs = []
        for r in plan["runs"]:
            if r.get("polarity") not in POLARITY:
                self.warnings.append(f"run {r['ms_assay_name']}: polarity unknown - omitted (scan polarity is "
                                     f"mandatory)")
                continue
            fin = finalise.get(r["run"]) or {}
            if not fin.get("sha1"):
                self.warnings.append(f"run {r['ms_assay_name']}: no SHA-1 of the deposited mzML")
            self.runs.append(dict(r, sha1=fin.get("sha1", "")))
        self._instruments(metadata)
        self.samples = list(dict.fromkeys(r["sample"] for r in self.runs))
        self.sample_info = {s["sample_name"]: s for s in study.get("samples", [])}
        self._study_variables()
        self._values()
        self._features()
        self.srm = bool(study.get("transitions")) or any((metadata.get(r["run"]) or {}).get("transitions")
                                                         for r in self.runs)

    def _instruments(self, metadata: dict):
        self.instruments, self.instrument_of = [], {}
        for a in self.plan["assays"]:
            model = a.get("instrument_model") or {}
            if not model.get("name"):
                continue
            meta = next((metadata[r] for r in a["runs"] if r in metadata), None)
            comps = {"source": [], "analyzer": [], "detector": []}
            for ic in ((meta or {}).get("instrument") or {}).get("configurations") or []:
                for c in ic.get("components", []):
                    if c.get("type") in comps:
                        comps[c["type"]] += [t[1] for t in c.get("terms", []) if "inlet" not in t[1]]
            key = (model["name"], model.get("accession", ""))
            known = [i for i, x in enumerate(self.instruments) if x["key"] == key]
            if known:
                idx = known[0]
            else:
                self.instruments.append(dict(key=key, components={k: list(dict.fromkeys(v)) for k, v in comps.items()}))
                idx = len(self.instruments) - 1
            for r in a["runs"]:
                self.instrument_of[r] = idx + 1

    def _study_variables(self):
        run_idx: dict[str, list[int]] = {}
        for i, r in enumerate(self.runs, 1):
            run_idx.setdefault(r["sample"], []).append(i)
        self.svs = []
        for sv in self.study.get("study_variables", []):
            members = [m["sample"] for m in self.study.get("study_variable_members", [])
                       if m["study_variable"] == sv["study_variable"]]
            refs = sorted({i for s in members for i in run_idx.get(s, [])})
            if not refs:
                self.warnings.append(f"study variable {sv['study_variable']}: no deposited runs of its member "
                                     f"samples - omitted")
                continue
            self.svs.append(dict(key=sv["study_variable"], description=sv.get("description") or "",
                                 average=(sv.get("average_function") or "").strip(), refs=refs))
        if not self.svs:
            self.svs.append(dict(key="undefined", description="", average="", refs=list(range(1, len(self.runs) + 1))))
        for sv in self.svs:
            if sv["average"] and sv["average"].lower() not in AVERAGE:
                self.warnings.append(f"study variable {sv['key']}: average function '{sv['average']}' is not "
                                     f"one of {sorted(AVERAGE)} - written as user parameter")

    def _values(self):
        values = self.study.get("values", [])
        units = Counter(v.get("unit") for v in values if v.get("unit"))
        if len(units) > 1:
            raise InputError(f"values use several units {sorted(units)}; mzTab-M has one quantification unit per file")
        self.unit = next(iter(units), "")
        if values and not self.unit:
            self.warnings.append("values have no unit - declared as arbitrary quantification unit")
        run_of = {r["run"]: i for i, r in enumerate(self.runs, 1)}
        sv_of = {sv["key"]: k for k, sv in enumerate(self.svs, 1)}
        self.assay_values, self.sv_values, types = {}, {}, {}
        for v in values:
            key = None
            if v.get("run"):
                if v["run"] in run_of:
                    key = (v["feature"], run_of[v["run"]])
                    target = self.assay_values
            elif v.get("study_variable") in sv_of:
                key = (v["feature"], sv_of[v["study_variable"]])
                target = self.sv_values
                vt = v.get("variation_type") or ""
                if v.get("variation") is not None and vt in VARIATION:
                    types.setdefault(key[1], set()).add(vt)
                elif v.get("variation") is not None:
                    self.warnings.append(f"feature {v['feature']}, study variable {v['study_variable']}: variation "
                                         f"without a declared variation_type - not written")
            if key is None:
                continue
            if key in target:
                self.warnings.append(f"feature {v['feature']}: duplicate value for "
                                     f"{v.get('run') or v.get('study_variable')} - first one kept")
                continue
            target[key] = v
        self.variation = {}
        for k, ts in types.items():
            if len(ts) == 1:
                self.variation[k] = next(iter(ts))
            else:
                self.warnings.append(f"study variable {self.svs[k - 1]['key']}: mixed variation types "
                                     f"{sorted(ts)} - variation not written")

    def _features(self):
        self.features: dict[str, dict] = {}
        for f in self.study.get("features", []):
            entry = self.features.setdefault(f["feature"], dict(f, adducts=[]))
            add = (f.get("adduct") or "").strip()
            if add and ADDUCT.match(add) and add not in entry["adducts"]:
                entry["adducts"].append(add)
            elif add and not ADDUCT.match(add):
                self.warnings.append(f"feature {f['feature']}: adduct '{add}' is not in mzTab-M notation - omitted")
        for f in self.features.values():
            given = str(f.get("level") or "").strip()
            goslin_levels = {a.get("goslin_level") or "" for a in self.annotations.get(f.get("name") or "", [])}
            goslin_level = next(iter(goslin_levels)) if len(goslin_levels) == 1 else ""
            f["_lipid"] = LIPID_LEVELS.get(given.lower()) or LIPID_LEVELS.get(goslin_level.lower(), "")
            f["_msi"] = given if given in ("1", "2", "3", "4") else ""
        n_lipid = sum(1 for f in self.features.values() if f["_lipid"])
        n_msi = sum(1 for f in self.features.values() if f["_msi"])
        self.lipid_levels = n_lipid > 0 and n_lipid >= n_msi
        missing = sum(1 for f in self.features.values() if not f["_lipid" if self.lipid_levels else "_msi"])
        if missing:
            self.warnings.append(f"{missing} features have no identification level in the "
                                 f"{'lipid shorthand' if self.lipid_levels else 'MSI'} scheme - written as the "
                                 f"lowest level ({'Category' if self.lipid_levels else '4'})")

    def reliability(self, f: dict) -> str:
        """mzTab-M requires a value: the declared scheme's level, else its lowest level (never overstated)."""
        if self.lipid_levels:
            return f["_lipid"] or "Category"
        return f["_msi"] or "4"

    def identification(self, f: dict) -> dict:
        """Per-alternative identification columns ('|'-separated, equal counts) and the databases used."""
        ann = self.annotations.get(f.get("name") or "", [])
        if f.get("database_id") or not ann:
            ids = [x.strip() for x in str(f.get("database_id") or "").split("|") if x.strip()]
            known = [i for i in map(database_info, ids) if i]
            if len(known) < len(ids):
                self.warnings.append(f"feature {f['feature']}: unrecognised database identifier - not written")
            if not known:
                known = [REPORTED_NAME | dict(curie=f"null:{name_id(f.get('name') or f['feature'])}", uri="null")]
            n = len(known)
            return dict(ids=[i["curie"] for i in known], dbs=known, uris=[i["uri"] for i in known],
                        formula=[f.get("formula") or "null"] * n, names=[f.get("name") or "null"] * n,
                        mass=["null"] * n, n=n)
        out = dict(ids=[], dbs=[], uris=[], formula=[], names=[], mass=[], n=len(ann))
        for a in ann:
            if a.get("swisslipids_id"):
                info = database_info(a["swisslipids_id"]) or dict(
                    database="SwissLipids", prefix="SLM", curie=a["swisslipids_id"],
                    uri=SWISSLIPIDS_ENTITY.format(a["swisslipids_id"]))
                out["ids"].append(info["curie"])
                out["dbs"].append(info)
                out["uris"].append(info["uri"])
            elif a.get("goslin_name"):
                out["ids"].append(f"goslin:{a['goslin_name']}")
                out["dbs"].append(dict(database="goslin", prefix="goslin"))
                out["uris"].append("null")
            else:
                out["ids"].append(f"null:{name_id(a.get('alternative') or f['feature'])}")
                out["dbs"].append(REPORTED_NAME)
                out["uris"].append("null")
            out["formula"].append(a.get("formula") or "null")
            out["names"].append(a.get("alternative") or "null")
            out["mass"].append(a.get("neutral_mass") or "null")
        if len(ann) == 1 and f.get("formula"):
            out["formula"] = [f["formula"]]
        return out


class Writer:
    """Serialises a Model in one mzTab-M version; collects the CV labels used for the cv[] section."""

    def __init__(self, model: Model, version: str):
        self.m, self.version, self.v21 = model, version, version == "2.1.0-M"
        self.cvs: list[str] = []

    def p(self, cv: str, acc: str, name: str, value: str = "") -> str:
        if cv and cv not in self.cvs:
            self.cvs.append(cv)
        return f"[{cv}, {acc}, {esc(name)}, {esc(value)}]" if cv else f"[,, {esc(name)}, {esc(value)}]"

    def unit(self) -> str:
        u = self.m.unit
        if not u:
            return self.p(*ARBITRARY_UNIT)
        if not self.v21:
            if u in NCIT_UNITS:
                return self.p("NCIT", *NCIT_UNITS[u])
            self.m.warnings.append(f"unit {u} has no NCIT mapping for mzTab-M 2.0 - written as user parameter")
            return self.p("", "", uo_names().get(u, u))
        if u in uo_names():
            return self.p("UO", u, uo_names()[u])
        return self.p("", "", u)

    def metadata(self) -> list[str]:
        m, cfg = self.m, self.m.config
        L = [f"MTD\tmzTab-version\t{self.version}"]
        if self.v21:
            L.append("MTD\tmzTab-profile\tM+S")
        L += [f"MTD\tmzTab-ID\t{m.plan['prefix']}", f"MTD\ttitle\t{esc(cfg.get('title') or m.plan['study'])}"]
        notes = ["Run locations are relative to the MetaboLights study root."]
        if "cv_percent" in m.variation.values():
            notes.append("Coefficients of variation are given in percent.")
        L.append("MTD\tdescription\t" + esc(" ".join(x for x in [cfg.get("description") or ""] + notes if x)))
        c = (m.study.get("contacts") or [None])[0]
        if c:
            name = " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x)
            L += [f"MTD\tcontact[1]-name\t{esc(name)}"]
            L += [f"MTD\tcontact[1]-affiliation\t{esc(c['affiliation'])}"] if c.get("affiliation") else []
            L += [f"MTD\tcontact[1]-email\t{c['email']}"] if c.get("email") else []
        for i, inst in enumerate(m.instruments, 1):
            name, acc = inst["key"]
            if acc and "instrument model" not in name:
                L.append(f"MTD\tinstrument[{i}]-name\t{self.p('MS', acc, name)}")
            else:
                L.append(f"MTD\tinstrument[{i}]-name\t{self.p('MS', 'MS:1000031', 'instrument model', name)}")
            comps = inst["components"]
            if comps["source"]:
                L.append(f"MTD\tinstrument[{i}]-source\t{self.p('', '', comps['source'][0])}")
            L += [f"MTD\tinstrument[{i}]-analyzer[{k}]\t{self.p('', '', n)}"
                  for k, n in enumerate(comps["analyzer"], 1)]
            if comps["detector"]:
                L.append(f"MTD\tinstrument[{i}]-detector\t{self.p('', '', comps['detector'][0])}")
        tool = self.p("MS", "MS:1000799", "custom unreleased software tool", f"msdeposit-tools {__version__}")
        L.append(f"MTD\tsoftware[1]\t{tool}")
        if any(a.get("goslin_name") for rows in m.annotations.values() for a in rows):
            L.append(f"MTD\tsoftware[2]\t{self.p('MS', 'MS:1003309', 'Goslin', goslin_version())}")
        qm = cfg.get("quantification_method")
        if isinstance(qm, dict) and qm.get("accession"):
            acc = qm["accession"].rsplit("/", 1)[-1].replace("_", ":")
            L.append(f"MTD\tquantification_method\t{self.p(acc.split(':')[0], acc, qm.get('term', ''))}")
        elif m.srm:
            L.append(f"MTD\tquantification_method\t{self.p('MS', 'MS:1001838', 'SRM quantitation analysis')}")
        elif m.unit in NCIT_UNITS:
            L.append(f"MTD\tquantification_method\t{self.p('MS', 'MS:1002514', 'absolute quantitation analysis')}")
        else:
            L.append(f"MTD\tquantification_method\t"
                     f"{self.p('MS', 'MS:1001834', 'LC-MS label-free quantitation analysis')}")
        for i, name in enumerate(m.samples, 1):
            L.append(f"MTD\tsample[{i}]\t{esc(name)}")
            info = m.sample_info.get(name, {})
            org = _characteristic(info, "organism")
            if org:
                t = _term(*org) if "NCBITaxon" in (org[1] or "") else None
                L.append(f"MTD\tsample[{i}]-species[1]\t" + (self.p(*t) if t else self.p("", "", org[0])))
            part = _characteristic(info, "organism part")
            if part:
                t = _term(*part)
                L.append(f"MTD\tsample[{i}]-tissue[1]\t" + (self.p(*t) if t else self.p("", "", part[0])))
        for i, r in enumerate(m.runs, 1):
            nid = r.get("nativeid_format") or {}
            nid = nid if nid.get("accession") else NATIVE_ID_NONE
            L += [f"MTD\tms_run[{i}]-location\t{LOCATION_ROOT}{r['derived_path']}",
                  f"MTD\tms_run[{i}]-format\t{self.p('MS', 'MS:1000584', 'mzML format')}",
                  f"MTD\tms_run[{i}]-id_format\t{self.p('MS', nid['accession'], nid['name'])}"]
            if r["run"] in m.instrument_of:
                L.append(f"MTD\tms_run[{i}]-instrument_ref\tinstrument[{m.instrument_of[r['run']]}]")
            L += [f"MTD\tms_run[{i}]-scan_polarity[{j}]\t{self.p(*t)}"
                  for j, t in enumerate(POLARITY[r["polarity"]], 1)]
            if r["sha1"]:
                L += [f"MTD\tms_run[{i}]-hash_method\t{self.p('MS', 'MS:1000569', 'SHA-1')}",
                      f"MTD\tms_run[{i}]-hash\t{r['sha1']}"]
        for i, r in enumerate(m.runs, 1):
            L += [f"MTD\tassay[{i}]\t{esc(r['ms_assay_name'])}",
                  f"MTD\tassay[{i}]-sample_ref\tsample[{m.samples.index(r['sample']) + 1}]",
                  f"MTD\tassay[{i}]-ms_run_ref\tms_run[{i}]"]
        for k, sv in enumerate(m.svs, 1):
            L += [f"MTD\tstudy_variable[{k}]\t{esc(sv['key'])}",
                  f"MTD\tstudy_variable[{k}]-assay_refs\t" + "|".join(f"assay[{i}]" for i in sv["refs"])]
            if sv["average"]:
                avg = AVERAGE.get(sv["average"].lower())
                L.append(f"MTD\tstudy_variable[{k}]-average_function\t"
                         + (self.p(*avg) if avg else self.p("", "", sv["average"])))
            if k in m.variation:
                L.append(f"MTD\tstudy_variable[{k}]-variation_function\t{self.p(*VARIATION[m.variation[k]])}")
            if sv["description"]:
                L.append(f"MTD\tstudy_variable[{k}]-description\t{esc(sv['description'])}")
        unit = self.unit()
        L.append(f"MTD\tsmall_molecule-quantification_unit\t{unit}")
        if not self.v21:
            L.append(f"MTD\tsmall_molecule_feature-quantification_unit\t{unit}")
        # always declared: jmzTab-m 1.0.6 aborts (exception, no result) when the declaration is missing
        L.append("MTD\tsmall_molecule-identification_reliability\t"
                 + (self.p("MS", "MS:1003310", "Lipid shorthand identification confidence level") if m.lipid_levels
                    else self.p("MS", "MS:1002896", "compound identification confidence level")))
        L += self.databases()
        if not self.v21:
            L.append(f"MTD\tid_confidence_measure[1]\t{self.confidence_measure()}")
        cv_lines = []
        for i, label in enumerate(self.cvs, 1):
            full, ver, uri = CVS.get(label, (label, "unknown", f"http://purl.obolibrary.org/obo/{label.lower()}.owl"))
            cv_lines += [f"MTD\tcv[{i}]-label\t{label}", f"MTD\tcv[{i}]-full_name\t{esc(full)}",
                         f"MTD\tcv[{i}]-version\t{esc(ver)}", f"MTD\tcv[{i}]-uri\t{uri}"]
        return L + cv_lines

    def confidence_measure(self) -> str:
        if self.m.lipid_levels:
            return self.p("MS", "MS:1003311", "Lipid empirical score")
        return self.p("", "", "no identification confidence measure reported")

    def databases(self) -> list[str]:
        used = {}
        for f in self.m.features.values():
            for d in self.m.identification(f)["dbs"]:
                used.setdefault(d["prefix"], d)
        entries = []   # (param, prefix, version, uri)
        for prefix, d in used.items():
            if prefix == "null":
                entries.append((self.p("", "", "no database", "null"), "null", "Unknown", "null"))
            elif prefix == "goslin":
                entries.append((self.p("", "", "normalized lipid shorthand name"), "goslin",
                                goslin_version() or "Unknown", "https://github.com/lifs-tools/goslin"))
            else:
                entries.append((self.p("", "", d["database"]), prefix, "Unknown",
                                DATABASE_URIS.get(d["database"], "null")))
        if not entries:
            entries.append((self.p("", "", "no database", "null"), "null", "Unknown", "null"))
        return [line for i, (param, prefix, ver, uri) in enumerate(entries, 1)
                for line in (f"MTD\tdatabase[{i}]\t{param}", f"MTD\tdatabase[{i}]-prefix\t{prefix}",
                             f"MTD\tdatabase[{i}]-version\t{ver}", f"MTD\tdatabase[{i}]-uri\t{uri}")]

    def small_molecules(self) -> list[str]:
        m = self.m
        hdr = (["SMH", "SML_ID", "SMF_ID_REFS", "database_identifier", "chemical_formula", "smiles", "inchi",
                "chemical_name", "uri", "theoretical_neutral_mass", "adduct_ions", "reliability",
                "best_id_confidence_measure", "best_id_confidence_value"]
               + [f"abundance_assay[{i}]" for i in range(1, len(m.runs) + 1)])
        for k in range(1, len(m.svs) + 1):
            hdr += [f"abundance_study_variable[{k}]", f"abundance_variation_study_variable[{k}]"]
        rows = ["\t".join(hdr)]
        measure = self.confidence_measure()
        for n, (key, f) in enumerate(m.features.items(), 1):
            ident = m.identification(f)
            k_alt = ident["n"]

            def col(values):
                return "|".join(cell(v) for v in values) if len(values) == k_alt else "null"
            row = ["SML", str(n), "null", col(ident["ids"]),
                   col(ident["formula"]) if "null" not in ident["formula"] else "null",
                   "|".join(["null"] * k_alt), "|".join(["null"] * k_alt), col(ident["names"]),
                   col(ident["uris"]) if any(u != "null" for u in ident["uris"]) else "null",
                   col(ident["mass"]) if "null" not in ident["mass"] else "null",
                   "|".join(f["adducts"]) or "null", m.reliability(f), measure, "null"]
            row += [num((m.assay_values.get((key, i)) or {}).get("value")) for i in range(1, len(m.runs) + 1)]
            for k in range(1, len(m.svs) + 1):
                v = m.sv_values.get((key, k)) or {}
                declared = k in m.variation and (v.get("variation_type") or "") == m.variation[k]
                row += [num(v.get("value")), num(v.get("variation")) if declared else "null"]
            rows.append("\t".join(row))
        return rows

    def text(self) -> str:
        sml = self.small_molecules()      # first: registers warnings and CV labels of the SML section
        return "\n".join(self.metadata()) + "\n\n" + "\n".join(sml) + "\n"


def parse_versions(text: str) -> list[str]:
    versions = [v.strip() for v in text.split(",") if v.strip()]
    bad = [v for v in versions if v not in VERSIONS]
    if bad or not versions:
        raise InputError(f"--versions: {bad or text!r} not in {list(VERSIONS)}")
    return list(dict.fromkeys(versions))


def run(args) -> int:
    versions = parse_versions(args.versions)
    study = read_json(args.study_json, "msdeposit/study/1")
    plan = read_json(args.plan, "msdeposit/plan/1")
    metadata = load_metadata(args.metadata) if args.metadata else {}
    model = Model(study, plan, metadata, load_finalise(args.finalise), load_annotations(args.annotations))
    if not model.runs:
        # no file: C7 then fails for the selected versions and the gate blocks the study with this reason
        print("WARNING: mzTab-M not written: no run with known polarity (scan polarity is required)")
        return 0
    args.outdir.mkdir(parents=True, exist_ok=True)
    for v in versions:
        path = args.outdir / file_name(plan["prefix"], v)
        path.write_text(Writer(model, v).text(), encoding="utf-8")
        print(f"{path.name}: runs={len(model.runs)} study variables={len(model.svs)} SML={len(model.features)}")
    for w in dict.fromkeys(model.warnings):
        print(f"WARNING: {w}")
    return 0
