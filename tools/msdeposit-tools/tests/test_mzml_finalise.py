import json
import re

import pytest

from helpers_mzml import (binaries, denylist_rows, edited, plain, plan_for, planted_left, write_denylist, write_plan,
                          xsd_valid)
from msdeposit_tools import cli
from msdeposit_tools.io import InputError
from msdeposit_tools.mzml import finalise, index
from synthetic import PLANTED, check_offsets, mzml_text

DEPOSITED = "QC_01_POS.mzML"
BODY_PLANTED = {"raw_name", "transition"}     # planted in spectrum titles / SRM chromatogram ids


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "in" / "converted.mzML"
    p.parent.mkdir()
    p.write_bytes(mzml_text(srm=True))
    return p


def run_finalise(src, outdir, *, mode="anonymise", deny=(), plan=None, **kw):
    pub, priv = finalise.finalise(src, outdir, plan or plan_for(), "r001", mode=mode, denylist=denylist_rows(*deny),
                                  **kw)
    out = outdir / DEPOSITED
    return pub, priv, (out.read_bytes() if out.exists() else None)


def header(data: bytes) -> bytes:
    return data[: data.index(b"<spectrumList")]


def body(data: bytes) -> bytes:
    return data[data.index(b"<spectrumList"): data.index(b"</mzML>")]


def assert_verified(pub, data):
    assert pub["status"] == "pass", pub["issues"]
    assert pub["body_sha256_in"] == pub["body_sha256_out"] and pub["body_sha256_in"]
    assert pub["element_counts_in"] == pub["element_counts_out"]
    assert pub["reread_ok"] and pub["index_ok"] and pub["checksum_ok"] and pub["ids_unique"]
    assert check_offsets(data)
    assert len(pub["sha1"]) == 40 and len(pub["sha256"]) == 64 and pub["bytes"] == len(data)


def test_rename_changes_only_ids_and_locations(src, tmp_path):
    pub, priv, data = run_finalise(src, tmp_path / "o", mode="rename")
    assert_verified(pub, data)
    orig = src.read_bytes()
    assert body(data) == body(orig)
    assert re.search(rb'<mzML [^>]*id="QC_01_POS"', data) and re.search(rb'<run id="QC_01_POS"', data)
    assert b'location="file:///"' in data and b"qmarbleford" not in data
    diff = [(a, b) for a, b in zip(header(orig).splitlines(), header(data).splitlines()) if a != b]
    assert len(header(orig).splitlines()) == len(header(data).splitlines())
    assert [re.match(rb"\s*<(\w+)", b).group(1) for _, b in diff] == [b"mzML", b"sourceFile", b"run"]
    assert pub["header_changes"]["ids_set"] == 2 and pub["header_changes"]["source_locations_sanitised"] == 1
    assert pub["header_changes"]["serial_removed"] == 0 and pub["body_rewrites"] == 0
    assert PLANTED["serial"].encode() in data      # rename mode does not anonymise
    assert xsd_valid(tmp_path / "o" / DEPOSITED)[0]


def test_rename_keeps_vendor_folder_of_directory_formats(tmp_path):
    data = mzml_text(vendor="agilent").replace(
        b'location="file:///D:/Users/qmarbleford/HaldenbrookProject/Batch7"',
        b'location="file:///D:/Users/qmarbleford/HaldenbrookProject/Batch7/HLB_qmf_plasma_007.d/AcqData"').replace(
        b'name="HLB_qmf_plasma_007.d"', b'name="MSScan.bin"')
    p = edited(tmp_path, data)
    pub, _, out = run_finalise(p, tmp_path / "r", mode="rename", plan=plan_for(vendor="agilent"))
    assert pub["status"] == "pass" and b'location="file:///HLB_qmf_plasma_007.d/AcqData"' in out
    pub, priv, out = run_finalise(p, tmp_path / "a", plan=plan_for(vendor="agilent"))
    assert pub["status"] == "pass"
    assert b'<sourceFile id="SF1" name="MSScan.bin" location="file:///S1_RAW_001.d/AcqData">' in out


