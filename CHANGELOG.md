# lifs-tools/msdeposit: Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## v1.0.0dev - [unreleased]

Initial release of lifs-tools/msdeposit, created with the [nf-core](https://nf-co.re/) template.

### `Added`

- Input contract: run sheet (one row per run), assays, samples (ISA-safe column encoding), studies YAML, optional
  contacts, publication, protocol texts, features, values, study variables, SRM transitions, deny/allow lists.
- Conversion of vendor files with ProteoWizard (`--accept_vendor_licenses`) or ThermoRawFileParser; pwiz text dumps
  and foreign mzML normalised to indexed mzML.
- mzML metadata and run metrics, deposit plan, mzML finalisation (rename or anonymise with body rewrite and
  verification), ISA-Tab, MAF, mzTab-M 2.0.0-M/2.1.0-M (`--mztab_versions`), mzQC, Goslin/SwissLipids annotation.
- Upload set assembly with manifest; checks C1–C9; public and private deposit reports; gate per study.
- Sensitive-data preset (`--sensitive_data`): mzML-only deposition, identifier denylist and scan with self-test,
  checks S1–S7, private output root, private-launch guard, redacted tool errors.
- Object-store support (S3 and compatible) for inputs and both output roots (`--private_launch_dir`).
- `msdeposit-tools` Python package and container with the domain logic and pinned offline validators.
- Test profiles `test` and `test_sensitive` with synthetic data; nf-tests.
- Test profile `test_mtbls` with real runs of MetaboLights studies MTBLS1381 (SCIEX) and MTBLS1382 (Thermo), fetched and
  verified by `tests/data/mtbls/fetch.sh`; its nf-test scans the public output for identifiers known to be in the
  original files. CI builds the `msdeposit-tools` image and caches the test data.
- `docs/submission.md`: step-by-step guide to conversion, pseudonymisation, hit review and post-hoc verification.

### `Fixed`

- CI: the image build checks the current jmzTab-m `dev-latest` validator (upstream rebuilt it on 2026-11-10); jobs
  that install Apptainer run on `ubuntu-24.04` (its package needs `libfuse3-3`, missing on 26.04); the template
  version check can read the private repository; prettier formatting; lint settings for a non-nf-core pipeline.
- ThermoRawFileParser output is re-indexed by `MZML_INDEX`: its index offsets point at the indentation before each
  element, so C2 failed it as "index invalid" and blocked every study with Thermo runs.
- Test profiles fit GitHub runners (2 CPUs, 6 GB); `test_mtbls` marks its Thermo run as `vendor=thermo`.
- Publishing the private check files works on Nextflow 26.10 (a publish `>>` inside a nested closure failed there).

### `Dependencies`

### `Deprecated`
