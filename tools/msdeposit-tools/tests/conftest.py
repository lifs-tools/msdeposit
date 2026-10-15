import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest

from msdeposit_tools import inputs


@pytest.fixture
def example_inputs(tmp_path):
    """Synthetic standard-mode study: sheets + mzML under tmp_path/in."""
    from synthetic import write_example_inputs
    return write_example_inputs(tmp_path / "in")


def validate_args(sheets: dict, outdir, mode="standard", **extra):
    import argparse
    p = argparse.ArgumentParser()
    inputs.add_arguments(p)
    argv = []
    for k, v in sheets.items():
        argv += [f"--{k.replace('_', '-')}", str(v)]
    argv += ["--mode", mode, "--outdir", str(outdir), "--base-dir", str(sheets["input"].parent)]
    for k, v in extra.items():
        argv += [f"--{k.replace('_', '-')}", str(v)]
    return p.parse_args(argv)


def fake_metadata(run, polarity="positive", model=("Q Exactive", "MS:1001911"), status="pass"):
    """A minimal mzml-metadata artefact (CONTRACTS.md §2) for tests of downstream steps."""
    return dict(schema="msdeposit/mzml_metadata/1", run=run, bytes=1000, sha1="ab" * 20, complete=True,
                indexed=True, checksum_ok=True,
                nativeid_format={"accession": "MS:1000768", "name": "Thermo nativeID format"},
                file_content=[["MS:1000579", "MS1 spectrum"]],
                instrument={"model": model[0], "model_accession": model[1], "serial": "SN1", "configurations": []},
                software=[], source_files=[{"id": "x", "name": "x.raw", "location": "file:///x", "sha1": "cd" * 20,
                                            "format": ["MS:1000563", "Thermo RAW format"]}],
                run_info={"id": "x", "start": "", "sample_ref": "", "default_instrument_configuration_ref": ""},
                counts={"spectra": 6, "ms1": 6}, chromatograms={"n": 1, "tic": 1}, polarity=polarity,
                mz_window=[100, 1200], mz_window_source="scan window", transitions=[], filter_strings={},
                resolving_powers={}, activations={}, collision_energies={}, injection_time_mean_ms={},
                metrics={}, issues=[], status=status)


@pytest.fixture
def validated(example_inputs, tmp_path):
    """validate-inputs output dir of the example study (standard mode)."""
    out = tmp_path / "validated"
    assert inputs.run(validate_args(example_inputs, out)) == 0
    return out
