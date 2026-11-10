import hashlib
import os

import pytest

from msdeposit_tools import cli
from msdeposit_tools.fixity import fixity_rows, tree_hash
from msdeposit_tools.io import InputError, read_table

FILES = {"analysis.tdf": b"T" * 300, "analysis.tdf_bin": b"\x00\x01" * 500, "chromatography-data.sqlite": b"S" * 7,
         "sub/method.m/submethods.xml": b"<xml/>", "sub/empty.txt": b""}


def make_tree(root, order=None, mtime=None):
    names = order or sorted(FILES)
    for name in names:
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(FILES[name])
        if mtime is not None:
            os.utime(p, (mtime, mtime))
    return root


def tree_row(rows):
    return next(r for r in rows if r["kind"] == "tree")


def test_single_file(tmp_path):
    f = tmp_path / "run_01.raw"
    f.write_bytes(b"vendor data")
    rows = fixity_rows(f, [], "S1", "S1_RAW_001")
    assert rows == [dict(study="S1", raw_id="S1_RAW_001", kind="file", member="run_01.raw", files=1, bytes=11,
                         sha256=hashlib.sha256(b"vendor data").hexdigest())]


def test_tree_hash_independent_of_order_time_and_name(tmp_path):
    a = make_tree(tmp_path / "a" / "x.d", mtime=1_000_000)
    b = make_tree(tmp_path / "b" / "renamed.d", order=sorted(FILES, reverse=True), mtime=2_000_000)
    ra, rb = fixity_rows(a, [], "S1", "R1", jobs=1), fixity_rows(b, [], "S1", "R1", jobs=3)
    assert tree_row(ra)["sha256"] == tree_row(rb)["sha256"]
    assert tree_row(ra)["files"] == len(FILES)
    assert tree_row(ra)["bytes"] == sum(map(len, FILES.values()))
    members = [r["member"] for r in ra if r["kind"] == "file"]
    assert members == sorted(members) and members[0].startswith("x.d/")
    expected = tree_hash([(k, hashlib.sha256(v).hexdigest()) for k, v in FILES.items()])
    assert tree_row(ra)["sha256"] == expected


def test_tree_hash_changes_with_content_or_names(tmp_path):
    base = tree_row(fixity_rows(make_tree(tmp_path / "a.d"), [], "S1", "R1"))["sha256"]
    changed = make_tree(tmp_path / "b.d")
    (changed / "analysis.tdf").write_bytes(b"U" * 300)
    renamed = make_tree(tmp_path / "c.d")
    (renamed / "analysis.tdf").rename(renamed / "analysis2.tdf")
    assert tree_row(fixity_rows(changed, [], "S1", "R1"))["sha256"] != base
    assert tree_row(fixity_rows(renamed, [], "S1", "R1"))["sha256"] != base


def test_symlinks(tmp_path):
    tree = make_tree(tmp_path / "x.d")
    staged = tmp_path / "work" / "x.d"
    staged.parent.mkdir()
    staged.symlink_to(tree)  # Nextflow staging: the raw path itself is a link
    (tree / "link_inside").symlink_to(tree / "analysis.tdf")
    rows = fixity_rows(staged, [], "S1", "R1")
    assert tree_row(rows)["member"] == "x.d"
    assert any(r["member"] == "x.d/link_inside" and r["bytes"] == 300 for r in rows)
    outside = tmp_path / "secret.txt"
    outside.write_text("outside")
    (tree / "link_out").symlink_to(outside)
    with pytest.raises(InputError, match="outside"):
        fixity_rows(staged, [], "S1", "R1")
    (tree / "link_out").unlink()
    (tree / "dirlink").symlink_to(tree / "sub", target_is_directory=True)
    with pytest.raises(InputError, match="symlinked directory"):
        fixity_rows(staged, [], "S1", "R1")


def test_cli_with_companions(tmp_path):
    f = tmp_path / "run.wiff"
    f.write_bytes(b"wiff")
    c = tmp_path / "run.wiff.scan"
    c.write_bytes(b"scan data")
    out = tmp_path / "out" / "fixity.tsv"
    assert cli.main(["raw-fixity", "--raw", str(f), "--companions", str(c), "--raw-id", "S1_RAW_001",
                     "--study", "S1", "--out", str(out)]) == 0
    rows = read_table(out)
    assert [r["kind"] for r in rows] == ["file", "companion"]
    assert rows[1]["member"] == "run.wiff.scan" and rows[1]["bytes"] == "9"
    assert list(rows[0]) == ["study", "raw_id", "kind", "member", "files", "bytes", "sha256"]


def test_missing_raw_is_input_error(tmp_path):
    assert cli.main(["raw-fixity", "--raw", str(tmp_path / "nope.raw"), "--raw-id", "R", "--study", "S",
                     "--out", str(tmp_path / "f.tsv")]) == 1