def test_anonymise_header_body_rewrite_off(src, tmp_path):
    pub, priv, data = run_finalise(src, tmp_path / "o", deny=[PLANTED["raw_name"], PLANTED["transition"]],
                                   body_rewrite=False)
    assert_verified(pub, data)
    # identifying text in the body stays when the body rewrite is off (S2 blocks such studies)
    assert planted_left(data) == BODY_PLANTED
    assert planted_left(header(data)) == set()
    assert body(data) == body(src.read_bytes()).replace(PLANTED["raw_name"].encode() + b".raw", b"SF1")
    # residual fields (S3)
    h = header(data)
    assert b"MS:1000529" not in h and b"<contact" not in h and b"<userParam" not in h
    assert b'<sourceFile id="SF1" name="S1_RAW_001.raw" location="file:///">' in h
    assert b'<sample id="QC_01" name="QC_01">' in h and b'sampleRef="QC_01"' in h and b'defaultSourceFileRef="SF1"' in h
    assert re.search(rb'<mzML [^>]*id="QC_01_POS"', h) and b'<run id="QC_01_POS"' in h
    assert pub["header_changes"] == dict(serial_removed=1, source_files_renamed=1, source_locations_sanitised=0,
                                         samples_renamed=1, user_params_removed=1, contacts_removed=1, timestamps=0,
                                         ids_set=2, header_values_rewritten=0)
    assert pub["body_rewrites"] == 0 and not pub["body_rewrite"]
    assert priv["header"]["serials"] == [PLANTED["serial"]]
    assert priv["header"]["user_params"] == [{"section": "sample", "name": "Operator", "value": PLANTED["initials"]}]
    assert xsd_valid(tmp_path / "o" / DEPOSITED)[0]


def test_only_processing_user_params_are_kept(tmp_path):
    cv = b'<cvParam cvRef="MS" accession="MS:1000544" name="Conversion to mzML" value=""/>\n'
    data = mzml_text().replace(cv, cv + b'          <userParam name="Thermo/Xcalibur peak picking" value="" '
                                        b'type="xsd:string"/>\n          <userParam name="note" value="by '
                               + PLANTED["initials"].encode() + b'" type="xsd:string"/>\n')
    pub, priv, out = run_finalise(edited(tmp_path, data), tmp_path / "o")
    assert pub["status"] == "pass" and pub["header_changes"]["user_params_removed"] == 2
    assert b'<userParam name="Thermo/Xcalibur peak picking" value=""' in out and b'name="note"' not in out
    assert [u["section"] for u in priv["header"]["user_params"]] == ["sample", "processingMethod"]


def test_anonymise_body_rewrite_removes_every_planted_token(src, tmp_path):
    pub, priv, data = run_finalise(src, tmp_path / "o", deny=[PLANTED["raw_name"], PLANTED["transition"]])
    assert_verified(pub, data)
    assert planted_left(data) == set()
    assert binaries(data) == binaries(src.read_bytes())
    assert pub["body_rewrites"] == 7 and pub["body_rewrite"]
    assert b'name=X2"' in data and b'value="X1.3.3.1"' in data
    assert b'idRef="- SRM SIC Q1=760.6 Q3=184.1 sample=1 period=1 experiment=1 transition=0 name=X2"' in data
    tokens = {t["token"]: (t["placeholder"], t["count"]) for t in priv["body"]["tokens"]}
    assert tokens == {PLANTED["raw_name"]: ("X1", 6), PLANTED["transition"]: ("X2", 1)}
    assert priv["body"]["fields"] == {"userParam/@value": 6, "chromatogram/@id": 1}
    example = {"field": "userParam/@value", "old": PLANTED["raw_name"] + ".1.1.1", "new": "X1.1.1.1"}
    assert example in priv["body"]["examples"]
    pub_text = json.dumps(pub)
    assert not planted_left(pub_text.encode()) and "/" not in pub_text.replace("msdeposit/finalise/1", "")
    assert xsd_valid(tmp_path / "o" / DEPOSITED)[0]


