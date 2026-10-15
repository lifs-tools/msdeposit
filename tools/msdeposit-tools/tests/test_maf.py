import csv

from helpers_results import describe_inputs, edit_study, parse_args
from msdeposit_tools.describe import maf
from msdeposit_tools.io import read_table, write_table
from synthetic import PLANTED


def run_maf(validated, tmp_path, transitions=None, annotations=None):
    d = describe_inputs(validated, tmp_path, transitions)
    out = tmp_path / "maf"
    argv = ["--study-json", d["study"], "--plan", d["plan"], "--metadata", d["metadata"], "--outdir", out]
    if annotations:
        argv += ["--annotations", annotations]
    assert maf.run(parse_args(maf, argv)) == 0
    return d["plan_data"], out


def read_maf(path):
    with open(path, newline="") as fh:
        text = fh.read()
    rows = list(csv.DictReader(text.splitlines(), delimiter="\t"))
    return text, rows


def test_maf_columns_values_and_todo(validated, tmp_path):
    plan, out = run_maf(validated, tmp_path)
    assert sorted(p.name for p in out.glob("m_*")) == sorted(a["maf"] for a in plan["assays"])
    pos = next(a for a in plan["assays"] if a["assay"] == "POS")
    names = {r["run"]: r["ms_assay_name"] for r in plan["runs"]}
    text, rows = read_maf(out / pos["maf"])
    header = text.splitlines()[0].split("\t")
    assert [h.strip('"') for h in header] == maf.MAF_COLUMNS + [names["r001"], names["r002"]]
    assert text.startswith('"database_identifier"')            # MetaboLights style: all cells quoted
    (f1,) = rows
    assert f1["metabolite_identification"] == "PC 34:1" and f1["mass_to_charge"] == "760.5851"
    assert f1["modifications"] == "[M+H]1+" and f1["charge"] == "1" and f1["chemical_formula"] == "C42H82NO8P"
    assert f1["species"] == "Homo sapiens" and f1["taxid"] == "NCBITaxon:9606"
    assert f1["retention_time"] == "" and f1[names["r001"]] == "12.1" and f1[names["r002"]] == ""
    neg = next(a for a in plan["assays"] if a["assay"] == "NEG")
    _, (f2,) = read_maf(out / neg["maf"])
    assert f2["retention_time"] == "5.1" and f2["charge"] == "-1"
    todo = {(t["feature"], t["assay"]): t for t in read_table(out / "maf_todo.tsv", "\t")}
    assert todo[("f1", "POS")]["missing"] == "retention_time;database_identifier"
    assert todo[("f1", "POS")]["level"] == "error"           # RT is required for LC-MS
    assert todo[("f2", "NEG")]["missing"] == "database_identifier" and todo[("f2", "NEG")]["level"] == "warning"
    assert str(tmp_path) not in (out / "maf_todo.tsv").read_text() + text


def test_features_without_assay_go_to_every_maf(validated, tmp_path):
    def add(s):
        s["features"].append(dict(assay="", feature="f3", name="cholesterol", level="", formula="C27H46O",
                                  mz=404.3887, rt=7.5, adduct="[M+NH4]1+", charge=None, database_id="CHEBI:16113",
                                  quantifier=""))
    edit_study(validated, add)
    plan, out = run_maf(validated, tmp_path)
    for a in plan["assays"]:
        _, rows = read_maf(out / a["maf"])
        chol = next(r for r in rows if r["metabolite_identification"] == "cholesterol")
        assert chol["database_identifier"] == "CHEBI:16113" and chol["database"] == "ChEBI"
        assert chol["uri"] == "http://purl.obolibrary.org/obo/CHEBI_16113" and chol["charge"] == "1"
    todo = read_table(out / "maf_todo.tsv", "\t")
    assert not [t for t in todo if t["feature"] == "f3"]


