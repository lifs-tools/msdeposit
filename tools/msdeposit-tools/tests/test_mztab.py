import json
import os

import pytest

from helpers_results import describe_inputs, edit_study, parse_args, tools_dir
from msdeposit_tools import validate_mztab
from msdeposit_tools.describe import mztab
from msdeposit_tools.describe.lipids import FIELDS
from msdeposit_tools.io import InputError, write_table


def run_mztab(validated, tmp_path, versions="2.0.0-M,2.1.0-M", annotations=None, edit_plan=None, finalise=True):
    d = describe_inputs(validated, tmp_path)
    if edit_plan:
        p = json.loads(d["plan"].read_text())
        edit_plan(p)
        d["plan"].write_text(json.dumps(p))
    out = tmp_path / "mztab"
    argv = ["--study-json", d["study"], "--plan", d["plan"], "--metadata", d["metadata"], "--versions", versions,
            "--outdir", out]
    argv += ["--finalise", d["finalise"]] if finalise else []
    argv += ["--annotations", annotations] if annotations else []
    assert mztab.run(parse_args(mztab, argv)) == 0
    return d, out


def parse(path):
    mtd, rows, header = {}, [], None
    for line in path.read_text().splitlines():
        cells = line.split("\t")
        if cells[0] == "MTD":
            mtd[cells[1]] = cells[2]
        elif cells[0] == "SMH":
            header = cells
        elif cells[0] == "SML":
            rows.append(dict(zip(header, cells)))
    return mtd, rows


def test_files_runs_assays_and_study_variables(validated, tmp_path):
    d, out = run_mztab(validated, tmp_path)
    assert sorted(p.name for p in out.iterdir()) == ["S1.mztab-2.0.0-M.mzTab", "S1.mztab-2.1.0-M.mzTab"]
    plan = d["plan_data"]
    for version in mztab.VERSIONS:
        mtd, rows = parse(out / mztab.file_name("S1", version))
        assert mtd["mzTab-version"] == version and mtd["mzTab-ID"] == "S1"
        assert mtd["title"] == "Synthetic lipidomics test study" and mtd["contact[1]-name"] == "Ada Example"
        fin = {r["run"]: json.loads((d["finalise"] / f"{r['run']}.finalise.json").read_text())["sha1"]
               for r in plan["runs"]}
        for i, r in enumerate(plan["runs"], 1):
            assert mtd[f"ms_run[{i}]-location"] == f"file:///FILES/DERIVED_FILES/{r['deposited_mzml']}"
            assert mtd[f"ms_run[{i}]-hash"] == fin[r["run"]]
            assert mtd[f"ms_run[{i}]-id_format"] == "[MS, MS:1000768, Thermo nativeID format, ]"
            pol = "positive scan" if r["polarity"] == "positive" else "negative scan"
            assert pol in mtd[f"ms_run[{i}]-scan_polarity[1]"]
            assert mtd[f"assay[{i}]"] == r["ms_assay_name"] and mtd[f"assay[{i}]-ms_run_ref"] == f"ms_run[{i}]"
        # study variables reference the runs of their member samples: NIST = QC_01 (r001, r003), P1 = P1_A
        assert mtd["study_variable[1]"] == "NIST" and mtd["study_variable[1]-assay_refs"] == "assay[1]|assay[3]"
        assert mtd["study_variable[2]"] == "P1" and mtd["study_variable[2]-assay_refs"] == "assay[2]|assay[4]"
        assert mtd["study_variable[1]-average_function"] == "[MS, MS:1002883, median, ]"
        assert mtd["sample[1]"] == "QC_01" and "NCBITaxon:9606" in mtd["sample[1]-species[1]"]
        assert mtd["instrument[1]-name"] == "[MS, MS:1001911, Q Exactive, ]"
        assert mtd["ms_run[1]-instrument_ref"] == "instrument[1]"
        assert {r["chemical_name"] for r in rows} == {"PC 34:1", "PE 36:2"}
        f1 = rows[0]
        assert f1["abundance_assay[1]"] == "12.1" and f1["abundance_assay[2]"] == "null"
        assert f1["abundance_study_variable[1]"] == "12.5" and f1["adduct_ions"] == "[M+H]1+"
        assert f1["reliability"] == "Species"
        text = (out / mztab.file_name("S1", version)).read_text()
        assert "opt_" not in text and str(tmp_path) not in text
        assert f1["database_identifier"] == "null:PC 34:1"     # no database: mzTab-M "no database" entry


