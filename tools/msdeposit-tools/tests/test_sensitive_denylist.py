import argparse
import builtins
import csv

import helpers_sensitive as hs
import pytest

from msdeposit_tools import cli
from msdeposit_tools.sensitive import denylist as dl
from msdeposit_tools.sensitive import vendor_metadata as vm


def read(path):
    with open(path, newline="") as fh:
        return {r["token"]: r for r in csv.DictReader(fh, delimiter="\t")}


def build(tmp_path, *extra):
    out = tmp_path / "denylist.tsv"
    assert cli.main(["identifier-denylist", "--study", "S1", "--out", str(out), *map(str, extra)]) == 0
    return read(out)


def test_user_tokens_are_never_filtered(tmp_path):
    (tmp_path / "user.txt").write_text("# lab names\nPlasma\nAB\nMüller\nHaldenbrook Lipid Lab\n")
    (tmp_path / "allow.txt").write_text("Plasma\n")
    d = build(tmp_path, "--user-denylist", tmp_path / "user.txt", "--allowlist", tmp_path / "allow.txt",
              "--frequency-filter", "zipf=1")
    assert {"Plasma", "AB", "Müller", "Haldenbrook Lipid Lab"} <= d.keys()
    assert d["Plasma"]["kind"] == "user" and d["AB"]["case_sensitive"] == "true"
    assert d["Mueller"]["kind"] == d["Muller"]["kind"] == "variant"


def test_user_tsv_with_case_column(tmp_path):
    (tmp_path / "user.tsv").write_text("token\tcase_sensitive\tnote\nlab7\ttrue\tbench code\nXY\tfalse\t\n")
    d = build(tmp_path, "--user-denylist", tmp_path / "user.tsv")
    assert d["lab7"]["case_sensitive"] == "true" and d["XY"]["case_sensitive"] == "false"


def test_candidates_filters_case_and_variants(tmp_path):
    c = tmp_path / "vm" / "S1_RAW_001"
    c.mkdir(parents=True)
    rows = ["Quentin Marbleford\tperson", "Marbleford\tperson", "QMF\tperson", "qmf\tsequence", "Strauß\tperson",
            "Orbitrap\tinstrument_name", "Q Exactive\tinstrument_name", "spectrumList\tpath", "Xcalibur\tpath",
            "Users\tpath", "Program Files\tpath", "minute\tpath", "2024-03-05\tsequence", "3.5kV\tsequence",
            "PC\tperson", "LPC\tsequence", "data1\tpath", "Lipids\tpath", "Ab12\tserial", "MassLynx\tpath",
            "0123456789abcdef0123\tserial", "Batch7\tpath", "Mueller\tperson",
            "1838271.10234\tserial", "20240305\tsequence", "2024-03-05T10:11:12\tserial"]
    (c / "candidates.tsv").write_text("token\tfield\tmember\n" + "".join(f"{r}\tx\n" for r in rows))
    (tmp_path / "allow.txt").write_text("Marbleford\n")
    d = build(tmp_path, "--candidates", tmp_path / "vm", "--allowlist", tmp_path / "allow.txt")
    assert {"Quentin Marbleford", "QMF", "qmf", "Strauß", "Strauss", "Ab12", "Mueller", "Müller",
            "1838271.10234"} == set(d)
    assert d["QMF"]["case_sensitive"] == "true" and d["Ab12"]["case_sensitive"] == "true"
    assert d["qmf"]["case_sensitive"] == "false" and d["Quentin Marbleford"]["case_sensitive"] == "false"
    assert d["QMF"]["kind"] == "candidate" and d["Strauss"]["kind"] == "variant"
    assert d["QMF"]["origin"] == "vendor:S1_RAW_001:person"


def test_run_sheet_original_names_not_keys(tmp_path, validated):
    d = build(tmp_path, "--run-keys", validated / "runs.normalised.csv")
    assert d["HLB_qmf_plasma_007_01"]["kind"] == "original_name"
    assert "HLB" in d and d["HLB"]["case_sensitive"] == "true" and "qmf" in d
    assert not {"r001", "POS", "NEG", "QC_01", "S1"} & set(d)              # keys are checked by S7, not listed
    assert "data" not in d and "mzML" not in d


