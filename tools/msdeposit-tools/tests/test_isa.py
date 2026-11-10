import json

import pytest
from helpers_isa import (PROTOCOL_TEXTS, column, isa_args, make_plan, read_isa_table, rich_metadata, write_isa,
                         write_protocols)
from synthetic import PLANTED

from msdeposit_tools import cli
from msdeposit_tools.describe import isa
from msdeposit_tools.io import InputError, write_json


def investigation(out):
    from metabolights_utils.isatab import Reader
    result = Reader.get_investigation_file_reader().read(str(out / "i_Investigation.txt"))
    assert not [m for m in result.parser_report.messages if str(m.type).upper().endswith(("ERROR", "CRITICAL"))]
    return result.investigation


def all_text(out) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(out.iterdir()))


def test_isa_files_structure_and_values(validated, tmp_path):
    out, plan = write_isa(validated, tmp_path)
    assert sorted(p.name for p in out.iterdir()) == ["a_S1_NEG.txt", "a_S1_POS.txt", "i_Investigation.txt",
                                                     "s_S1.txt"]
    tmpl = isa.templates()
    for pa in plan["assays"]:
        path = out / pa["file"]
        hdr, rows = read_isa_table(path)
        isa.check_structure(hdr, tmpl["assay"]["LC-MS"], pa["file"])
        runs = [r for r in plan["runs"] if r["assay"] == pa["assay"]]
        assert column(path, "MS Assay Name") == [r["ms_assay_name"] for r in runs]      # plan run order
        assert column(path, "Derived Spectral Data File") == [r["derived_path"] for r in runs]
        assert column(path, "Raw Spectral Data File") == [r["raw_path"] for r in runs]
        assert column(path, "Comment[Raw data file identifier]") == [r["raw_id"] for r in runs]
        assert set(column(path, "Metabolite Assignment File")) == {pa["maf"]}
        assert set(column(path, "Parameter Value[Scan polarity]")) == {pa["polarity"]}
        assert set(column(path, "Parameter Value[Column type]")) == {"reverse phase"}
        assert set(column(path, "Parameter Value[Column model]")) == {"C18 100 x 2.1 mm"}
        assert set(column(path, "Parameter Value[Instrument]")) == {"Q Exactive"}
        assert set(column(path, "Term Accession Number", 2)) == {"http://purl.obolibrary.org/obo/MS_1001911"}
        assert set(column(path, "Parameter Value[Ion source]")) == {"electrospray ionization"}
        assert set(column(path, "Parameter Value[Mass analyzer]")) == {"orbitrap"}
        assert set(column(path, "Parameter Value[Scan m/z range]")) == {"100-1200"}
        assert set(column(path, "Comment[Acquisition start time]")) == {"2024-03-05T09:41:17Z"}
        assert set(column(path, "Comment[Acquisition software]")) == {"Xcalibur 4.1.31.9"}
        assert column(path, "Protocol REF", 1) == ["Chromatography"] * len(runs)
    inv = investigation(out)
    study = inv.studies[0]
    assert study.file_name == "s_S1.txt" and study.title == "Synthetic lipidomics test study"
    assert [a.file_name for a in study.study_assays.assays] == ["a_S1_POS.txt", "a_S1_NEG.txt"]
    assert study.study_assays.assays[0].measurement_type.term == "targeted analysis"
    assert [p.name for p in study.study_protocols.protocols] == [
        "Sample collection", "Extraction", "Chromatography", "Mass spectrometry", "Data transformation",
        "Metabolite identification"]
    assert [c.last_name for c in study.study_contacts.people] == ["Example"]     # contact row with empty study
    assert [f.name for f in study.study_factors.factors] == ["Sample material"]
    text = (out / "i_Investigation.txt").read_text()
    for comment in ('Comment[License]\t"CC BY 4.0"', 'Comment[Study Category]\t"ms-mhd-enabled"',
                    'Comment[Template Version]\t"1.0"', 'Comment[Sample Template]\t"mhd-enabled"',
                    'Comment[Created At]\t"2026-01-01"'):
        assert comment in text
    sources = {r.source_name for r in inv.ontology_source_references.references}
    assert {"MS", "NCBITaxon", "OBI", "EFO", "CHMO"} <= sources and "BTO" not in sources   # only used sources
    assert "[[TODO-" not in text