def test_variation_only_when_declared(validated, tmp_path):
    d, out = run_mztab(validated, tmp_path)
    mtd, rows = parse(out / "S1.mztab-2.0.0-M.mzTab")
    # NIST values declare cv_percent; P1 declares 'none'
    assert mtd["study_variable[1]-variation_function"] == "[MS, MS:1002963, variation coefficient, ]"
    assert "study_variable[2]-variation_function" not in mtd
    assert rows[0]["abundance_variation_study_variable[1]"] == "4.2"
    assert rows[1]["abundance_study_variable[2]"] == "3.3"
    assert rows[1]["abundance_variation_study_variable[2]"] == "null"
    assert "percent" in mtd["description"]


def test_undeclared_mixed_and_sd_variation(validated, tmp_path):
    def edit(s):
        s["values"] = [
            dict(feature="f1", run="", study_variable="NIST", value=1.0, unit="UO:0000064", variation=0.5,
                 variation_type=""),                                      # undeclared: never written
            dict(feature="f1", run="", study_variable="P1", value=2.0, unit="UO:0000064", variation=0.1,
                 variation_type="sd"),
            dict(feature="f2", run="", study_variable="P1", value=3.0, unit="UO:0000064", variation=0.2,
                 variation_type="sd")]
    edit_study(validated, edit)
    _, out = run_mztab(validated, tmp_path)
    mtd, rows = parse(out / "S1.mztab-2.1.0-M.mzTab")
    assert "study_variable[1]-variation_function" not in mtd
    assert rows[0]["abundance_study_variable[1]"] == "1" and rows[0]["abundance_variation_study_variable[1]"] == "null"
    assert mtd["study_variable[2]-variation_function"] == "[STATO, STATO:0000237, standard deviation, ]"
    assert [r["abundance_variation_study_variable[2]"] for r in rows] == ["0.1", "0.2"]
    assert "STATO" in mtd.values()

    def mixed(s):
        s["values"][2]["variation_type"] = "cv_percent"
    edit_study(validated, mixed)
    _, out = run_mztab(validated, tmp_path / "mixed")
    mtd, rows = parse(out / "S1.mztab-2.1.0-M.mzTab")
    assert "study_variable[2]-variation_function" not in mtd
    assert all(r["abundance_variation_study_variable[2]"] == "null" for r in rows)


def test_units_per_version(validated, tmp_path):
    _, out = run_mztab(validated, tmp_path)
    m20, _ = parse(out / "S1.mztab-2.0.0-M.mzTab")
    m21, _ = parse(out / "S1.mztab-2.1.0-M.mzTab")
    assert m20["small_molecule-quantification_unit"] == "[NCIT, NCIT:C48508, Micromole per Liter, ]"
    assert m20["small_molecule_feature-quantification_unit"] == m20["small_molecule-quantification_unit"]
    assert m20["id_confidence_measure[1]"] == "[MS, MS:1003311, Lipid empirical score, ]"
    assert m21["small_molecule-quantification_unit"] == "[UO, UO:0000064, micromolar, ]"
    assert "small_molecule_feature-quantification_unit" not in m21 and m21["mzTab-profile"] == "M+S"
    assert m20["quantification_method"] == "[MS, MS:1002514, absolute quantitation analysis, ]"


def test_several_units_rejected(validated, tmp_path):
    def edit(s):
        s["values"][0]["unit"] = "UO:0000065"
    edit_study(validated, edit)
    with pytest.raises(InputError, match="several units"):
        run_mztab(validated, tmp_path)


def test_swisslipids_ids_in_identifier_and_uri(validated, tmp_path):
    ann = tmp_path / "lipid_annotation.tsv"
    write_table(ann, [dict(name="PC 34:1", alternative="PC 34:1", goslin_name="PC 34:1", goslin_level="SPECIES",
                           formula="C42H82NO8P", neutral_mass="759.5778", swisslipids_name="PC(34:1)",
                           swisslipids_id="SLM:900000001", swisslipids_level="Species", note="")], FIELDS)
    _, out = run_mztab(validated, tmp_path, annotations=ann)
    mtd, rows = parse(out / "S1.mztab-2.0.0-M.mzTab")
    assert rows[0]["database_identifier"] == "SLM:900000001"
    assert rows[0]["uri"] == "https://www.swisslipids.org/#/entity/SLM:900000001"
    assert rows[0]["theoretical_neutral_mass"] == "759.5778"
    assert rows[1]["database_identifier"] == "null:PE 36:2" and rows[1]["chemical_name"] == "PE 36:2"
    dbs = {mtd[k]: mtd[k.replace("-prefix", "")] for k in mtd if k.endswith("-prefix")}
    assert set(dbs) == {"SLM", "null"} and "SwissLipids" in dbs["SLM"]
    assert mtd["software[2]"].startswith("[MS, MS:1003309, Goslin")


