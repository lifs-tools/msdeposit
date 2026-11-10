import json
import os
import struct
import zipfile

import pytest
from helpers_assembly import make_plan, write_plan

from msdeposit_tools import cli
from msdeposit_tools.io import InputError, file_hashes
from msdeposit_tools.raw_package import build_zip, package, verify_zip

TREE = {"AcqData/MSScan.bin": b"\x00" * 2000, "AcqData/Contents.xml": b"<Contents/>",
        "AcqData/sub dir/info.xml": b"<i/>", "desktop.ini": b"[x]"}


def make_dir(root, reverse=False, mtime=None):
    for name in sorted(TREE, reverse=reverse):
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(TREE[name])
        if mtime:
            os.utime(p, (mtime, mtime))
    return root


def test_zip_is_deterministic(tmp_path):
    a = make_dir(tmp_path / "a" / "P1_A.d", mtime=1_500_000_000)
    b = make_dir(tmp_path / "b" / "P1_A.d", reverse=True, mtime=1_700_000_000)
    build_zip(a, tmp_path / "1.zip", "P1_A.d")
    build_zip(b, tmp_path / "2.zip", "P1_A.d")
    assert (tmp_path / "1.zip").read_bytes() == (tmp_path / "2.zip").read_bytes()


def test_zip_layout_and_verification(tmp_path):
    src = make_dir(tmp_path / "P1_A.d")
    members = build_zip(src, tmp_path / "x.zip", "P1_A.d")
    assert verify_zip(tmp_path / "x.zip", members) == []
    with zipfile.ZipFile(tmp_path / "x.zip") as zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]
        assert names == sorted(names)
        assert names[0] == "P1_A.d/"
        assert "P1_A.d/AcqData/sub dir/info.xml" in names  # member names below the root are kept
        for i in infos:
            assert i.date_time == (1980, 1, 1, 0, 0, 0)
            assert i.compress_type == zipfile.ZIP_STORED
        files = [i for i in infos if not i.is_dir()]
        raw = (tmp_path / "x.zip").read_bytes()
        for i in files:  # ZIP64 extra field in every local file header (force_zip64)
            n, m = struct.unpack("<HH", raw[i.header_offset + 26:i.header_offset + 30])
            assert raw[i.header_offset + 30 + n:i.header_offset + 32 + n] == b"\x01\x00" and m >= 20
        assert {i.filename: zf.read(i) for i in files} == {f"P1_A.d/{k}": v for k, v in TREE.items()}


def test_verification_detects_corruption_and_missing_members(tmp_path):
    src = make_dir(tmp_path / "P1_A.d")
    members = build_zip(src, tmp_path / "x.zip", "P1_A.d")
    data = bytearray((tmp_path / "x.zip").read_bytes())
    at = data.find(b"<Contents/>")
    data[at + 1] = ord("X")
    (tmp_path / "bad.zip").write_bytes(bytes(data))
    assert any("Bad CRC" in p or "differs" in p for p in verify_zip(tmp_path / "bad.zip", members))
    extra = dict(members, **{"P1_A.d/ghost.bin": (1, "0" * 64)})
    assert any("member list" in p for p in verify_zip(tmp_path / "x.zip", extra))


def test_package_zip_and_copy(tmp_path):
    plan = make_plan()
    d = make_dir(tmp_path / "in" / "P1_A.d")
    res = package(d, [], plan, "S1_RAW_002", tmp_path / "out")
    assert res["packaging"] == "zip" and res["verified"] and res["members"] == len(TREE)
    assert res["files"][0]["deposited"] == "FILES/RAW_FILES/P1_A.d.zip"
    out = tmp_path / "out" / "P1_A.d.zip"
    assert res["files"][0]["sha256"] == file_hashes(out, ("sha256",))["sha256"]

    wiff = tmp_path / "in" / "QC 01.wiff"
    wiff.write_bytes(b"wiff" * 10)
    scan = tmp_path / "in" / "QC 01.wiff.scan"
    scan.write_bytes(b"scan" * 10)
    plan["raw_files"][0]["deposited"] = ["FILES/RAW_FILES/QC_01.wiff", "FILES/RAW_FILES/QC_01.wiff.scan"]
    res = package(wiff, [scan], plan, "S1_RAW_001", tmp_path / "out2")
    assert [f["deposited"] for f in res["files"]] == plan["raw_files"][0]["deposited"]
    assert os.path.samefile(wiff, tmp_path / "out2" / "QC_01.wiff")  # hard link
    assert (tmp_path / "out2" / "QC_01.wiff.scan").read_bytes() == b"scan" * 10
    assert not (tmp_path / "out2" / "QC_01.wiff").is_symlink()


def test_package_none_and_unknown_raw_id(tmp_path):
    plan = make_plan(mode="sensitive", deposit_raw=False)
    f = tmp_path / "x.wiff"
    f.write_bytes(b"x")
    with pytest.raises(InputError, match="not deposited"):
        package(f, [], plan, "S1_RAW_001", tmp_path / "out")
    with pytest.raises(InputError, match="not in the plan"):
        package(f, [], make_plan(), "S1_RAW_999", tmp_path / "out")
    with pytest.raises(InputError, match="needs a directory"):
        package(f, [], make_plan(), "S1_RAW_002", tmp_path / "out")


def test_cli(tmp_path):
    plan_path = write_plan(tmp_path / "plan.json", make_plan())
    d = make_dir(tmp_path / "in" / "P1_A.d")
    assert cli.main(["raw-package", "--raw", str(d), "--plan", str(plan_path), "--raw-id", "S1_RAW_002",
                     "--outdir", str(tmp_path / "out")]) == 0
    rec = json.loads((tmp_path / "out" / "S1_RAW_002.raw_package.json").read_text())
    assert rec["schema"] == "msdeposit/raw_package/1" and rec["files"][0]["bytes"] > 2000
    assert str(tmp_path) not in json.dumps(rec)