def test_sample_table(validated, tmp_path):
    def edit(s):
        s["samples"][0]["characteristics"].append({"name": "Age", "value": "52", "source": "", "accession": "",
                                                   "unit": "year"})
        s["samples"][0]["characteristics"].append({"name": "organism part", "value": "", "source": "",
                                                   "accession": "", "unit": ""})
        s["samples"][1]["factors"][0]["unit"] = ""
    out, plan = write_isa(validated, tmp_path, edit_study=edit)
    hdr, rows = read_isa_table(out / "s_S1.txt")
    assert hdr[:19] == ["Source Name", "Characteristics[Organism]", "Term Source REF", "Term Accession Number",
                        "Characteristics[Organism part]", "Term Source REF", "Term Accession Number",
                        "Characteristics[Variant]", "Term Source REF", "Term Accession Number",
                        "Characteristics[Sample type]", "Term Source REF", "Term Accession Number",
                        "Characteristics[Disease]", "Term Source REF", "Term Accession Number",
                        "Characteristics[Cell type]", "Term Source REF", "Term Accession Number"]
    assert hdr[19:25] == ["Characteristics[Age]", "Unit", "Term Source REF", "Term Accession Number",
                          "Protocol REF", "Sample Name"]
    assert hdr[25:] == ["Factor Value[Sample material]", "Term Source REF", "Term Accession Number",
                        "Comment[Sequence position]"]
    assert [r[0] for r in rows] == ["NIST SRM 1950", "Plasma 1"]
    assert rows[0][1:4] == ["Homo sapiens", "NCBITaxon", "http://purl.obolibrary.org/obo/NCBITaxon_9606"]
    assert rows[0][19:21] == ["52", "year"] and rows[1][19:21] == ["", ""]
    assert rows[0][4] == "blood plasma"                  # duplicate empty entry does not override the value
    assert all(r[7:19] == [""] * 12 for r in rows)       # required template characteristics stay empty, not invented
    assert column(out / "s_S1.txt", "Protocol REF") == ["Sample collection"] * 2
    isa.check_structure(hdr, isa.templates()["sample"]["mhd-enabled"], "s_S1.txt", {"Characteristics[Age]"})


def test_minimum_sample_template_for_other_category(validated, tmp_path):
    out, _ = write_isa(validated, tmp_path, study_category="other")
    hdr, _ = read_isa_table(out / "s_S1.txt")
    assert "Characteristics[Disease]" not in hdr and "Characteristics[Sample type]" in hdr
    assert 'Comment[Sample Template]\t"minimum"' in (out / "i_Investigation.txt").read_text()


def sensitive(s):
    s["mode"] = "sensitive"


def test_sensitive_plan_has_no_raw_files(validated, tmp_path):
    def raw_path_anyway(plan):       # a raw path in a sensitive plan must never reach the assay table
        plan["runs"][0]["raw_path"] = "FILES/RAW_FILES/x.raw"
    out, plan = write_isa(validated, tmp_path, naming="neutral", deposit_raw=False, edit_study=sensitive,
                          edit_plan=raw_path_anyway)
    for pa in plan["assays"]:
        assert set(column(out / pa["file"], "Raw Spectral Data File")) == {""}
        ids = column(out / pa["file"], "Comment[Raw data file identifier]")
        assert ids == [r["raw_id"] for r in plan["runs"] if r["assay"] == pa["assay"]] and all(ids)
    assert column(out / "a_S1_POS.txt", "MS Assay Name") == ["QC_01_POS", "P1_A_POS"]
    text = (out / "i_Investigation.txt").read_text()
    assert "anonymised" in text and "Only mzML files are deposited" in text