def test_srm_transitions_from_mzml_and_table(validated, tmp_path):
    def edit(s):
        for f in s["features"]:
            f["mz"] = None
        s["transitions"] = [dict(assay="NEG", transition="T-NEG-1", q1=740.5, q3=281.2, polarity="negative",
                                 feature="f2", collision_energy=30)]
    edit_study(validated, edit)
    srm = [{"id": "- SRM SIC Q1=760.6 Q3=184.1 sample=1 period=1 experiment=1 transition=0 name=PC 34:1 quan",
            "q1": 760.6, "q3": 184.1, "polarity": "positive", "ce": 35},
           {"id": f"- SRM SIC Q1=760.6 Q3=184.1 sample=1 period=1 experiment=1 transition=1 "
                  f"name={PLANTED['transition']}", "q1": 760.6, "q3": 184.1, "polarity": "positive", "ce": 35},
           {"id": "- SRM SIC Q1=786.6 Q3=184.1 sample=1 period=1 experiment=1 transition=2 name=PC 36:2",
            "q1": 786.6, "q3": 184.1, "polarity": "positive", "ce": 35}]
    plan, out = run_maf(validated, tmp_path, transitions={"r001": srm, "r002": srm})
    pos, neg = (next(a for a in plan["assays"] if a["assay"] == k) for k in ("POS", "NEG"))
    text, (f1,) = read_maf(out / pos["maf"])
    assert f1["mass_to_charge"] == "760.6" and f1["fragmentation"] == "184.1"
    _, (f2,) = read_maf(out / neg["maf"])
    assert f2["mass_to_charge"] == "740.5" and f2["fragmentation"] == "281.2"
    todo = {t["feature"]: t for t in read_table(out / "maf_todo.tsv", "\t")}
    assert todo["f1"]["mz_source"] == "transition Q1" and todo["f1"]["transitions"] == "1"
    assert "mass_to_charge" not in todo["f1"]["missing"]
    everything = text + (out / "maf_todo.tsv").read_text()
    assert PLANTED["transition"] not in everything and "quan" not in everything   # mzML ids are never written


def test_quantifier_wins_and_dialects(validated, tmp_path):
    def edit(s):
        s["features"][0].update(mz=None, quantifier="PC 34:1 Q1")
    edit_study(validated, edit)
    srm = [{"id": "name=PC(34:1)", "q1": 760.6, "q3": 86.1, "polarity": "positive", "ce": 35},
           {"id": "name=PC 34:1 Q1", "q1": 760.6, "q3": 184.1, "polarity": "positive", "ce": 35}]
    plan, out = run_maf(validated, tmp_path, transitions={"r001": srm})
    pos = next(a for a in plan["assays"] if a["assay"] == "POS")
    _, (f1,) = read_maf(out / pos["maf"])
    assert f1["fragmentation"] == "184.1"
    assert maf.name_keys("PC(16:0/18:1)") & maf.name_keys("PC 16:0/18:1")
    assert maf.name_keys("PC 34:1_quan2") & maf.name_keys("PC 34:1")
    assert not maf.name_keys("PC 34:1_d7") & maf.name_keys("PC 34:1")


def test_annotation_fallback(validated, tmp_path):
    from msdeposit_tools.describe.lipids import FIELDS
    ann = tmp_path / "lipid_annotation.tsv"
    write_table(ann, [dict(name="PC 34:1", alternative="PC 34:1", goslin_name="PC 34:1", goslin_level="SPECIES",
                           formula="C42H82NO8P", neutral_mass="759.5778", swisslipids_name="PC(34:1)",
                           swisslipids_id="SLM:900000001", swisslipids_level="Species", note="")], FIELDS)

    def edit(s):
        s["features"][0]["formula"] = None
    edit_study(validated, edit)
    plan, out = run_maf(validated, tmp_path, annotations=ann)
    pos = next(a for a in plan["assays"] if a["assay"] == "POS")
    _, (f1,) = read_maf(out / pos["maf"])
    assert f1["database_identifier"] == "SLM:900000001" and f1["database"] == "SwissLipids"
    assert f1["uri"] == "https://www.swisslipids.org/#/entity/SLM:900000001"
    assert f1["chemical_formula"] == "C42H82NO8P"


def test_helpers():
    assert maf.adduct_charge("[M+2H]2+") == 2 and maf.adduct_charge("[M-H]-") == -1 and maf.adduct_charge("") is None
    assert maf.database_info("HMDB0000122")["curie"] == "HMDB:HMDB0000122"
    assert maf.database_info("whatever") is None
    assert maf.chromatogram_label("- SRM SIC Q1=1 Q3=2 name=X y") == "X y" and maf.chromatogram_label("T1") == "T1"
