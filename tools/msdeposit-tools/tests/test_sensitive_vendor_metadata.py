import csv

import helpers_sensitive as hs
import pytest

from msdeposit_tools import cli
from msdeposit_tools.sensitive import vendor_metadata as vm


def candidates(path):
    with open(path, newline="") as fh:
        return {(r["token"], r["field"]) for r in csv.DictReader(fh, delimiter="\t")}


def tokens(path):
    return {t for t, _ in candidates(path)}


def test_thermo_header_paths_and_method_strings(tmp_path):
    raw = hs.thermo_raw(tmp_path / "raw" / "HLB_qmf_plasma_007.raw")
    assert cli.main(["vendor-metadata", "--raw", str(raw), "--raw-id", "S1_RAW_001",
                     "--outdir", str(tmp_path / "vm")]) == 0
    out = tmp_path / "vm" / "S1_RAW_001"
    c = candidates(out / "candidates.tsv")
    assert ("qmarbleford", "account") in c and ("HALDEN-PC07", "account") in c        # file header audit tags
    assert {"HaldenbrookProject", "Haldenbrook", "qmarbleford", "HLB_qmf_plasma_007"} <= tokens(out / "candidates.tsv")
    assert ("Haldenbrook Lipid Lab", "organisation") in c                          # lab name in a free-text comment
    assert ("Mürbleford", "person") in c and ("Quentin", "person") in c            # non-ASCII UTF-16 strings
    lines = (out / "method" / "method_strings.txt").read_text().splitlines()
    sv = [i for i, line in enumerate(lines) if line == "Spray Voltage (kV): 3.50"]
    assert len(sv) == 3                                       # consecutive duplicate collapsed, later repeats kept
    assert all(b - a > 1 for a, b in zip(sv, sv[1:]))
    assert "AfterNoise: ignored-setting" in lines


def test_sciex_strings_and_accounts(tmp_path):
    raw = hs.sciex_wiff(tmp_path / "x.wiff")
    vm.extract(raw, "sciex", tmp_path / "o")
    c = candidates(tmp_path / "o" / "candidates.tsv")
    assert ("HLB-MS-PC07", "host") in c and ("HALDEN", "host") in c and ("qmarbleford", "person") in c
    assert ("HLB_sequence_2024", "sequence") in c


def test_bruker_tdf_global_metadata_and_symlinks(tmp_path):
    d = hs.bruker_tdf(tmp_path / "raw" / "run1.d", outside=tmp_path / "elsewhere")
    before = (d / "analysis.tdf").stat().st_mtime_ns
    stats = vm.extract(d, "bruker_tdf", tmp_path / "o")
    c = candidates(tmp_path / "o" / "candidates.tsv")
    assert ("Quentin Marbleford", "person") in c and ("1838271.10234", "serial") in c
    assert ("Haldenbrook", "path") in c                                    # MethodName path component
    assert ("QMF", "person") in c and ("qmf", "person") in c               # SampleInfo.xml attr, .m permname
    assert ("q.marbleford@haldenbrook.example", "email") in c
    assert ("timsControl", "sequence") not in c and not any("2024-03-05" in t for t, _ in c)
    toks = {t for t, _ in c}
    assert "OutsideOperatorName" not in toks and "evil" not in toks     # symlinked member outside the raw path
    assert stats["skipped_outside"] == 1
    assert "SECRETINBINARY" not in toks                                 # spectra/binary members are never read
    assert (tmp_path / "o" / "method" / "GlobalMetadata.tsv").read_text().count("\n") == 8
    assert (d / "analysis.tdf").stat().st_mtime_ns == before and not (d / "analysis.tdf-journal").exists()


def test_waters_and_agilent(tmp_path):
    w = hs.waters_raw(tmp_path / "w.raw")
    vm.extract(w, "waters", tmp_path / "ow")
    c = candidates(tmp_path / "ow" / "candidates.tsv")
    assert ("WAA123", "serial") in c and ("qmarbleford", "person") in c and ("HLB-2024-07", "sequence") in c
    assert ("Haldenbrook.PRO", "path") in c
    assert "InTheSpectraFile" not in tokens(tmp_path / "ow" / "candidates.tsv")
    a = hs.agilent_d(tmp_path / "a.d")
    vm.extract(a, "agilent", tmp_path / "oa")
    c = candidates(tmp_path / "oa" / "candidates.tsv")
    assert ("Quentin Marbleford", "person") in c and ("SG1234X567", "serial") in c
    assert ("SG", "serial") not in c                                       # serials are not split
    assert ("HLB_qmf_plasma_007", "sequence") in c
    assert sorted(p.name for p in (tmp_path / "oa" / "method").iterdir()) == [
        "AcqData__Devices.xml", "AcqData__lipids.m__AcqMethod.xml", "AcqData__sample_info.xml"]


def test_size_limits(tmp_path, monkeypatch):
    a = hs.agilent_d(tmp_path / "a.d")
    monkeypatch.setattr(vm, "MAX_MEMBER", 120)
    stats = vm.extract(a, "agilent", tmp_path / "o")
    assert stats["skipped_large"] >= 1
    monkeypatch.setattr(vm, "MAX_STRINGS", 1400)
    raw = hs.thermo_raw(tmp_path / "t.raw")
    vm.extract(raw, "thermo", tmp_path / "ot")
    assert "AfterNoise" not in (tmp_path / "ot" / "method" / "method_strings.txt").read_text()


def test_detects_vendor_and_rejects_bad_input(tmp_path):
    w = hs.waters_raw(tmp_path / "w.raw")
    assert cli.main(["vendor-metadata", "--raw", str(w), "--raw-id", "R1", "--outdir", str(tmp_path / "o")]) == 0
    assert (tmp_path / "o" / "R1" / "method" / "_HEADER.TXT").exists()
    assert cli.main(["vendor-metadata", "--raw", str(tmp_path / "missing.raw"), "--vendor", "thermo",
                     "--raw-id", "R2", "--outdir", str(tmp_path / "o")]) == 1


@pytest.mark.parametrize("key,field", [("Operator Name", "person"), ("Acquired By", "person"), ("Computer", "host"),
                                       ("InstrumentSerialNumber", "serial"), ("Sample List", "sequence"),
                                       ("Spray Voltage", None), ("Comment", "free_text")])
def test_classify_key(key, field):
    assert vm.classify_key(key) == field
