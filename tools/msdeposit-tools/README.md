# msdeposit-tools

The domain logic of the [lifs-tools/msdeposit](../../README.md) pipeline as a Python package and container. Every
pipeline step is one subcommand; the Nextflow modules only call them. The files exchanged between the steps are
defined in [CONTRACTS.md](CONTRACTS.md).

```
msdeposit-tools <subcommand> --help
```

| Subcommand                                          | Step                                                                                                                                        |
| --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `validate-inputs`                                   | input sheets (C1) → `study.json` per study, normalised run sheet                                                                            |
| `raw-fixity`                                        | SHA-256 of vendor files and directory trees (private)                                                                                       |
| `text-to-mzml`, `mzml-index`                        | ProteoWizard text dumps and foreign mzML → indexed mzML with checksum                                                                       |
| `mzml-metadata`                                     | streaming mzML metadata and run metrics (C2)                                                                                                |
| `deposit-plan`                                      | deposited names, assay and MAF files, raw packaging (C3)                                                                                    |
| `mzml-finalise`                                     | rename or anonymise an mzML, header and body, with verification (C4)                                                                        |
| `raw-package`                                       | vendor files for upload; deterministic ZIP64 for directory formats                                                                          |
| `vendor-metadata`, `identifier-denylist`            | identifier candidates from vendor metadata; per-study denylist                                                                              |
| `isa`, `lipid-annotate`, `maf`, `mztab`, `mzqc`     | ISA-Tab, Goslin/SwissLipids annotation, MAF, mzTab-M 2.0/2.1, mzQC                                                                          |
| `assemble`                                          | upload set and manifest (C5); `--extend` adds the report without re-hashing                                                                 |
| `check`                                             | MetaboLights, mzTab-M and mzQC validation, completeness (C6–C9)                                                                             |
| `identifier-scan`, `check-sensitive`, `canary-scan` | scanner self-test and scan (S1, S2), residual fields, raw absence, mapping isolation, key safety (S3–S5, S7), content scan of any directory |
| `gate`, `deposit-report`                            | gate decision per study; public (allow-list) and private reports                                                                            |

## Development

```bash
uv venv .venv && uv pip install -e '.[test]'
.venv/bin/pytest                                   # unit and integration tests (synthetic data only)
MSDEPOSIT_TOOLS_DIR=/path/to/tools .venv/bin/pytest -m "external or not external"   # with the validators
```

Tests marked `external` need the bundled validators (`$MSDEPOSIT_TOOLS_DIR`), tests marked `network` need network
access (mzTab-M 2.0 semantic validation looks up CV terms on OLS).

## Container

```bash
docker build -t ghcr.io/lifs-tools/msdeposit-tools:dev .
docker run --rm --network none msdeposit-tools:test         # target 'test': external tests, offline
```

The image bundles, pinned by version and SHA-256 (see `docker/fetch_tools.sh`): Open Policy Agent, the MetaboLights
validation bundle (build `bd31e44`, template version 1.0; it supports CC BY 4.0 and the `mhd-enabled` sample template,
which the `v2.2.1` tag does not), jmzTab-m 1.0.6 with the mzTab-M 2.0 summary mapping and the jmzTab-m 2.1 validator.
Files placed in `docker/tools/` are used instead of downloads (offline builds).

Set `MSDEPOSIT_REDACT_ERRORS=1` (the pipeline does in sensitive mode) to print exception types and code locations
instead of messages that may contain paths or names.
