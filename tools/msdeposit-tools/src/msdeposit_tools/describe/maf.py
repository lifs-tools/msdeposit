"""maf: MetaboLights metabolite assignment files (MAF), one per plan assay, plus a to-do list for C9.

Rows are the features of the assay (features with an empty `assay` are listed in every assay). Filled from the
features sheet, with the lipid annotation (`--annotations`) as fallback for formula and database identifiers:
  - metabolite_identification = feature name; chemical_formula; database_identifier/database/uri (SwissLipids only by
    exact name match); modifications = adduct; charge (or derived from the adduct); retention_time
  - mass_to_charge: the feature m/z, else the precursor (Q1) of the SRM transitions assigned to the feature, with the
    product ions (Q3) in `fragmentation`. Transitions come from the `transitions` table (assigned by its `feature`
    column or by name) and from the named SRM chromatograms in the mzML metadata (assigned by name). A name matches
    the feature's `quantifier`, `name` or key, ignoring case, a trailing quantifier/qualifier tag and Goslin dialects.
  - one abundance column per run of the assay, named by the plan `ms_assay_name`, from the per-run `values`
Unknown values stay empty; MetaboLights requires m/z (and RT for chromatographic assays), which C9 reports from
maf_todo.tsv: `maf`, `assay`, `feature`, `name`, `missing` (;-separated MAF columns), `level` (error|warning),
`mz_source`, `transitions` (number assigned).

Transition ids from the mzML are never written (they may carry lab identifiers before finalisation).
"""

import csv
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from ..io import read_json, write_table
from ..io import load_metadata
from .lipids import alternatives, load_annotations, parser, regio, SWISSLIPIDS_ENTITY

MAF_COLUMNS = ["database_identifier", "chemical_formula", "smiles", "inchi", "metabolite_identification",
               "mass_to_charge", "fragmentation", "modifications", "charge", "retention_time", "taxid", "species",
               "database", "database_version", "reliability", "uri", "search_engine", "search_engine_score",
               "smallmolecule_abundance_sub", "smallmolecule_abundance_stdev_sub",
               "smallmolecule_abundance_std_error_sub"]
TODO_FIELDS = ["maf", "assay", "feature", "name", "missing", "level", "mz_source", "transitions"]
CHROMATOGRAPHIC = {"LC-MS", "GC-MS", "CE-MS", "SFC-MS"}
# identifier pattern -> (database, mzTab prefix, CURIE form, URI)
DATABASES = [
    (r"SLM:\d+", "SwissLipids", "SLM", "{id}", "https://www.swisslipids.org/#/entity/{id}"),
    (r"CHEBI:\d+", "ChEBI", "CHEBI", "{id}", "http://purl.obolibrary.org/obo/{u}"),
    (r"HMDB\d+", "HMDB", "HMDB", "HMDB:{id}", "https://hmdb.ca/metabolites/{id}"),
    (r"LM[A-Z]{2}\d{4,}", "LIPID MAPS", "LM", "LM:{id}", "https://www.lipidmaps.org/databases/lmsd/{id}"),
]
DATABASE_URIS = {"SwissLipids": "https://www.swisslipids.org", "ChEBI": "https://www.ebi.ac.uk/chebi",
                 "HMDB": "https://hmdb.ca", "LIPID MAPS": "https://www.lipidmaps.org"}


def add_arguments(p):
    p.add_argument("--study-json", required=True, type=Path)
    p.add_argument("--plan", required=True, type=Path)
    p.add_argument("--metadata", nargs="*", type=Path, default=[],
                   help="metadata.json files or directories (SRM transitions)")
    p.add_argument("--annotations", type=Path, help="lipid_annotation.tsv (fallback formula and identifiers)")
    p.add_argument("--outdir", type=Path, default=Path("."))


def fmt(v) -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, float):
        return format(v, ".10g")
    return str(v)


def database_info(identifier: str) -> dict | None:
    """Known identifier -> {database, prefix, curie, uri}; None for an unrecognised identifier."""
    ident = identifier.strip()
    for pattern, db, prefix, curie, uri in DATABASES:
        if re.fullmatch(pattern, ident):
            return dict(database=db, prefix=prefix, curie=curie.format(id=ident),
                        uri=uri.format(id=ident, u=ident.replace(":", "_")))
    return None


