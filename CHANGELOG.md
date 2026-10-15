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

### `Fixed`

### `Dependencies`

### `Deprecated`