def test_no_paths_serials_or_private_metadata(validated, tmp_path):
    out, _ = write_isa(validated, tmp_path, naming="neutral", deposit_raw=False, edit_study=sensitive,
                       timestamp_policy="date")
    text = all_text(out)
    for token in PLANTED.values():
        assert token not in text, token
    for token in ("file://", "Users", str(tmp_path), "SN1", ".raw", "IC1"):
        assert token not in text, token
    assert "2024-03-05" in text and "09:41:17" not in text


def test_protocol_placeholders_and_drafts(validated, tmp_path):
    out, _ = write_isa(validated, tmp_path, protocols=False)
    protocols = {p.name: p.description for p in investigation(out).studies[0].study_protocols.protocols}
    for name, key in (("Sample collection", "sample_collection"), ("Extraction", "extraction"),
                      ("Chromatography", "chromatography"), ("Mass spectrometry", "mass_spectrometry"),
                      ("Metabolite identification", "metabolite_identification")):
        assert f"[[TODO-PROTOCOL:{key}]]" in protocols[name]
    assert "TODO" not in protocols["Data transformation"]          # drafted text is complete without a block
    ms = protocols["Mass spectrometry"]
    assert ms.startswith(isa.DRAFT) and "Q Exactive" in ms and "electrospray ionization" in ms
    assert "positive" in ms and "negative" in ms and "100-1200" in ms and "acquired 2024-03-05" in ms
    assert protocols["Chromatography"].startswith(isa.DRAFT) and "C18 100 x 2.1 mm" in protocols["Chromatography"]
    assert "converted" not in protocols["Data transformation"]     # format=mzml runs were not converted
    assert "identifiers were set to the deposited file names" in protocols["Data transformation"]


def test_protocol_texts_study_specific_and_assay_keys(validated, tmp_path):
    pdir = write_protocols(tmp_path / "protocols")
    write_protocols(pdir / "S1", {"extraction": "Study specific extraction text, long enough for MetaboLights."})
    (pdir / "chromatography_hilic.md").write_text("HILIC separation text\nfor the negative assay.\n")
    (pdir / "ms_neg.md").write_text("Negative mode source settings.\n")

    def keys(s):
        s["assays"][1]["protocol_keys"] = ["extraction", "chromatography_hilic", "Mass spectrometry=ms_neg"]
    out, _ = write_isa(validated, tmp_path, edit_study=keys, protocols=pdir)
    protocols = {p.name: p.description for p in investigation(out).studies[0].study_protocols.protocols}
    assert protocols["Extraction"] == "Study specific extraction text, long enough for MetaboLights."
    chrom = protocols["Chromatography"]
    assert PROTOCOL_TEXTS["chromatography"] in chrom and "NEG: HILIC separation text for the negative assay." in chrom
    assert "NEG: Negative mode source settings." in protocols["Mass spectrometry"]


def test_unknown_protocol_key_is_an_input_error(validated, tmp_path):
    def keys(s):
        s["assays"][0]["protocol_keys"] = ["sample_prep_x"]
    with pytest.raises(InputError, match="sample_prep_x"):
        write_isa(validated, tmp_path, edit_study=keys)


def test_unknown_protocol_name_in_config(validated, tmp_path):
    def cfg(s):
        s["config"]["protocols"] = {"Lipid extraction": "x"}
    with pytest.raises(InputError, match="Lipid extraction"):
        write_isa(validated, tmp_path, edit_study=cfg)


def test_timestamp_drop(validated, tmp_path):
    out, _ = write_isa(validated, tmp_path, timestamp_policy="drop")
    hdr, _ = read_isa_table(out / "a_S1_POS.txt")
    assert "Comment[Acquisition start time]" not in hdr
    assert "2024-03-05" not in all_text(out)


