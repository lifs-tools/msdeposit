import gzip

import pytest

from helpers_mzml import bad_checksum, bad_offset, binaries, plain, xsd_valid
from msdeposit_tools import cli
from msdeposit_tools.io import InputError
from msdeposit_tools.mzml import index
from synthetic import check_offsets, mzml_text


@pytest.mark.parametrize("chunk", [7, 100, 4096, index.CHUNK])
def test_structure_scan_any_chunk_size(tmp_path, chunk):
    p = tmp_path / "a.mzML"
    p.write_bytes(mzml_text(srm=True))
    st = index.scan(p, chunk)
    assert st.root == "indexedmzML" and st.complete and st.index_ok and st.checksum_ok and st.valid
    assert st.counts() == {"spectra": 6, "chromatograms": 3, "binary_arrays": 18}
    assert st.duplicate_ids == 0 and st.missing_ids == 0
    assert st.body_sha256() == index.scan(p).body_sha256()


def test_valid_file_is_linked(tmp_path):
    src = tmp_path / "a.mzML"
    src.write_bytes(mzml_text())
    r = index.reindex(src, tmp_path / "out" / "a.mzML")
    assert r["action"] in ("linked", "copied")
    assert (tmp_path / "out" / "a.mzML").read_bytes() == src.read_bytes()
    if r["action"] == "linked":
        assert (tmp_path / "out" / "a.mzML").stat().st_ino == src.stat().st_ino


@pytest.mark.parametrize("damage", ["plain", "checksum", "offset", "gzip"])
def test_reindex_repairs(tmp_path, damage):
    good = mzml_text(srm=True)
    data = {"plain": plain, "checksum": bad_checksum, "offset": bad_offset, "gzip": gzip.compress}[damage](good)
    src = tmp_path / "in.mzML"
    src.write_bytes(data)
    if damage != "gzip":
        assert not index.scan(src).valid
    out = tmp_path / "out.mzML"
    r = index.reindex(src, out, chunk=333)
    assert r["action"] == "reindexed"
    res = out.read_bytes()
    st = index.scan(out)
    assert st.valid and check_offsets(res)
    assert binaries(res) == binaries(good)
    assert st.counts() == {"spectra": 6, "chromatograms": 3, "binary_arrays": 18}
    assert xsd_valid(out)[0]
    assert not (tmp_path / "out.mzML.partial").exists()


def test_reindex_large_file_small_chunks(tmp_path):
    src = tmp_path / "big.mzML"
    src.write_bytes(plain(mzml_text(n_spectra=2000, srm=True)))
    out_small, out_big = tmp_path / "s.mzML", tmp_path / "b.mzML"
    index.reindex(src, out_small, chunk=1000)
    index.reindex(src, out_big)
    assert out_small.read_bytes() == out_big.read_bytes()
    st = index.scan(out_small, chunk=999)
    assert st.valid and st.counts()["spectra"] == 2000 and check_offsets(out_small.read_bytes())


def test_incomplete_input_is_rejected(tmp_path):
    data = mzml_text()
    src = tmp_path / "cut.mzML"
    src.write_bytes(data[: len(data) // 2])
    with pytest.raises(InputError):
        index.reindex(src, tmp_path / "out.mzML")
    assert not (tmp_path / "out.mzML").exists()
    (tmp_path / "x.mzML").write_bytes(b"not xml at all")
    assert cli.main(["mzml-index", "--mzml", str(tmp_path / "x.mzML"), "--out", str(tmp_path / "o.mzML")]) == 1


def test_cli(tmp_path, capsys):
    src = tmp_path / "in.mzML"
    src.write_bytes(plain(mzml_text()))
    assert cli.main(["mzml-index", "--mzml", str(src), "--out", str(tmp_path / "out.mzML")]) == 0
    assert index.scan(tmp_path / "out.mzML").valid
    assert str(tmp_path) not in capsys.readouterr().out


def test_writer_escapes_ids(tmp_path):
    data = mzml_text(n_spectra=2).replace(b'id="controllerType=0 controllerNumber=1 scan=1"',
                                          b'id="a &amp; b &quot;c&quot; &lt;1&gt;"')
    src = tmp_path / "in.mzML"
    src.write_bytes(plain(data))
    index.reindex(src, tmp_path / "out.mzML")
    res = (tmp_path / "out.mzML").read_bytes()
    assert b'idRef="a &amp; b &quot;c&quot; &lt;1&gt;"' in res
    assert index.scan(tmp_path / "out.mzML").valid