def organism(study: dict) -> tuple[str, str]:
    """(species, taxid) when all samples with an NCBI taxonomy organism share it, else ('', ''). Blanks and QC
    materials without a taxonomy term (e.g. 'blank' from the MetaboLights vocabulary) are ignored."""
    found = {(c.get("value", ""), c.get("accession", "")) for s in study.get("samples", [])
             for c in s.get("characteristics", []) if c.get("name", "").lower() == "organism" and c.get("value")
             and re.search(r"NCBITaxon", (c.get("source") or "") + (c.get("accession") or ""))}
    if len(found) != 1:
        return "", ""
    value, acc = next(iter(found))
    m = re.search(r"NCBITaxon[_:](\d+)$", acc or "")
    return value, f"NCBITaxon:{m[1]}" if m else ""


def adduct_charge(adduct: str) -> int | None:
    """'[M+H]1+' -> 1, '[M-H]-' -> -1, '[M+2H]2+' -> 2."""
    m = re.fullmatch(r"\[.*\](\d*)([+-])", (adduct or "").strip())
    return (int(m[1] or 1)) * (1 if m[2] == "+" else -1) if m else None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower()


@lru_cache(maxsize=None)
def name_keys(name: str) -> frozenset:
    """Comparable forms of a feature or transition name: as given, without a trailing quantifier/qualifier tag,
    and Goslin-normalised (e.g. 'PC(16:0/18:1)' == 'PC 16:0/18:1')."""
    if not name:
        return frozenset()
    forms = {name.strip(), re.sub(r"[ _-]+(quan|quant|quantifier|qual|qualifier)\d*$", "", name.strip(), flags=re.I)}
    forms |= {alt for f in list(forms) for alt in alternatives(f)}
    for f in list(forms):
        try:
            forms.add(parser().parse(regio(f)[0]).get_lipid_string())
        except Exception:
            pass
    return frozenset(_norm(f) for f in forms if f)


def chromatogram_label(cid: str) -> str:
    """Transition name of an SRM chromatogram id (pwiz: '... name=<label>'), else the id itself."""
    m = re.search(r"\bname=(.*)$", cid or "")
    return (m[1] if m else cid or "").strip()


def assay_transitions(study: dict, assay: str, runs: list[str], metadata: dict) -> list[dict]:
    """Transitions of one assay: [{label, feature, q1, q3, source}] (deduplicated)."""
    out, seen = [], set()
    for t in study.get("transitions", []):
        if t.get("assay") == assay:
            key = ("table", t.get("transition"), t.get("q1"), t.get("q3"))
            if key not in seen:
                seen.add(key)
                out.append(dict(label=t.get("transition") or "", feature=t.get("feature") or "", q1=t.get("q1"),
                                q3=t.get("q3"), source="transitions table"))
    for r in runs:
        for t in (metadata.get(r) or {}).get("transitions") or []:
            label = chromatogram_label(t.get("id", ""))
            key = ("mzml", label, t.get("q1"), t.get("q3"))
            if key not in seen and t.get("q1") is not None:
                seen.add(key)
                out.append(dict(label=label, feature="", q1=t.get("q1"), q3=t.get("q3"), source="mzML"))
    return out


def feature_transitions(feature: dict, transitions: list[dict]) -> list[dict]:
    """Transitions assigned to a feature; transitions named after its quantifier win over name matches."""
    explicit = [t for t in transitions if t["feature"] and t["feature"] == feature.get("feature")]
    if explicit:
        return explicit
    unassigned = [t for t in transitions if not t["feature"]]
    quant = name_keys(feature.get("quantifier") or "")
    hits = [t for t in unassigned if quant & name_keys(t["label"])]
    if hits:
        return hits
    keys = name_keys(feature.get("name") or "") | name_keys(feature.get("feature") or "")
    return [t for t in unassigned if keys & name_keys(t["label"])]


def feature_ids(feature: dict, ann: list[dict]) -> tuple[list[dict], str]:
    """Database identifiers of a feature: the features sheet wins, else SwissLipids ids of the annotation.
    Returns ([{database, prefix, curie, uri, id}], source)."""
    if feature.get("database_id"):
        ids = [x.strip() for x in str(feature["database_id"]).split("|") if x.strip()]
        return [dict(database_info(i) or dict(database="", prefix="", curie="", uri=""), id=i) for i in ids], "features"
    sl = [a["swisslipids_id"] for a in ann if a.get("swisslipids_id")]
    return [dict(database_info(i) or dict(database="SwissLipids", prefix="SLM", curie=i,
                                          uri=SWISSLIPIDS_ENTITY.format(i)), id=i) for i in sl], "annotation"


