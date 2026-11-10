import base64
import csv
import json
import shutil

import helpers_sensitive as hs
import pytest
from synthetic import PLANTED, mzml_text, write_mzml

from msdeposit_tools import cli
from msdeposit_tools.sensitive import scan
from msdeposit_tools.sensitive import vendor_metadata as vm
from msdeposit_tools.sensitive.denylist import Entry, Matcher


@pytest.fixture
def matcher():
    return Matcher([Entry("Marbleford", "candidate", False), Entry("QMF", "candidate", True),
                    Entry("Haldenbrook", "user", False), Entry("Müller", "user", False),
                    Entry("SN-ZX9917", "candidate", True)])


def hits_of(path, matcher, display=None, **kw):
    h = scan.scan_files([(path, display or path.name)], matcher, **kw)
    return {(loc, tok) for (_, loc, tok) in h.counts}, h


def clean(tmp_path, name="QC_01_POS.mzML"):
    p = tmp_path / name
    p.write_bytes(hs.clean_mzml("QC_01_POS", "S1_RAW_001", "QC_01"))
    return p


# --- planted tokens per field and file type -----------------------------------------------------------------------
def test_mzml_header_field(tmp_path, matcher):
    p = clean(tmp_path)
    p.write_bytes(p.read_bytes().replace(b'<sample id="QC_01" name="QC_01"', b'<sample id="QC_01" name="Haldenbrook"'))
    assert hits_of(p, matcher)[0] == {("header", "Haldenbrook")}


def test_mzml_chromatogram_id_short_code(tmp_path, matcher):
    p = tmp_path / "x.mzML"
    p.write_bytes(mzml_text(plant=False, srm=True, run_id="x").replace(b"name=PC 36:2", b"name=PC 36:2_QMF"))
    assert hits_of(p, matcher)[0] == {("body", "QMF")}


def test_mzml_userparam_on_binary_line_and_payload_blanked(tmp_path, matcher):
    p = clean(tmp_path)
    data = p.read_bytes()
    k = data.index(b"</binary>")
    j = data.index(b"<binary>") + len(b"<binary>")
    data = (data[:j] + b"MarblefordInPayload" + data[j:k] + b"</binary>"
            + b'<userParam name="note" value="QMF" type="xsd:string"/>' + data[k + len(b"</binary>"):])
    p.write_bytes(data)
    assert hits_of(p, matcher)[0] == {("body", "QMF")}       # the payload token is not reported


def test_planted_synthetic_mzml_everywhere(tmp_path, matcher):
    p = write_mzml(tmp_path / "x.mzML", srm=True)
    found, h = hits_of(p, matcher)
    assert {("header", "Marbleford"), ("header", "Haldenbrook"), ("header", "QMF"), ("header", "SN-ZX9917")} <= found


@pytest.mark.parametrize("chunk", [7, 64, 1000])
def test_chunk_boundaries(tmp_path, matcher, monkeypatch, chunk):
    p = write_mzml(tmp_path / "x.mzML", srm=True)
    ref = scan.scan_files([(p, "x")], matcher).counts
    monkeypatch.setattr(scan, "CHUNK", chunk)
    assert scan.scan_files([(p, "x")], matcher).counts == ref


def test_parallel_scan_and_gzip(tmp_path, matcher):
    import gzip
    p = write_mzml(tmp_path / "x.mzML", srm=True)
    (tmp_path / "y.mzML.gz").write_bytes(gzip.compress(p.read_bytes()))
    files = [(p, "x.mzML"), (tmp_path / "y.mzML.gz", "x.mzML")]
    serial, parallel = scan.scan_files(files, matcher), scan.scan_files(files, matcher, jobs=2)
    assert serial.counts == parallel.counts and serial.total == 2 * scan.scan_files(files[:1], matcher).total


def test_length_changing_lowercase_falls_back(matcher):
    text = "İstanbul office of Marbleford"            # 'İ'.lower() has two characters
    assert [e.token for e in matcher.search(text)] == ["Marbleford"]


def test_isa_mztab_cells_and_file_names(tmp_path, matcher):
    up = hs.upload_set(tmp_path)["dir"]
    s = up / "s_S1.txt"
    s.write_text(s.read_text().replace('"Plasma 1"', '"Plasma 1 Haldenbrook"'))
    mt = up / "FILES" / "RESULTS" / "S1.mztab-2.0.0-M.mzTab"
    mt.write_text(mt.read_text() + "COM\tacquired by QMF\n")
    (up / "FILES" / "QC" / "HLB_QMF_notes.txt").write_text("nothing here\n")
    h = scan.scan_files(scan.iter_files([up]), matcher)
    assert set(h.counts) == {("s_S1.txt", "cell", "Haldenbrook"),
                             ("FILES/RESULTS/S1.mztab-2.0.0-M.mzTab", "cell", "QMF"),
                             ("FILES/QC/HLB_QMF_notes.txt", "name", "QMF")}
    assert next(r for r in h.rows if r["file"] == "s_S1.txt")["context"].startswith("[line 3]")


