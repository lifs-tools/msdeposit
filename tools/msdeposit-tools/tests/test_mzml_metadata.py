import json

import pytest

from helpers_mzml import bad_checksum, bad_offset, edited, plain
from msdeposit_tools import cli
from msdeposit_tools.mzml import metadata
from synthetic import PLANTED, mzml_text


def meta_of(tmp_path, data: bytes, **kw) -> dict:
    p = tmp_path / "x.mzML"
    p.write_bytes(data)
    return metadata.metadata(p, "r001", **kw)


def test_spectra_file(tmp_path):
    m = meta_of(tmp_path, mzml_text())
    assert m["schema"] == "msdeposit/mzml_metadata/1" and m["run"] == "r001"
    assert m["status"] == "pass" and m["issues"] == []
    assert m["complete"] and m["indexed"] and m["checksum_ok"]
    assert m["counts"] == dict(spectra=6, ms1=6, ms2=0, msn=0, centroid=6, profile=0, pos=6, neg=0, im_arrays=0)
    assert m["chromatograms"] == dict(n=1, srm=0, tic=1, bpc=0, sim=0)
    assert m["polarity"] == "positive" and m["transitions"] == []
    assert m["nativeid_format"] == {"accession": "MS:1000768", "name": "Thermo nativeID format"}
    assert m["file_content"] == [["MS:1000579", "MS1 spectrum"], ["MS:1000127", "centroid spectrum"]]
    assert m["instrument"]["model"] == "Q Exactive" and m["instrument"]["model_accession"] == "MS:1001911"
    assert m["instrument"]["serial"] == PLANTED["serial"]
    assert [c["type"] for c in m["instrument"]["configurations"][0]["components"]] == ["source", "analyzer", "detector"]
    sf = m["source_files"][0]
    assert sf["id"] == sf["name"] == PLANTED["raw_name"] + ".raw"
    assert sf["format"] == ["MS:1000563", "Thermo RAW format"]
    assert sf["sha1"] == "0123456789abcdef0123456789abcdef01234567"
    assert m["run_info"] == {"id": PLANTED["raw_name"], "start": "2024-03-05T09:41:17Z", "sample_ref": "sample_1",
                             "default_instrument_configuration_ref": "IC1"}
    assert m["mz_window"] == [100.0, 1200.0] and m["mz_window_source"] == "scan window"
    assert m["metrics"]["rt_range_s"] == pytest.approx([6.0, 36.0])
    for key in ("ms_run_duration_s", "tic_auc_ms1", "ms1_freq_fastest_hz", "ms1_freq_slowest_hz", "empty_ms1",
                "ms1_jump10", "ms1_fall10"):
        assert key in m["metrics"]
    assert {s["id"] for s in m["software"]} == {"Xcalibur", "pwiz"}


def test_srm_transitions(tmp_path):
    m = meta_of(tmp_path, mzml_text(srm=True))
    assert m["chromatograms"] == dict(n=3, srm=2, tic=1, bpc=0, sim=0)
    assert [(t["q1"], t["q3"], t["polarity"], t["ce"]) for t in m["transitions"]] == [
        (760.6, 184.1, "positive", 35.0), (786.6, 184.1, "positive", 35.0)]
    assert m["transitions"][0]["id"].endswith(PLANTED["transition"])
    assert m["status"] == "pass"


def test_chromatogram_only_run_uses_tic(tmp_path):
    m = meta_of(tmp_path, mzml_text(n_spectra=0, srm=True))
    assert m["counts"]["spectra"] == 0 and m["chromatograms"]["n"] == 3 and m["status"] == "pass"
    assert m["metrics"]["rt_range_s"] == pytest.approx([6.0, 24.0]) and m["metrics"]["rt_source"] == "TIC chromatogram"
    assert m["mz_window"] == [760.6, 786.6] and m["mz_window_source"] == "SRM precursor m/z"


@pytest.mark.parametrize("vendor,acc", [("sciex", "MS:1000770"), ("waters", "MS:1000769"), ("agilent", "MS:1001508")])
def test_nativeid_formats(tmp_path, vendor, acc):
    m = meta_of(tmp_path, mzml_text(vendor=vendor))
    assert m["nativeid_format"]["accession"] == acc and "nativeID format" in m["nativeid_format"]["name"]


