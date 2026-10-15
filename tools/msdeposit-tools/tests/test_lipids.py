import json
import urllib.request

import pytest

from helpers_results import edit_study, parse_args
from msdeposit_tools.describe import lipids
from msdeposit_tools.io import read_table, write_table


@pytest.fixture
def no_network(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("network access in a test")
    monkeypatch.setattr(urllib.request, "urlopen", fail)


def run(validated, tmp_path, *extra):
    out = tmp_path / "lipid_annotation.tsv"
    assert lipids.run(parse_args(lipids, ["--study-json", validated / "S1" / "study.json", "--out", out,
                                          *extra])) == 0
    return read_table(out, "\t")


def test_goslin_offline(validated, tmp_path, no_network):
    rows = run(validated, tmp_path, "--mode", "goslin")
    assert [r["name"] for r in rows] == ["PC 34:1", "PE 36:2"]
    pc = rows[0]
    assert pc["goslin_name"] == "PC 34:1" and pc["goslin_level"] == "SPECIES"
    assert pc["formula"] == "C42H82NO8P" and pc["neutral_mass"] == "759.5778"
    assert pc["swisslipids_id"] == "" and list(rows[0]) == lipids.FIELDS


def test_alternatives_regio_and_non_lipids(no_network):
    names = ["PC O-34:2/PC P-34:1", "1,2-DG 16:0_18:1", "Cer 18:1;O2/24:0", "glucose", "PC O-34:2/PC P-34:1"]
    rows, n = lipids.annotate(names, "goslin", {}, allow_network=True)
    assert n == 0
    by = [(r["name"], r["alternative"]) for r in rows]
    assert by[:2] == [("PC O-34:2/PC P-34:1", "PC O-34:2"), ("PC O-34:2/PC P-34:1", "PC P-34:1")]
    assert len(rows) == 5          # duplicates annotated once; the ceramide '/' is not an alternative separator
    dg = rows[2]
    assert dg["goslin_name"] == "DG 16:0_18:1" and "regio isomer" in dg["note"]
    assert rows[3]["goslin_level"] == "SN_POSITION"
    assert rows[4]["formula"] == "" and "Goslin could not parse" in rows[4]["note"]


def test_swisslipids_from_cache_without_network(validated, tmp_path, no_network):
    cache = tmp_path / "sl_cache.tsv"
    write_table(cache, [dict(query="PC(34:1)", swisslipids_id="SLM:900000001", swisslipids_level="Species")],
                lipids.CACHE_FIELDS)
    before = cache.read_text()
    rows = run(validated, tmp_path, "--mode", "goslin+swisslipids", "--cache", cache, "--allow-network", "false")
    pc, pe = rows
    assert pc["swisslipids_id"] == "SLM:900000001" and pc["swisslipids_name"] == "PC(34:1)"
    assert pe["swisslipids_id"] == "" and "not looked up" in pe["note"]
    assert cache.read_text() == before


def test_swisslipids_lookup_updates_cache(validated, tmp_path, monkeypatch):
    calls = []

    def fake_lookup(query):
        calls.append(query)
        return ("SLM:900000002", "Species") if query == "PE(36:2)" else ("", "")
    monkeypatch.setattr(lipids, "lookup_swisslipids", fake_lookup)
    cache = tmp_path / "sl_cache.tsv"
    rows = run(validated, tmp_path, "--mode", "goslin+swisslipids", "--cache", cache, "--allow-network", "true")
    assert calls == ["PC(34:1)", "PE(36:2)"]
    assert rows[1]["swisslipids_id"] == "SLM:900000002"
    cached = {r["query"]: r["swisslipids_id"] for r in read_table(cache, "\t")}
    assert cached == {"PC(34:1)": "", "PE(36:2)": "SLM:900000002"}      # "not found" is cached as well
    calls.clear()
    run(validated, tmp_path, "--mode", "goslin+swisslipids", "--cache", cache, "--allow-network", "true")
    assert calls == []


def test_lookup_parses_exact_matches_only(monkeypatch):
    class Response:
        def __init__(self, data):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(self.data).encode()
    hits = [{"entity_name": "PC(34:1/0:0)", "entity_id": "SLM:1"}, {"entity_name": "PC(34:1)", "entity_id": "SLM:2",
                                                                     "classification_level": "Species"}]
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response(hits))
    monkeypatch.setattr(lipids.time, "sleep", lambda s: None)
    assert lipids.lookup_swisslipids("PC(34:1)") == ("SLM:2", "Species")
    assert lipids.lookup_swisslipids("PC(36:1)") == ("", "")


def test_swisslipids_names():
    assert lipids.swisslipids_name("Cer 18:1;O2/24:0") == "Cer(d18:1/24:0)"
    assert lipids.swisslipids_name("PC O-34:2") == "PC(O-34:2)"


def test_mode_none_and_all_features(validated, tmp_path, no_network):
    def add(s):
        s["features"].append(dict(assay="", feature="f3", name="TG 52:2", level="", formula="", mz=None, rt=None,
                                  adduct="", charge=None, database_id="", quantifier=""))
    edit_study(validated, add)
    assert run(validated, tmp_path, "--mode", "none") == []
    assert (tmp_path / "lipid_annotation.tsv").read_text().startswith("name\talternative")
    assert [r["name"] for r in run(validated, tmp_path, "--mode", "goslin")] == ["PC 34:1", "PE 36:2", "TG 52:2"]
