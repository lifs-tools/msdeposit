"""`msdeposit-tools <subcommand>`: one subcommand per pipeline step (CONTRACTS.md).

Each subcommand module provides `add_arguments(parser)` and `run(args) -> int`. Modules are imported only when
their subcommand is called, so a missing optional dependency affects only that step.
"""

import argparse
import importlib
import sys
import traceback

from . import __version__
from .io import InputError

SUBCOMMANDS = {
    # name: (module, one-line help)
    "validate-inputs": ("msdeposit_tools.inputs", "validate and normalise the input sheets (C1)"),
    "raw-fixity": ("msdeposit_tools.fixity", "SHA-256 of vendor files and directory trees"),
    "text-to-mzml": ("msdeposit_tools.mzml.text_to_mzml", "convert a pwiz text dump to indexed mzML"),
    "mzml-index": ("msdeposit_tools.mzml.index", "normalise a foreign mzML to indexed mzML with checksum"),
    "mzml-metadata": ("msdeposit_tools.mzml.metadata", "streaming mzML metadata and run metrics (C2)"),
    "deposit-plan": ("msdeposit_tools.plan", "per-study deposit plan and private mapping (C3)"),
    "mzml-finalise": ("msdeposit_tools.mzml.finalise", "rename or anonymise an mzML for deposition (C4)"),
    "raw-package": ("msdeposit_tools.raw_package", "package vendor raw data for upload (standard mode)"),
    "vendor-metadata": ("msdeposit_tools.sensitive.vendor_metadata", "identifier candidates and method dump"),
    "identifier-denylist": ("msdeposit_tools.sensitive.denylist", "per-study identifier denylist"),
    "identifier-scan": ("msdeposit_tools.sensitive.scan", "scan files for denylist tokens (S1 self-test, S2)"),
    "check-sensitive": ("msdeposit_tools.sensitive.checks", "sensitive-path checks S3-S5, S7"),
    "canary-scan": ("msdeposit_tools.sensitive.canary", "post-run scan of a whole output directory (S6)"),
    "isa": ("msdeposit_tools.describe.isa", "ISA-Tab investigation, sample and assay files"),
    "lipid-annotate": ("msdeposit_tools.describe.lipids", "Goslin/SwissLipids annotation of feature names"),
    "maf": ("msdeposit_tools.describe.maf", "MetaboLights metabolite assignment files"),
    "mztab": ("msdeposit_tools.describe.mztab", "mzTab-M 2.0.0-M / 2.1.0-M summary files"),
    "mzqc": ("msdeposit_tools.describe.mzqc", "mzQC run-level metrics"),
    "assemble": ("msdeposit_tools.assemble", "assemble the upload set and its manifest (C5)"),
    "check": ("msdeposit_tools.checks", "checks C6-C9 on the assembled upload set"),
    "gate": ("msdeposit_tools.gate", "combine check results per study"),
    "deposit-report": ("msdeposit_tools.report", "public and private deposit reports"),
}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    top = argparse.ArgumentParser(prog="msdeposit-tools", description=__doc__.splitlines()[0])
    top.add_argument("--version", action="version", version=f"msdeposit-tools {__version__}")
    sub = top.add_subparsers(dest="command", metavar="<subcommand>")
    for name, (_, help_) in SUBCOMMANDS.items():
        sub.add_parser(name, help=help_, add_help=False)
    if not argv or argv[0] not in SUBCOMMANDS:
        top.parse_args(argv or ["--help"])
        return 1
    name = argv[0]
    module = importlib.import_module(SUBCOMMANDS[name][0])
    parser = argparse.ArgumentParser(prog=f"msdeposit-tools {name}", description=SUBCOMMANDS[name][1])
    module.add_arguments(parser)
    args = parser.parse_args(argv[1:])
    try:
        return int(module.run(args) or 0)
    except InputError as e:
        print(f"msdeposit-tools {name}: {e}", file=sys.stderr)
        return 1
    except Exception:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
