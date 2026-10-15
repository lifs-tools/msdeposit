import pytest

from helpers_mzml import text_dump, xsd_valid
from msdeposit_tools import cli
from msdeposit_tools.io import InputError
from msdeposit_tools.mzml import index, metadata, text_to_mzml
from synthetic import check_offsets


def dump(tmp_path, text, name="run_1.txt"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_plain_dump(tmp_path):
    src = dump(tmp_path, text_dump(4))
    out = tmp_path / "run_1.mzML"
    r = text_to_mzml.convert(src, out, vendor="shimadzu", polarity="negative")
    assert r == {"chromatograms": 4, "srm": 4, "values": 48}
    data = out.read_bytes()
    assert index.scan(out).valid and check_offsets(data) and xsd_valid(out)[0]
    assert b'<mzML ' in data and b'id="run_1"' in data and b"Shimadzu instrument model" in data
    m = metadata.metadata(out, "r1")
    assert m["status"] == "pass" and m["polarity"] == "negative" and m["chromatograms"]["srm"] == 4
    assert m["transitions"][2] == {"id": "PC 32:1##Q1=720.6 Q3=184.07 CE=-14.999", "q1": 720.6, "q3": 184.07,
                                   "polarity": "negative", "ce": 14.999}
    assert m["nativeid_format"] == {"accession": "MS:1000824", "name": "no nativeID format"}
    assert m["source_files"][0]["name"] == "run_1.txt" and m["source_files"][0]["location"] == "file:///"
    assert len(m["source_files"][0]["sha1"]) == 40
    assert str(tmp_path).encode() not in data


def test_nested_dump_with_cvparams_and_seconds(tmp_path):
    out = tmp_path / "n.mzML"
    r = text_to_mzml.convert(dump(tmp_path, text_dump(style="nested", unit="second")), out, run_id="nested")
    assert r["chromatograms"] == 2 and r["srm"] == 1
    data = out.read_bytes()
    assert b'unitAccession="UO:0000010" unitName="second"' in data and b"total ion current chromatogram" in data
    assert xsd_valid(out)[0]
    m = metadata.metadata(out, "r1")
    assert m["transitions"] == [{"id": "SRM SIC Q1=760.6 Q3=184.1 name=PC 34:1", "q1": 760.6, "q3": 184.1,
                                 "polarity": "positive", "ce": 35.0}]
    assert m["metrics"]["rt_range_s"] == [1.0, 4.5] and m["chromatograms"]["tic"] == 1
    assert m["instrument"]["model_accession"] == "MS:1000031"


@pytest.mark.parametrize("text,msg", [
    ("spectrumList (1 spectra):\n  spectrum:\n    index: 0\n", "not supported"),
    ("chromatogramList:\n   index: 0\n   id: a\n   binaryDataArray:\n   cvParam: time array, minutes\n"
     "   binary: [3] 1 2\n", "header says 3"),
    ("chromatogramList:\n   index: 0\n   id: a\n   binaryDataArray:\n   cvParam: time array, minutes\n"
     "   binary: [1] 1\n", "lacks"),
    ("chromatogramList:\n   index: 0\n   id: a\n   binaryDataArray:\n   cvParam: ms level array\n"
     "   binary: [1] 1\n", "unsupported"),
    ("chromatogramList:\n", "no chromatograms")])
def test_bad_dumps(tmp_path, text, msg):
    with pytest.raises(InputError, match=msg):
        text_to_mzml.convert(dump(tmp_path, text), tmp_path / "o.mzML")
    assert not (tmp_path / "o.mzML").exists()


def test_round_trip_detects_a_wrong_value(tmp_path, monkeypatch):
    orig = text_to_mzml.chromatogram_xml

    def wrong(i, c, polarity):
        if i == 1:
            c = dict(c, intensity=[v + 1 for v in c["intensity"]])
        return orig(i, c, polarity)
    monkeypatch.setattr(text_to_mzml, "chromatogram_xml", wrong)
    with pytest.raises(RuntimeError, match="chromatogram 1 intensity"):
        text_to_mzml.convert(dump(tmp_path, text_dump(3)), tmp_path / "o.mzML")
    assert not (tmp_path / "o.mzML").exists() and not (tmp_path / "o.mzML.partial").exists()


def test_cli(tmp_path, capsys):
    src = dump(tmp_path, text_dump(2))
    out = tmp_path / "x.mzML"
    assert cli.main(["text-to-mzml", "--text", str(src), "--out", str(out), "--vendor", "sciex",
                     "--instrument-model", "MS:1002583|TripleTOF 6600"]) == 0
    assert b'accession="MS:1002583" name="TripleTOF 6600"' in out.read_bytes()
    assert "round trip ok" in capsys.readouterr().out
    assert cli.main(["text-to-mzml", "--text", str(src), "--out", str(out), "--instrument-model", "bad"]) == 1
    assert cli.main(["text-to-mzml", "--text", str(tmp_path / "missing.txt"), "--out", str(out)]) == 1
