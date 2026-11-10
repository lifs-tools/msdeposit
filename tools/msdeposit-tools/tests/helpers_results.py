"""Synthetic describe inputs for the results/QC writers: plan, mzml-metadata and finalise artefacts of the example
study."""

import argparse
import hashlib
import json
from pathlib import Path

from conftest import fake_metadata
from msdeposit_tools import plan
from msdeposit_tools.io import read_json, write_json

COMPONENTS = [{"id": "IC1", "components": [
    {"type": "source", "order": 1, "terms": [["MS:1000073", "electrospray ionization"]]},
    {"type": "analyzer", "order": 2, "terms": [["MS:1000484", "orbitrap"]]},
    {"type": "detector", "order": 3, "terms": [["MS:1000624", "inductive detector"]]}]}]
METRICS = {"rt_range_s": [6.0, 36.0], "ms_run_duration_s": 30.0, "tic_auc_ms1": 1234567.5,
           "ms1_freq_fastest_hz": 0.1667, "ms1_freq_slowest_hz": 0.1667, "ms1_jump10": 0, "ms1_fall10": 0,
           "empty_ms1": 0, "empty_ms2": 0}


def metadata(run: str, polarity: str, transitions=()) -> dict:
    m = fake_metadata(run, polarity)
    m["instrument"]["configurations"] = COMPONENTS
    m["metrics"] = dict(METRICS)
    m["counts"] = {"spectra": 6, "ms1": 6, "ms2": 0, "msn": 0, "centroid": 6, "profile": 0, "pos": 6, "neg": 0,
                   "im_arrays": 0}
    m["injection_time_mean_ms"] = {"1": 35.2}
    m["transitions"] = [dict(t) for t in transitions]
    return m


def edit_study(validated: Path, edit) -> Path:
    path = validated / "S1" / "study.json"
    s = json.loads(path.read_text())
    edit(s)
    path.write_text(json.dumps(s))
    return path


def describe_inputs(validated: Path, tmp: Path, transitions: dict | None = None) -> dict:
    """plan.json, metadata/ and finalise/ for the example study (4 runs r001-r004, assays POS and NEG).

    `transitions` maps run -> list of metadata transitions ({id, q1, q3, polarity, ce})."""
    transitions = transitions or {}
    mdir, fdir = tmp / "metadata", tmp / "finalise"
    for run, pol in (("r001", "positive"), ("r002", "positive"), ("r003", "negative"), ("r004", "negative")):
        write_json(mdir / f"{run}.json", metadata(run, pol, transitions.get(run, ())))
    p = argparse.ArgumentParser()
    plan.add_arguments(p)
    out = tmp / "plan"
    study = validated / "S1" / "study.json"
    assert plan.run(p.parse_args(["--study-json", str(study), "--metadata", str(mdir), "--runs-normalised",
                                  str(validated / "runs.normalised.csv"), "--outdir", str(out)])) == 0
    pl = read_json(out / "plan.json")
    for r in pl["runs"]:
        sha1 = hashlib.sha1(r["deposited_mzml"].encode()).hexdigest()
        write_json(fdir / f"{r['run']}.finalise.json",
                   dict(schema="msdeposit/finalise/1", run=r["run"], deposited_mzml=r["deposited_mzml"], mode="rename",
                        bytes=1000, sha1=sha1, sha256="00" * 32, status="pass"))
        write_json(fdir / f"{r['run']}.finalise.private.json", dict(schema="msdeposit/finalise-private/1"))
    return dict(study=study, plan=out / "plan.json", metadata=mdir, finalise=fdir, plan_data=pl)


def parse_args(module, argv: list) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    module.add_arguments(p)
    return p.parse_args([str(a) for a in argv])


def tools_dir() -> Path | None:
    import os
    d = os.environ.get("MSDEPOSIT_TOOLS_DIR")
    return Path(d) if d else None