def test_mhd_profile_values(validated, tmp_path):
    out, _ = write_isa(validated, tmp_path, controlled_value_profile="mhd")
    path = out / "a_S1_POS.txt"
    assert set(column(path, "Parameter Value[Scan polarity]")) == {"positive polarity acquisition"}
    assert set(column(path, "Parameter Value[Column type]")) == {"reversed phase chromatography"}
    assert set(column(path, "Parameter Value[Mass analyzer]")) == {"quadrupole orbitrap instrument"}
    hdr, rows = read_isa_table(path)
    i = hdr.index("Parameter Value[Mass analyzer]")
    assert rows[0][i + 1:i + 3] == ["MS", "http://purl.obolibrary.org/obo/MS_1003769"]


def test_instrument_fallback_and_inferred_analyzer(validated, tmp_path):
    metas = [rich_metadata(r, p, srm=3) for r, p in (("r001", "positive"), ("r002", "positive"),
                                                       ("r003", "negative"), ("r004", "negative"))]
    for m in metas:
        m["instrument"].update(model="instrument model", model_accession="MS:1000031")
        m["instrument"]["configurations"][0]["components"] = [
            c for c in m["instrument"]["configurations"][0]["components"] if c["type"] != "analyzer"]

    def fallback(s):
        for a in s["assays"]:
            a["instrument_fallback"] = "Example triple quadrupole"
    out, _ = write_isa(validated, tmp_path, metas=metas, edit_study=fallback)
    path = out / "a_S1_POS.txt"
    assert set(column(path, "Parameter Value[Instrument]")) == {"Example triple quadrupole"}
    assert set(column(path, "Parameter Value[Mass analyzer]")) == {"quadrupole"}
    assert set(column(path, "Comment[Inferred values]")) == {
        "instrument from the assay sheet; mass analyzer from SRM chromatograms"}


def test_unknown_technique(validated, tmp_path):
    def sfc(s):
        s["assays"][0]["technique"] = "SFC-MS"
    with pytest.raises(InputError, match="SFC-MS"):
        write_isa(validated, tmp_path, edit_study=sfc)


def test_gc_ms_template(validated, tmp_path):
    def gc(s):
        for a in s["assays"]:
            a.update(technique="GC-MS", column_type="non-polar", column_model="DB-5 30 m x 0.25 mm")
    out, plan = write_isa(validated, tmp_path, edit_study=gc)
    hdr, _ = read_isa_table(out / "a_S1_POS.txt")
    isa.check_structure(hdr, isa.templates()["assay"]["GC-MS"], "a_S1_POS.txt")
    assert set(column(out / "a_S1_POS.txt", "Parameter Value[Column type]")) == {"low polarity"}
    assert investigation(out).studies[0].study_assays.assays[0].assay_type.term == \
        "gas chromatography mass spectrometry assay"


def test_finalise_inputs(validated, tmp_path):
    study_path, plan_path, mdir = make_plan(validated, tmp_path, "neutral", False, sensitive)
    plan = json.loads(plan_path.read_text())
    fdir = tmp_path / "final"
    for r in plan["runs"]:
        write_json(fdir / r["run"] / "finalise.json", {"schema": "msdeposit/finalise/1", "run": r["run"],
                                                      "deposited_mzml": r["deposited_mzml"], "mode": "anonymise"})
    out = tmp_path / "isa"
    assert isa.run(isa_args(study_path, plan_path, mdir, out, finalise=fdir, timestamp_policy="drop")) == 0
    text = (out / "i_Investigation.txt").read_text()
    assert "anonymised" in text and "Acquisition timestamps were removed" in text
    write_json(fdir / "r001" / "finalise.json", {"schema": "msdeposit/finalise/1", "run": "r001",
                                                 "deposited_mzml": "other.mzML", "mode": "anonymise"})
    with pytest.raises(InputError, match="r001"):
        isa.run(isa_args(study_path, plan_path, mdir, out, finalise=fdir))