def test_token_matching_rules(src, tmp_path):
    # short tokens (<= 4 characters) match on alphanumeric boundaries only; case per token
    pub, _, data = run_finalise(src, tmp_path / "a", deny=["QMF"])
    assert pub["status"] == "pass" and b"HLB_X1_plasma_007.1.1.1" in data and pub["body_rewrites"] == 6
    pub, _ = finalise.finalise(src, tmp_path / "c", plan_for(), "r001", mode="anonymise",
                               denylist=denylist_rows("QMF", case_sensitive=True))
    assert pub["status"] == "pass" and pub["body_rewrites"] == 0
    # separators in a token match any separator run
    pub, _, data = run_finalise(src, tmp_path / "d", deny=["hlb qmf plasma"])
    assert pub["body_rewrites"] == 6 and b"X1_007.2.2.1" in data


def test_numbers_and_nativeid_keys_are_never_rewritten(src, tmp_path):
    pub, _, data = run_finalise(src, tmp_path / "o", deny=["100", "scan", "sample", "1200"])
    assert pub["status"] == "pass" and pub["body_rewrites"] == 0
    assert body(data) == body(src.read_bytes()).replace(PLANTED["raw_name"].encode() + b".raw", b"SF1")


def test_number_like_words_are_text(tmp_path):
    data = mzml_text().replace(b'value="' + PLANTED["raw_name"].encode() + b".2.2.1", b'value="Nan')
    pub, _, out = run_finalise(edited(tmp_path, data), tmp_path / "o", deny=["Nan"])
    assert pub["status"] == "pass" and pub["body_rewrites"] == 1 and b'value="Nan' not in out


@pytest.mark.parametrize("policy,expected", [("keep", b'startTimeStamp="2024-03-05T09:41:17Z"'),
                                             ("date", b'startTimeStamp="2024-03-05T00:00:00"'), ("drop", None)])
@pytest.mark.parametrize("mode", ["rename", "anonymise"])
def test_timestamp_policy(src, tmp_path, mode, policy, expected):
    pub, _, data = run_finalise(src, tmp_path / "o", mode=mode, policy=policy)
    assert pub["status"] == "pass"
    if expected:
        assert expected in data
    else:
        assert b"startTimeStamp" not in data
    assert pub["header_changes"]["timestamps"] == (policy != "keep")
    assert xsd_valid(tmp_path / "o" / DEPOSITED)[0]


def test_corrupted_binary_fails_verification(src, tmp_path, monkeypatch):
    orig = finalise.BodyRewriter.__call__

    def corrupt(self, block):
        return re.sub(rb"<binary>e", b"<binary>f", orig(self, block), count=1)
    monkeypatch.setattr(finalise.BodyRewriter, "__call__", corrupt)
    pub, priv, data = run_finalise(src, tmp_path / "o", deny=[PLANTED["raw_name"]])
    assert data is None and pub["status"] == "fail" and "binary payload hash differs" in pub["issues"]
    assert pub["sha1"] is None and list((tmp_path / "o").iterdir()) == []


def test_corrupted_index_fails_verification(src, tmp_path, monkeypatch):
    orig = index.IndexedWriter.add
    monkeypatch.setattr(index.IndexedWriter, "add", lambda self, kind, id_, offset: orig(self, kind, id_, offset + 1))
    pub, _, data = run_finalise(src, tmp_path / "o", mode="rename")
    assert data is None and pub["status"] == "fail" and "index invalid" in pub["issues"]
    assert not (tmp_path / "o" / (DEPOSITED + ".partial")).exists()


def test_failed_run_removes_stale_output(src, tmp_path):
    out = tmp_path / "o"
    out.mkdir()
    (out / DEPOSITED).write_bytes(b"stale")
    cut = tmp_path / "cut.mzML"
    data = src.read_bytes()
    cut.write_bytes(data[: int(len(data) * 0.7)])
    pub, priv, res = run_finalise(cut, out)
    assert res is None and pub["status"] == "fail" and pub["issues"] == ["input is not a complete mzML"]
    assert priv["details"] and not list(out.iterdir())


def test_duplicate_ids_after_rewrite_fail(tmp_path):
    # after the rewrite both SRM chromatograms would be called '... transition=0 name=X1'
    data = mzml_text(srm=True).replace(b"Q1=786.6 Q3=184.1 sample=1 period=1 experiment=1 transition=1 name=PC 36:2",
                                       b"Q1=760.6 Q3=184.1 sample=1 period=1 experiment=1 transition=0 name=X1")
    p = edited(tmp_path, data)
    pub, _, res = run_finalise(p, tmp_path / "o", deny=[PLANTED["transition"]])
    assert pub["status"] == "fail" and "duplicate or missing spectrum/chromatogram ids" in pub["issues"]