def test_html_report_entities_json_escapes_and_data_uris(tmp_path, matcher):
    html = tmp_path / "deposit_report.html"
    svg = base64.b64encode(b"<svg><text>Haldenbrook</text></svg>").decode()
    png = base64.b64encode(b"\x89PNGMarbleford" * 3).decode()
    html.write_text(f'<html><body><p>by &#81;MF</p><img src="data:image/svg+xml;base64,{svg}"/>'
                    f'<img src="data:image/png;base64,{png}"/></body></html>')
    assert hits_of(html, matcher)[0] == {("text", "QMF"), ("text", "Haldenbrook")}
    qc = tmp_path / "S1.mzQC"
    qc.write_text(json.dumps({"mzQC": {"contactName": "Müller"}}, ensure_ascii=True))
    assert "\\u00fc" in qc.read_text() and hits_of(qc, matcher)[0] == {("text", "Müller")}
    log = tmp_path / "x.log"
    log.write_bytes("operator Müller\n".encode("latin-1"))                 # not UTF-8
    assert hits_of(log, matcher)[0] == {("text", "Müller")}


def test_transliteration_variant_found(tmp_path):
    from msdeposit_tools.sensitive.denylist import variants
    m = Matcher([Entry("Müller", "user", False)] + [Entry(v, "variant", False) for v in variants("Müller")])
    p = tmp_path / "s_x.txt"
    p.write_text("Sample Name\nMueller_01\nMuller_02\n")
    assert {t for _, t in hits_of(p, m)[0]} == {"Mueller", "Muller"}


def test_allowed_contact_rows(tmp_path, matcher):
    i = tmp_path / "i_Investigation.txt"
    i.write_text('Study Title\t"x"\nStudy Person Last Name\t"Marbleford"\nStudy Person Affiliation\t"Haldenbrook"\n')
    assert hits_of(i, matcher)[0] == set()
    assert hits_of(i, matcher, allow_regions=False)[0] == {("cell", "Marbleford"), ("cell", "Haldenbrook")}


# --- false positives -------------------------------------------------------------------------------------------------
def test_no_false_positives_on_clean_upload_set(tmp_path, validated):
    """A denylist harvested from all synthetic vendor formats and the run sheet finds nothing in a clean set:
    generic words, instrument model names, mzML element names, units and ISA headers are not tokens."""
    raws = [(hs.thermo_raw(tmp_path / "r" / "a.raw"), "thermo"), (hs.sciex_wiff(tmp_path / "r" / "b.wiff"), "sciex"),
            (hs.bruker_tdf(tmp_path / "r" / "c.d"), "bruker_tdf"), (hs.waters_raw(tmp_path / "r" / "d.raw"), "waters"),
            (hs.agilent_d(tmp_path / "r" / "e.d"), "agilent")]
    for i, (p, v) in enumerate(raws):
        vm.extract(p, v, tmp_path / "vm" / f"R{i}")
    dl = tmp_path / "denylist.tsv"
    assert cli.main(["identifier-denylist", "--study", "S1", "--candidates", str(tmp_path / "vm"),
                     "--run-keys", str(validated / "runs.normalised.csv"), "--out", str(dl)]) == 0
    assert len(dl.read_text().splitlines()) > 30
    up = hs.upload_set(tmp_path / "up")["dir"]
    out = tmp_path / "scan"
    assert cli.main(["identifier-scan", "--denylist", str(dl), "--files", str(up), "--out-dir", str(out),
                     "--study", "S1", "--self-test"]) == 0
    s2 = json.loads((out / "check_S2.json").read_text())
    assert s2["status"] == "pass", (out / "hits.tsv").read_text()
    assert json.loads((out / "check_S1.json").read_text())["status"] == "pass"


# --- S1 self-test and S2 ---------------------------------------------------------------------------------------------
def run_scan(tmp_path, up, *extra):
    dl = hs.planted_denylist(tmp_path / "denylist.tsv")
    out = tmp_path / "out"
    rc = cli.main(["identifier-scan", "--denylist", str(dl), "--files", str(up), "--out-dir", str(out),
                   "--study", "S1", *extra])
    assert rc == 0
    return out, {c: json.loads((out / f"check_{c}.json").read_text()) for c in ("S1", "S2")
                 if (out / f"check_{c}.json").exists()}