def test_cli_entry(validated, tmp_path):
    study_path, plan_path, mdir = make_plan(validated, tmp_path)
    out = tmp_path / "cli"
    rc = cli.main(["isa", "--study-json", str(study_path), "--plan", str(plan_path), "--metadata", str(mdir),
                   "--outdir", str(out)])
    assert rc == 0 and (out / "i_Investigation.txt").exists()


def test_check_structure_rejects_wrong_columns():
    tmpl = isa.templates()["assay"]["LC-MS"]
    header = [h for h, s, *_ in tmpl for h in ([h, "Term Source REF", "Term Accession Number"]
                                                 if s == "ONTOLOGY_COLUMN" else [h])]
    isa.check_structure(header + ["Comment[x]"], tmpl, "ok")
    with pytest.raises(ValueError):
        isa.check_structure([h for h in header if h != "Parameter Value[Column type]"], tmpl, "missing")
    with pytest.raises(ValueError):
        isa.check_structure(header[:7] + header[9:], tmpl, "single instead of ontology")


@pytest.mark.parametrize("src,acc,expected", [
    ("NCBITaxon", "NCBITaxon:9606", ("NCBITaxon", "http://purl.obolibrary.org/obo/NCBITaxon_9606")),
    ("", "BTO:0000131", ("BTO", "http://purl.obolibrary.org/obo/BTO_0000131")),
    ("EFO", "EFO_0001795", ("EFO", "http://www.ebi.ac.uk/efo/EFO_0001795")),
    ("EDAM", "EDAM:topic_0153", ("EDAM", "http://edamontology.org/topic_0153")),
    ("MS", "http://purl.obolibrary.org/obo/MS_1000073", ("MS", "http://purl.obolibrary.org/obo/MS_1000073")),
    ("", "", ("", "")),
])
def test_term_url(src, acc, expected):
    assert isa.term_url(src, acc) == expected


@pytest.mark.parametrize("analyzers,srm,im,expected", [
    (["quadrupole", "orbitrap"], False, False, "quadrupole orbitrap instrument"),
    (["quadrupole", "orbitrap", "quadrupole", "radial ejection linear ion trap"], False, False,
     "quadrupole ion trap orbitrap instrument"),
    (["orbitrap"], False, False, "orbitrap instrument"),
    (["quadrupole", "time-of-flight"], False, False, "quadrupole time-of-flight instrument"),
    (["quadrupole", "time-of-flight"], False, True, "quadrupole ion mobility time-of-flight instrument"),
    (["quadrupole"], True, False, "triple quadrupole instrument"),
    (["quadrupole"], False, False, "quadrupole instrument"),
    (["radial ejection linear ion trap"], False, False, "ion trap instrument"),
    ([], False, False, ""),
])
def test_instrument_class(analyzers, srm, im, expected):
    assert isa.instrument_class(analyzers, srm, im)[0] == expected


def test_srm_only_runs_use_the_quadrupole(validated, tmp_path):
    """QTRAP-like instruments list a linear ion trap last, but SRM acquisition uses the quadrupole mass filters."""
    metas = [rich_metadata(r, p, srm=3) for r, p in (("r001", "positive"), ("r002", "positive"),
                                                       ("r003", "negative"), ("r004", "negative"))]
    for m in metas:
        m["counts"] = {k: 0 for k in m["counts"]}
        comps = m["instrument"]["configurations"][0]["components"]
        for c in comps:
            if c["type"] == "analyzer":
                c["terms"] = [["MS:1000291", "linear ion trap"]]
    out, _ = write_isa(validated, tmp_path, metas=metas)
    assert set(column(out / "a_S1_POS.txt", "Parameter Value[Mass analyzer]")) == {"quadrupole"}