def test_sample_words_are_not_added(tmp_path):
    rows = [dict(study="S1", run="r1", assay="POS", sample="Cohort", path="/x/Cohort_HLB_9.raw", companions="")]
    with open(tmp_path / "runs.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    d = build(tmp_path, "--run-keys", tmp_path / "runs.csv")
    assert "Cohort_HLB_9" in d and "HLB" in d and "Cohort" not in d


def test_metadata_serials_and_source_files(tmp_path):
    from conftest import fake_metadata
    from msdeposit_tools.io import write_json
    m = fake_metadata("r001")
    m["instrument"]["serial"] = "Exactive Series slot #1234"
    m["source_files"][0].update(name="HLB_qmf_7.raw", location=r"file:///D:/Users/qmarbleford/x")
    write_json(tmp_path / "meta" / "r001.json", m)
    d = build(tmp_path, "--metadata", tmp_path / "meta")
    assert "Exactive Series slot #1234" in d and "HLB_qmf_7" in d and "qmarbleford" in d


def test_frequency_filter_degrades_without_wordfreq(tmp_path, monkeypatch, capsys):
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "wordfreq":
            raise ImportError
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    b = dl.Builder(set(), 3.3)
    b.add_harvested("Haldenbrook", "candidate", "x")
    assert b.result() and b.warnings


def test_frequency_filter_with_wordfreq(monkeypatch):
    b = dl.Builder(set(), 3.0)
    b.freq = lambda w, lang: 5.0 if w in ("rook", "river") else 1.0
    b.add_harvested("River", "candidate", "x")
    b.add_harvested("Rook", "candidate", "x", keep_common=True)
    b.add_harvested("Haldenbrook", "candidate", "x")
    got = {e.token: e for e in b.result()}
    assert "River" not in got and got["Rook"].case_sensitive and "Haldenbrook" in got
    with pytest.raises(Exception):
        dl.parse_frequency_filter("zipf")
    assert dl.parse_frequency_filter("off") is None and dl.parse_frequency_filter("zipf=3.3") == 3.3


def test_end_to_end_from_vendor_metadata(tmp_path):
    vm.extract(hs.thermo_raw(tmp_path / "x.raw"), "thermo", tmp_path / "vm" / "R1")
    d = build(tmp_path, "--candidates", tmp_path / "vm")
    assert {"qmarbleford", "Haldenbrook", "HaldenbrookProject", "Mürbleford", "Muerbleford", "Murbleford"} <= set(d)
    generic = {"Orbitrap", "Xcalibur", "Exactive", "Plus", "Lipids", "Plasma", "Spray", "Voltage", "Users", "HESI",
               "Instrument", "Method", "Finnigan", "Sheath", "Lab", "Lipid", "Batch", "Project"}
    assert not generic & set(d), generic & set(d)


@pytest.mark.parametrize("token,cs", [("QMF", True), ("AB12", True), ("ab1", True), ("qmf", False), ("Qmf", False),
                                      ("HALDENBROOK", False), ("Marbleford", False)])
def test_case_rule(token, cs):
    assert dl.auto_case(token) is cs


def test_matcher_rules():
    m = dl.Matcher([dl.Entry("QMF", "user", True), dl.Entry("Haldenbrook", "user", False),
                    dl.Entry("Haldenbrook Lipid Lab", "user", False), dl.Entry("Müller", "user", False)])
    assert [e.token for e in m.search("HLB_QMF_007")] == ["QMF"]
    assert m.search("QMFX") == [] and m.search("qmf") == [] and m.search("aQMF") == []
    assert [e.token for e in m.search("HaldenbrookProject")] == ["Haldenbrook"]
    assert [e.token for e in m.search("HALDENBROOK_lipid-lab")] == ["Haldenbrook Lipid Lab"]
    assert [e.token for e in m.search("MÜLLER")] == ["Müller"]
    assert m.search("Q Exactive spectrumList Orbitrap") == []
    n = dl.Matcher([dl.Entry("1838271.10234", "candidate", False)])
    assert n.search("SN 1838271.10234") and not n.search("51838271.102345")


def test_read_denylist_rejects_bad_file(tmp_path):
    (tmp_path / "d.tsv").write_text("word\tkind\nx\ty\n")
    with pytest.raises(Exception):
        dl.read_denylist(tmp_path / "d.tsv")
    assert cli.main(["identifier-denylist", "--study", "S1", "--candidates", str(tmp_path / "nope"),
                     "--out", str(tmp_path / "o.tsv")]) == 1


def test_written_columns(tmp_path):
    dl.write_denylist(tmp_path / "d.tsv", [dl.Entry("QMF", "user", True, "user")])
    lines = (tmp_path / "d.tsv").read_text().splitlines()
    assert lines == ["token\tkind\tcase_sensitive\torigin", "QMF\tuser\ttrue\tuser"]
    assert dl.read_denylist(tmp_path / "d.tsv") == [dl.Entry("QMF", "user", True, "user")]


def test_parse_args_defaults():
    p = argparse.ArgumentParser()
    dl.add_arguments(p)
    a = p.parse_args(["--study", "S1"])
    assert a.frequency_filter == "off" and a.candidates == []


def test_base_dir_components_are_not_harvested():
    from msdeposit_tools.sensitive.denylist import relative_parts
    assert relative_parts("/data/projects/trial/Haldenbrook/run_01.raw", "/data/projects/trial") == ["Haldenbrook", "run_01"]
    assert relative_parts("/data/projects/trial/Haldenbrook/run_01.raw", None)[:2] == ["data", "projects"]
    # paths outside the base: only the shared leading part is dropped
    assert relative_parts("/data/raw/Haldenbrook/x.raw", "/data/projects/trial") == ["raw", "Haldenbrook", "x"]
    assert relative_parts("/x.raw", "/x.raw") == ["x"]   # the file name itself is always kept


def test_object_store_paths():
    from msdeposit_tools.sensitive.denylist import relative_parts
    assert relative_parts("s3://trial-in/Haldenbrook/run_01.raw", "s3://trial-in") == ["Haldenbrook", "run_01"]