def test_s1_and_s2_pass_on_clean_set(tmp_path):
    up = hs.upload_set(tmp_path)["dir"]
    out, checks = run_scan(tmp_path, up, "--self-test")
    assert checks["S1"]["status"] == "pass" and checks["S2"]["status"] == "pass"
    s1 = checks["S1"]["counts"]
    assert s1["file_types"] == 4 and s1["found"] == s1["planted"]
    assert checks["S1"]["counts"]["planted"] >= 12
    assert not list(out.glob("s1_*"))                                      # temporary copies removed
    assert checks["S2"]["counts"]["files"] == 9


def test_s2_fails_on_hits_and_public_check_is_clean(tmp_path):
    up = hs.upload_set(tmp_path)["dir"]
    mz = up / "FILES" / "DERIVED_FILES" / "QC_01_POS.mzML"
    mz.write_bytes(mz.read_bytes().replace(b'name="S1_RAW_001.raw"', f'name="{PLANTED["raw_name"]}.raw"'.encode()))
    out, checks = run_scan(tmp_path, up)
    assert checks["S2"]["status"] == "fail" and checks["S2"]["counts"]["hits_header"] >= 2
    public = (out / "check_S2.json").read_text()
    for tok in ("HLB", "qmf", PLANTED["raw_name"], str(tmp_path)):
        assert tok not in public
    rows = list(csv.DictReader(open(out / "hits.tsv"), delimiter="\t"))
    assert {r["token"] for r in rows} == {"HLB", PLANTED["raw_name"]}
    assert json.loads((out / "check_S2.private.json").read_text())["details"]


def test_s1_detects_a_broken_binary_blanker(tmp_path, monkeypatch):
    """A known scanner bug: lines containing <binary> were dropped, hiding text on the same line."""
    def line_dropping(self, data, final=False):
        return b"\n".join(line for line in data.split(b"\n") if b"<binary>" not in line)
    monkeypatch.setattr(scan.BinaryBlanker, "feed", line_dropping)
    up = hs.upload_set(tmp_path)["dir"]
    out, checks = run_scan(tmp_path, up, "--self-test")
    assert checks["S1"]["status"] == "fail" and checks["S2"]["status"] == "fail"
    assert "self-test" in checks["S2"]["summary"]
    details = json.loads((out / "check_S1.private.json").read_text())["details"]
    assert [d["location"] for d in details if not d["found"]] == ["body"]


def test_s1_detects_case_and_table_failures(tmp_path, monkeypatch):
    real = Matcher.__init__

    def case_sensitive_only(self, entries):
        real(self, [Entry(e.token, e.kind, True, e.origin) for e in entries])
    monkeypatch.setattr(Matcher, "__init__", case_sensitive_only)
    up = hs.upload_set(tmp_path)["dir"]
    _, checks = run_scan(tmp_path, up, "--self-test")
    assert checks["S1"]["status"] == "fail"
    monkeypatch.setattr(Matcher, "__init__", real)
    monkeypatch.setattr(scan, "scan_lines", lambda *a, **k: iter(()))
    _, checks = run_scan(tmp_path / "b", up, "--self-test")
    assert checks["S1"]["status"] == "fail"


def test_s2_fails_without_files_or_denylist(tmp_path):
    (tmp_path / "empty").mkdir()
    _, checks = run_scan(tmp_path, tmp_path / "empty")
    assert checks["S2"]["status"] == "fail"
    (tmp_path / "e.tsv").write_text("token\tkind\tcase_sensitive\torigin\n")
    up = hs.upload_set(tmp_path / "u")["dir"]
    assert cli.main(["identifier-scan", "--denylist", str(tmp_path / "e.tsv"), "--files", str(up),
                     "--out-dir", str(tmp_path / "o2"), "--study", "S1"]) == 0
    assert json.loads((tmp_path / "o2" / "check_S2.json").read_text())["status"] == "fail"


def test_out_dir_inside_scanned_tree_is_excluded(tmp_path):
    up = hs.upload_set(tmp_path)["dir"]
    shutil.copy(hs.planted_denylist(tmp_path / "d.tsv"), tmp_path / "d2.tsv")
    out = up / "scan_out"
    (out / "old").mkdir(parents=True)
    (out / "old" / "hits.tsv").write_text("file\tlocation\ttoken\tcontext\nx\tcell\tHLB\tHLB\n")
    assert cli.main(["identifier-scan", "--denylist", str(tmp_path / "d2.tsv"), "--files", str(up),
                     "--out-dir", str(out), "--study", "S1"]) == 0
    assert json.loads((out / "check_S2.json").read_text())["status"] == "pass"
