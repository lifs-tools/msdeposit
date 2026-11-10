import csv
import json
import shutil

import helpers_sensitive as hs
import pytest

from msdeposit_tools import cli
from msdeposit_tools.sensitive import scan


@pytest.fixture
def outdir(tmp_path):
    info = hs.upload_set(tmp_path / "work")
    out = tmp_path / "outdir"
    shutil.copytree(info["dir"], out / "S1")
    (out / "pipeline_info").mkdir()
    (out / "pipeline_info" / "execution_report.html").write_text("<html><body>run completed</body></html>")
    (out / "S1" / "check_S2.json").write_text(json.dumps({"status": "pass"}))
    return out


def canary(tmp_path, root, *extra):
    dl = hs.planted_denylist(tmp_path / "private" / "denylist.tsv")
    q = tmp_path / "private" / "quarantine"
    rc = cli.main(["canary-scan", "--denylist", str(dl), "--root", str(root), "--quarantine-dir", str(q),
                   *map(str, extra)])
    check = json.loads((q / "check_S6.json").read_text()) if (q / "check_S6.json").exists() else None
    return rc, check, q


def test_clean_outdir_passes(tmp_path, outdir):
    rc, check, q = canary(tmp_path, outdir, "--fail-exit")
    assert rc == 0 and check["status"] == "pass" and check["scope"] == "global"
    assert check["counts"]["files"] == 11 and check["counts"]["quarantined"] == 0
    assert check["counts"]["self_test_found"] == check["counts"]["self_test_planted"] > 0
    assert (outdir / "pipeline_info" / "execution_report.html").exists()
    assert [p.name for p in q.iterdir() if p.is_dir()] == []                 # self-test copies removed


def test_hits_are_quarantined(tmp_path, outdir):
    (outdir / "pipeline_info" / "execution_report.html").write_text("<html>workDir /data/Haldenbrook/work</html>")
    d = outdir / "S1" / "FILES" / "HLB_extra"
    d.mkdir()
    (d / "a.txt").write_text("harmless")
    (d / "b.txt").write_text("harmless")
    rc, check, q = canary(tmp_path, outdir)
    assert rc == 0 and check["status"] == "fail"
    assert check["counts"]["quarantined"] == 2 and check["counts"]["hits"] >= 3
    assert not (outdir / "pipeline_info" / "execution_report.html").exists() and not d.exists()
    assert (q / "pipeline_info" / "execution_report.html").exists()
    assert (q / "S1" / "FILES" / "HLB_extra" / "b.txt").exists()
    assert (outdir / "S1" / "i_Investigation.txt").exists()                 # unaffected outputs stay
    rows = list(csv.DictReader(open(q / "canary_hits.tsv"), delimiter="\t"))
    assert {r["token"] for r in rows} == {"Haldenbrook", "HLB"}
    public = (q / "check_S6.json").read_text()
    assert "Haldenbrook" not in public and "HLB" not in public


def test_console_log_and_fail_exit(tmp_path, outdir):
    log = tmp_path / "private" / "console.log"
    log.parent.mkdir(parents=True)
    log.write_text("ERROR ~ msconvert: cannot open D:\\Users\\qmarbleford\\run.raw\n")
    rc, check, _ = canary(tmp_path, outdir, "--console-log", log, "--fail-exit")
    assert rc == 3 and check["status"] == "fail" and check["counts"]["console_hits"] == 1
    assert check["counts"]["quarantined"] == 0 and log.exists()


def test_broken_scanner_fails(tmp_path, outdir, monkeypatch):
    monkeypatch.setattr(scan, "scan_lines", lambda *a, **k: iter(()))
    rc, check, _ = canary(tmp_path, outdir)
    assert check["status"] == "fail" and "self-test" in check["summary"]


def test_quarantine_inside_root_is_refused(tmp_path, outdir):
    dl = hs.planted_denylist(tmp_path / "d.tsv")
    assert cli.main(["canary-scan", "--denylist", str(dl), "--root", str(outdir),
                     "--quarantine-dir", str(outdir / "q")]) == 1