def test_polarity_expected(tmp_path):
    m = meta_of(tmp_path, mzml_text(polarity="negative"), expected_polarity="negative")
    assert m["polarity"] == "negative" and m["counts"]["neg"] == 6 and m["status"] == "pass"
    m = meta_of(tmp_path, mzml_text(polarity="negative"), expected_polarity="positive")
    assert "polarity mismatch" in m["issues"] and m["status"] == "fail"


@pytest.mark.parametrize("damage,issue", [(bad_checksum, "checksum mismatch"), (bad_offset, "index invalid"),
                                          (plain, "not indexed")])
def test_integrity_failures(tmp_path, damage, issue):
    m = meta_of(tmp_path, damage(mzml_text()))
    assert issue in m["issues"] and m["status"] == "fail"
    assert m["checksum_ok"] is (damage is bad_offset)
    assert m["counts"]["spectra"] == 6


def test_truncated_file_fails(tmp_path):
    data = mzml_text()
    m = meta_of(tmp_path, data[: int(len(data) * 0.6)])
    assert m["issues"][0] == "incomplete" and m["status"] == "fail" and not m["complete"] and not m["indexed"]


def test_unreadable_and_empty(tmp_path):
    m = meta_of(tmp_path, b"\x00\x01binary garbage, not an mzML")
    assert m["issues"] == ["unreadable"] and m["status"] == "fail"
    data = mzml_text(n_spectra=0)
    start, end = data.index(b"      <chromatogramList"), data.index(b"    </run>")
    m = metadata.metadata(edited(tmp_path, data[:start] + data[end:]), "r001")
    assert "no spectra or chromatograms" in m["issues"] and m["status"] == "fail"


def test_non_failing_issues(tmp_path):
    data = mzml_text().replace(b'accession="MS:1000127" name="centroid spectrum"',
                               b'accession="MS:1000128" name="profile spectrum"')
    data = data.replace(b'<cvParam cvRef="MS" accession="MS:1000130" name="positive scan" value=""/>', b"")
    m = metadata.metadata(edited(tmp_path, data), "r001")
    assert m["counts"]["profile"] == 6 and m["polarity"] == ""
    assert m["issues"] == ["profile spectra present", "polarity unknown"] and m["status"] == "pass"


def test_chunk_size_does_not_matter(tmp_path):
    p = tmp_path / "big.mzML"
    p.write_bytes(mzml_text(n_spectra=2000, srm=True))
    small, big = metadata.metadata(p, "r1", chunk=997), metadata.metadata(p, "r1")
    assert small == big and small["counts"]["spectra"] == 2000 and small["indexed"] and small["checksum_ok"]


def test_cli_writes_metadata_and_check(tmp_path):
    src = tmp_path / "in.mzML"
    src.write_bytes(mzml_text(srm=True))
    out = tmp_path / "meta" / "metadata.json"
    assert cli.main(["mzml-metadata", "--mzml", str(src), "--run", "r001", "--study", "S1", "--out", str(out),
                     "--expected-polarity", "positive"]) == 0
    m = json.loads(out.read_text())
    assert m["status"] == "pass" and len(m["transitions"]) == 2
    c2 = json.loads((tmp_path / "meta" / "check_C2.r001.json").read_text())
    assert c2["check"] == "C2" and c2["scope"] == "run" and c2["run"] == "r001" and c2["study"] == "S1"
    assert c2["status"] == "pass" and c2["counts"]["spectra"] == 6 and c2["counts"]["srm_chromatograms"] == 2
    assert str(tmp_path) not in json.dumps(c2) and PLANTED["raw_name"] not in json.dumps(c2)
    bad = tmp_path / "bad.mzML"
    bad.write_bytes(bad_checksum(mzml_text()))
    assert cli.main(["mzml-metadata", "--mzml", str(bad), "--run", "r002", "--out", str(tmp_path / "m2.json"),
                     "--check-dir", str(tmp_path / "checks")]) == 0
    c2 = json.loads((tmp_path / "checks" / "check_C2.r002.json").read_text())
    assert c2["status"] == "fail" and "checksum mismatch" in c2["summary"]
    assert cli.main(["mzml-metadata", "--mzml", str(tmp_path / "missing.mzML"), "--run", "r"]) == 1