def test_plain_input_and_body_source_file_refs(tmp_path):
    raw_ref = PLANTED["raw_name"].encode() + b".raw"
    data = plain(mzml_text().replace(b'<spectrum index="0" id="controllerType=0 controllerNumber=1 scan=1"',
                                     b'<spectrum index="0" sourceFileRef="' + raw_ref +
                                     b'" id="controllerType=0 controllerNumber=1 scan=1"'))
    p = tmp_path / "plain.mzML"
    p.write_bytes(data)
    pub, _, out = run_finalise(p, tmp_path / "o", body_rewrite=False)
    assert pub["status"] == "pass" and out.startswith(b'<?xml version="1.0" encoding="utf-8"?>\n<indexedmzML ')
    assert b'<spectrum index="0" sourceFileRef="SF1"' in out and raw_ref not in out
    assert xsd_valid(tmp_path / "o" / DEPOSITED)[0]


def test_escaped_values(tmp_path):
    data = mzml_text().replace(b'value="' + PLANTED["raw_name"].encode() + b".1.1.1",
                               b'value="Smith &amp; Sons ' + PLANTED["raw_name"].encode() + b".1.1.1")
    pub, _, out = run_finalise(edited(tmp_path, data), tmp_path / "o", deny=["Smith & Sons"])
    assert pub["status"] == "pass" and b"Smith" not in out and b'value="X1 HLB_qmf' in out


def test_large_file_small_chunks(tmp_path):
    p = tmp_path / "big.mzML"
    p.write_bytes(mzml_text(n_spectra=2000, srm=True))
    deny = [PLANTED["raw_name"], PLANTED["transition"]]
    pub_s, _, small = run_finalise(p, tmp_path / "s", deny=deny, chunk=1000)
    pub_b, _, big = run_finalise(p, tmp_path / "b", deny=deny)
    assert pub_s["status"] == "pass" and small == big and pub_s["body_rewrites"] == 2001
    assert pub_s["element_counts_out"] == {"spectra": 2000, "chromatograms": 3, "binary_arrays": 4006}
    assert planted_left(small) == set() and check_offsets(small)


def test_cli(src, tmp_path):
    plan = write_plan(tmp_path / "plan.json")
    deny = write_denylist(tmp_path / "denylist.tsv", PLANTED["raw_name"], PLANTED["transition"])
    out = tmp_path / "fin"
    assert cli.main(["mzml-finalise", "--mzml", str(src), "--plan", str(plan), "--run", "r001", "--mode", "anonymise",
                     "--timestamp-policy", "date", "--body-rewrite", "true", "--denylist", str(deny),
                     "--outdir", str(out)]) == 0
    assert sorted(p.name for p in out.iterdir()) == sorted([DEPOSITED, "finalise.json", "finalise.private.json",
                                                            "check_C4.r001.json"])
    pub = json.loads((out / "finalise.json").read_text())
    c4 = json.loads((out / "check_C4.r001.json").read_text())
    assert pub["status"] == c4["status"] == "pass" and c4["scope"] == "run" and c4["study"] == "S1"
    assert c4["counts"]["body_rewrites"] == 7 and c4["blocking"]
    for f in ("finalise.json", "check_C4.r001.json", DEPOSITED):
        text = (out / f).read_bytes()
        assert planted_left(text) == set() and str(tmp_path).encode() not in text
    priv = json.loads((out / "finalise.private.json").read_text())
    assert priv["source_mzml"] == "converted.mzML"
    assert cli.main(["mzml-finalise", "--mzml", str(src), "--plan", str(plan), "--run", "r999",
                     "--outdir", str(out)]) == 1


def test_run_must_be_planned(src, tmp_path):
    with pytest.raises(InputError):
        finalise.finalise(src, tmp_path, plan_for(), "r002")
    with pytest.raises(InputError):
        finalise.finalise(src, tmp_path, plan_for(deposited="../x.mzML"), "r001")