def maf_rows(study: dict, plan: dict, assay: dict, metadata: dict, annotations: dict) -> tuple[list[dict], list, list]:
    """(rows, columns, todo) of one assay's MAF."""
    runs = [r for r in plan["runs"] if r["assay"] == assay["assay"]]
    run_cols = {r["run"]: r["ms_assay_name"] for r in runs}
    values = {(v["feature"], v["run"]): v.get("value") for v in study.get("values", []) if v.get("run")}
    transitions = assay_transitions(study, assay["assay"], [r["run"] for r in runs], metadata)
    species, taxid = organism(study)
    needs_rt = assay.get("technique", "") in CHROMATOGRAPHIC
    rows, todo = [], []
    for f in study.get("features", []):
        if f.get("assay") not in (None, "", assay["assay"]):
            continue
        name = f.get("name") or ""
        ann = annotations.get(name, [])
        hits = feature_transitions(f, transitions)
        ids, _ = feature_ids(f, ann)
        formula = f.get("formula") or "|".join(a["formula"] for a in ann if a.get("formula"))
        charge = f.get("charge")
        if charge in (None, ""):
            charge = adduct_charge(f.get("adduct") or "")
        mz = fmt(f.get("mz"))
        mz_source = "features" if mz else ""
        if not mz and hits:
            mz = "|".join(dict.fromkeys(fmt(t["q1"]) for t in hits))
            mz_source = "transition Q1"
        row = dict.fromkeys(MAF_COLUMNS, "")
        row.update(database_identifier="|".join(i["id"] for i in ids), chemical_formula=formula,
                   metabolite_identification=name, mass_to_charge=mz,
                   fragmentation="|".join(dict.fromkeys(fmt(t["q3"]) for t in hits if t.get("q3") is not None)),
                   modifications=f.get("adduct") or "", charge=fmt(charge), retention_time=fmt(f.get("rt")),
                   taxid=taxid, species=species,
                   database="|".join(dict.fromkeys(i["database"] for i in ids if i["database"])),
                   uri="|".join(i["uri"] for i in ids if i["uri"]))
        for run, col in run_cols.items():
            row[col] = fmt(values.get((f.get("feature"), run)))
        rows.append(row)
        missing = [c for c in ("mass_to_charge", "retention_time", "database_identifier", "chemical_formula")
                   if not row[c] and (c != "retention_time" or needs_rt)]
        if missing:
            level = "error" if {"mass_to_charge", "retention_time"} & set(missing) else "warning"
            todo.append(dict(maf=assay["maf"], assay=assay["assay"], feature=f.get("feature", ""), name=name,
                             missing=";".join(missing), level=level, mz_source=mz_source, transitions=len(hits)))
    return rows, MAF_COLUMNS + list(run_cols.values()), todo


def write_maf(path: Path, rows: list[dict], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, delimiter="\t", quoting=csv.QUOTE_ALL, lineterminator="\n",
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def run(args) -> int:
    study = read_json(args.study_json, "msdeposit/study/1")
    plan = read_json(args.plan, "msdeposit/plan/1")
    metadata = load_metadata(args.metadata) if args.metadata else {}
    annotations = load_annotations(args.annotations)
    todo, summary = [], defaultdict(int)
    for assay in plan["assays"]:
        rows, columns, t = maf_rows(study, plan, assay, metadata, annotations)
        write_maf(args.outdir / assay["maf"], rows, columns)
        todo += t
        summary["features"] += len(rows)
        summary["with_mz"] += sum(1 for r in rows if r["mass_to_charge"])
        print(f"{assay['maf']}: {len(rows)} features, m/z {sum(1 for r in rows if r['mass_to_charge'])}, "
              f"RT {sum(1 for r in rows if r['retention_time'])}")
    write_table(args.outdir / "maf_todo.tsv", todo, TODO_FIELDS)
    print(f"{len(plan['assays'])} MAF files, {summary['features']} rows, {len(todo)} to-do entries")
    return 0