def test_no_study_variables_unknown_polarity_and_no_hash(validated, tmp_path):
    def edit(s):
        s["study_variables"], s["study_variable_members"] = [], []
        s["values"] = [v for v in s["values"] if v.get("run")]
        for f in s["features"]:
            f["level"] = "2"

    def unknown_polarity(p):
        p["runs"][3]["polarity"] = ""
    edit_study(validated, edit)
    _, out = run_mztab(validated, tmp_path, edit_plan=unknown_polarity, finalise=False)
    mtd, rows = parse(out / "S1.mztab-2.0.0-M.mzTab")
    assert mtd["study_variable[1]"] == "undefined"
    assert mtd["study_variable[1]-assay_refs"] == "assay[1]|assay[2]|assay[3]"
    assert "ms_run[4]-location" not in mtd and "ms_run[1]-hash" not in mtd
    assert [r["reliability"] for r in rows] == ["2", "2"]
    assert "MS:1002896" in mtd["small_molecule-identification_reliability"]       # MSI levels


def test_versions_option():
    assert mztab.parse_versions("2.1.0-M, 2.0.0-M,2.1.0-M") == ["2.1.0-M", "2.0.0-M"]
    with pytest.raises(InputError):
        mztab.parse_versions("2.2.0-M")


@pytest.mark.external
@pytest.mark.skipif(not (tools_dir() and (tools_dir() / validate_mztab.VALIDATOR_21).exists()),
                    reason="MSDEPOSIT_TOOLS_DIR with the jmzTab-m validators not set")
@pytest.mark.parametrize("variant", ["example", "sd_unannotated", "msi_levels"])
def test_external_validators_accept_both_versions(validated, tmp_path, variant):
    ann = None
    if variant == "sd_unannotated":
        def edit(s):
            s["values"].append(dict(feature="f2", run="", study_variable="NIST", value=1.0, unit="UO:0000064",
                                    variation=0.1, variation_type="sd"))
            s["values"] = [v for v in s["values"] if v["feature"] != "f1" or v.get("run")]
            s["features"].append(dict(assay="", feature="f3", name="unknown 404.39", level="", formula="",
                                      mz=404.39, rt=7.5, adduct="[M+NH4]1+", charge=None, database_id="",
                                      quantifier=""))
        edit_study(validated, edit)
    elif variant == "msi_levels":
        def edit(s):
            s["study_variables"], s["study_variable_members"] = [], []
            s["values"] = [dict(feature="f1", run="r001", study_variable="", value=1e6, unit="", variation=None,
                                variation_type="")]
            s["features"][0].update(level="2", database_id="HMDB0000564")
            s["features"][1].update(level="", adduct="M-H")
        edit_study(validated, edit)
    else:
        ann = tmp_path / "lipid_annotation.tsv"
        write_table(ann, [dict(name="PC 34:1", alternative="PC 34:1", goslin_name="PC 34:1", goslin_level="SPECIES",
                               formula="C42H82NO8P", neutral_mass="759.5778", swisslipids_name="PC(34:1)",
                               swisslipids_id="SLM:900000001", swisslipids_level="Species", note="")], FIELDS)
    _, out = run_mztab(validated, tmp_path, annotations=ann)
    for version in mztab.VERSIONS:
        res = validate_mztab.validate(out / mztab.file_name("S1", version), version)
        print(version, res["tool"], res["errors"], res["warnings"], res["messages"])
        assert res["available"]
        assert res["errors"] == 0, res["messages"]
        # offline (container without network) the 2.0 semantic CV lookup is reported as one warning
        other = [m for m in res["messages"] if m["level"] == "warning" and m["rule"] != "semantic_incomplete"]
        assert not other, res["messages"]
        assert os.fspath(tmp_path) not in json.dumps(res)
