# msdeposit-tools: artefact contracts

This file defines the files exchanged between the `msdeposit-tools` subcommands (and therefore between the
Nextflow processes). It complements `docs/design.md` (§4 inputs, §6 modules, §8 checks, §9 leak boundary).
Every JSON artefact carries a `"schema"` key `msdeposit/<name>/<major>`; readers reject unknown majors.

**Public** artefacts may end up in `outdir` or the upload set. They must never contain input paths, original
file names (sensitive mode), denylist tokens or hit contexts. **Private** artefacts go to `private_outdir` only.

## Conventions

- Encoding UTF-8; TSV with a header row; JSON indented by 1; keys in `snake_case`.
- `study` and `run` keys are opaque strings from the run sheet. File names written by the tools use
  `msdeposit_tools.io.safe_name()` (MetaboLights rule: only `.-_A-Za-z0-9`).
- Hashes are lower-case hex. `sha1` = whole file SHA-1, `sha256` = whole file SHA-256,
  `body_sha256` = SHA-256 over the concatenated base64 text of all `<binary>` elements of an mzML.
- Polarity values: `positive`, `negative`, `alternating`, `""` (unknown).
- Vendor values: `thermo`, `sciex`, `agilent`, `bruker_tdf`, `bruker_baf`, `waters`, `shimadzu`, `unknown`.
- Every subcommand: exit 0 on success (including "check failed" results, which are reported in the check JSON),
  exit 1 on a usage or input error, exit 2 on an internal error. Checks never raise to signal a failed check.

## 1. `validate-inputs` (C1)

Inputs: the parameters of design §4 (`--input`, `--assays`, `--samples`, `--studies`, optional files).

| Output | Visibility | Content |
|---|---|---|
| `runs.normalised.csv` | private | the run sheet with defaults resolved: `vendor` detected, `raw_id` assigned, `companions` expanded (`;`-separated absolute paths), `path` absolute |
| `<study>/study.json` | public-safe | `msdeposit/study/1`, see below; no paths |
| `raw_ids.tsv` | private | `study`, `raw_id`, `path` |
| `check_C1.json` | check | see §9 |

`study.json`:

```json
{
 "schema": "msdeposit/study/1",
 "study": "S1",
 "mode": "standard",
 "config": {"accession": null, "title": "...", "description": "...", "license": "CC BY 4.0",
            "release_date": "2027-01-15", "design_descriptors": [{"term": "...", "source": "", "accession": ""}],
            "factors": [{"name": "...", "type": {"term": "...", "source": "", "accession": ""}}],
            "protocols": {"Extraction": "extraction_key", "...": "..."},
            "related_studies": [], "submitters": [], "...": "other keys of --studies, passed through"},
 "assays": [{"assay": "LC-MS_POS", "technique": "LC-MS", "polarity": "positive", "acquisition": "",
             "column_model": "", "column_type": "reverse phase", "injection_system": "",
             "measurement_type": "targeted", "instrument_fallback": "", "protocol_keys": []}],
 "samples": [{"source_name": "...", "sample_name": "...",
              "characteristics": [{"name": "Organism", "value": "Homo sapiens", "source": "NCBITaxon",
                                   "accession": "http://purl.obolibrary.org/obo/NCBITaxon_9606"}],
              "factors": [{"name": "...", "value": "...", "source": "", "accession": "", "unit": ""}],
              "comments": [{"name": "...", "value": "..."}]}],
 "runs": [{"run": "r001", "assay": "LC-MS_POS", "sample": "...", "format": "vendor_raw", "vendor": "thermo",
           "run_index": null, "polarity": "positive", "deposited_name": "", "raw_id": "S1_RAW_001"}],
 "features": [], "values": [], "study_variables": [], "study_variable_members": [], "transitions": [],
 "contacts": [], "publication": null
}
```

`features`, `values`, `study_variables`, `study_variable_members`, `transitions` hold the rows of the matching
input tables (design §4.5) for this study as objects with the column names as keys (numbers parsed, empty → `null`).
`contacts` rows: `first_name`, `last_name`, `email`, `affiliation`, `address`, `orcid`, `roles` (list).

## 2. `mzml-metadata` (per run; C2)

Input: one mzML. Output `metadata.json` (`msdeposit/mzml_metadata/1`), **private** (it carries the serial number,
source file names and timestamps; public writers use only the fields marked P):

| Key | P | Content |
|---|---|---|
| `run` | P | run key (passed with `--run`) |
| `bytes`, `sha1`, `complete`, `indexed`, `checksum_ok` | P | integrity |
| `nativeid_format` | P | `{"accession": "MS:1000768", "name": "Thermo nativeID format"}` |
| `file_content` | P | list of `[accession, name]` of `fileContent` |
| `instrument` | P (not `serial`) | `{"model", "model_accession", "serial", "configurations": [{"id", "components": [{"type", "order", "terms": [[acc, name]]}]}]}` |
| `software` | P | `[{"id", "version", "terms": [[acc, name]]}]` |
| `source_files` | – | `[{"id", "name", "location", "sha1", "format": [acc, name]}]` |
| `run_info` | – | `{"id", "start", "sample_ref", "default_instrument_configuration_ref"}` |
| `counts` | P | `spectra`, `ms1`, `ms2`, `msn`, `centroid`, `profile`, `pos`, `neg`, `im_arrays` |
| `chromatograms` | P | `n`, `srm`, `tic`, `bpc`, `sim` |
| `polarity` | P | from spectra/chromatograms |
| `mz_window`, `mz_window_source` | P | `[min, max]` or null |
| `transitions` | P* | `[{"id", "q1", "q3", "polarity", "ce"}]` for SRM chromatograms (*ids may be identifying; in sensitive mode only after finalisation) |
| `filter_strings`, `resolving_powers`, `activations`, `collision_energies`, `injection_time_mean_ms` | P | summaries |
| `metrics` | P | run-level QC metrics (keys as in the CLIG `mzml_qc._metrics`: `rt_range_s`, `ms_run_duration_s`, `tic_auc_ms1`, `ms1_freq_fastest_hz`, …) |
| `issues` | P | list of strings: `incomplete`, `not indexed`, `no spectra or chromatograms`, `profile spectra present`, `polarity unknown`, `checksum mismatch` |
| `status` | P | `pass` / `fail` (C2: fail when incomplete, unreadable or empty) |

## 3. `deposit-plan` (per study; C3)

Inputs: `study.json`, the `metadata.json` of every run of the study, `runs.normalised.csv` (for original names),
`--deposited-naming original|neutral`, `--deposit-raw true|false`.

`plan.json` (`msdeposit/plan/1`, **public**):

```json
{
 "schema": "msdeposit/plan/1",
 "study": "S1", "accession": null, "prefix": "S1", "mode": "standard",
 "deposited_naming": "original", "deposit_raw": true,
 "investigation_file": "i_Investigation.txt", "sample_file": "s_S1.txt",
 "assays": [{"assay": "LC-MS_POS", "file": "a_S1_LC-MS_POS.txt", "maf": "m_S1_LC-MS_POS_maf.tsv",
             "technique": "LC-MS", "polarity": "positive", "acquisition": "", "vendor": "thermo",
             "instrument_model": {"name": "Q Exactive", "accession": "MS:1001911"},
             "runs": ["r001", "r002"]}],
 "runs": [{"run": "r001", "assay": "LC-MS_POS", "sample": "QC_01", "ms_assay_name": "QC_01_POS",
           "deposited_mzml": "QC_01_POS.mzML", "derived_path": "FILES/DERIVED_FILES/QC_01_POS.mzML",
           "raw_id": "S1_RAW_001", "raw_path": "FILES/RAW_FILES/QC_01.raw", "polarity": "positive",
           "vendor": "thermo", "nativeid_format": {"accession": "MS:1000768", "name": "Thermo nativeID format"},
           "source_sha1": "…"}],
 "raw_files": [{"raw_id": "S1_RAW_001", "packaging": "copy", "deposited": ["FILES/RAW_FILES/QC_01.raw"]}],
 "issues": [], "status": "pass"
}
```

- `prefix` = `accession` when set, else the study key (sanitised). ISA/MAF names use it; mzML names never do.
- `raw_path` is `""`, `raw_files[].deposited` empty and `packaging` `"none"` when `deposit_raw` is false
  or the run's input `format` is not `vendor_raw` (mzML/text input: no vendor file to deposit).
  `packaging`: `copy` (file + companions, e.g. `.wiff` + `.wiff.scan`; `deposited` lists all, main file first),
  `zip` (directory format → `<name>.zip`), `none`. `raw_path` is the main deposited file of the run's raw data.
  Deposited raw names are the sanitised original names; a collision between two raw paths prefixes `<raw_id>_`.
- Naming `original`: deposited stem = sanitised stem of the input file (multi-sample files: `<stem>_<run_index>`).
  Naming `neutral`: `<sample>_<POL>[_<acquisition>]` (`POS`/`NEG`/`ALT`/`UNK`). Collisions get `_2`, `_3`, … in run-sheet
  order and an issue entry; an explicit `deposited_name` wins and duplicates of explicit names fail.
- Polarity: the mzML polarity wins; a conflict with the run sheet or assay polarity is an issue (fail). Runs whose
  assay mixes vendors or instrument models fail the homogeneity check.
- `issues`: `[{"level": "error"|"warning", "code": "…", "run": "…", "message": "…"}]` (messages without paths).

`mapping.tsv` (**private**): `run`, `raw_id`, `original_path`, `original_name`, `deposited_mzml`, `source_sha1`.
`check_C3.json` as in §9.

## 4. `mzml-finalise` (per run; C4)

Inputs: the converted mzML, `plan.json` + `--run`, `--mode rename|anonymise`, `--timestamp-policy keep|date|drop`,
`--body-rewrite true|false`, `--denylist denylist.tsv` (sensitive). Output: `<deposited_mzml>` and
`finalise.json` (`msdeposit/finalise/1`, public part) + `finalise.private.json` (private):

| Key | Content |
|---|---|
| `run`, `deposited_mzml`, `mode` | |
| `bytes`, `sha1`, `sha256` | of the output file |
| `body_sha256_in`, `body_sha256_out` | must be equal |
| `element_counts_in/out` | spectra, chromatograms, binaryDataArrays |
| `header_changes` | counts per kind (`serial_removed`, `source_files_renamed`, `user_params_removed`, `contacts_removed`, `timestamps`) |
| `body_rewrites` | count of replaced tokens in ids/titles/userParams (`0` when off) |
| `reread_ok`, `index_ok`, `checksum_ok` | verification |
| `status` | `pass` / `fail` (C4) |

The private JSON adds the replaced values (`old → new`) for review. `check_C4.json` per run (`scope: run`).

## 5. Sensitive-path artefacts

| Artefact | Producer | Content |
|---|---|---|
| `vendor_metadata/<raw_id>/candidates.tsv` | `vendor-metadata` | `token`, `field`, `member` — identifier candidates from metadata members only (private) |
| `vendor_metadata/<raw_id>/method/…` | `vendor-metadata` | instrument-method dump (private) |
| `denylist.tsv` | `identifier-denylist` | `token`, `kind` (`user`, `candidate`, `original_name` (original file stems and path components; run/assay keys are checked by S7 instead), `variant`), `case_sensitive` (bool), `origin` (private) |
| `hits.tsv` | `identifier-scan` | `file`, `location` (`name`, `header`, `body`, `cell`, `text`), `token`, `context`; ≤ 20 rows per file/location/token, full counts in `check_S2.private.json` (private) |
| `check_S1.json` … `check_S7.json` | `check-sensitive` | §9 |

## 6. Describe artefacts (per study, public)

| Subcommand | Output |
|---|---|
| `isa` | `i_Investigation.txt`, `s_<prefix>.txt`, `a_*.txt` (names from the plan) |
| `lipid-annotate` | `lipid_annotation.tsv`: `name`, `alternative`, `goslin_name`, `goslin_level`, `formula`, `neutral_mass`, `swisslipids_name`, `swisslipids_id`, `swisslipids_level`, `note` |
| `maf` | `m_*_maf.tsv` per assay (names from the plan) + `maf_todo.tsv` (`maf`, `assay`, `feature`, `name`, `missing` (`;`-list of empty MAF columns), `level` (`error` for missing m/z, or RT for LC/GC/CE/SFC-MS; else `warning`), `mz_source`, `transitions`; only incomplete rows) |
| `mztab` | `<prefix>.mztab-<version>.mzTab` per selected version (e.g. `S1.mztab-2.0.0-M.mzTab`) |
| `mzqc` | `<prefix>.mzQC` |

Describe inputs: `study.json`, `plan.json`, the `metadata.json` (public fields only) and `finalise.json` of each run
(hashes of the deposited files), `--protocols <dir>`, optional `lipid_annotation.tsv`.
Unresolved text uses `[[TODO-<KIND>:<key>]]` placeholders (counted by C9).

## 7. `assemble` (per study; C5)

Builds the unpublished upload set:

```
<study>/
  i_Investigation.txt  s_*.txt  a_*.txt  m_*_maf.tsv
  FILES/DERIVED_FILES/*.mzML
  FILES/RAW_FILES/*            (standard mode only)
  FILES/RESULTS/*.mzTab
  FILES/QC/*.mzQC  FILES/QC/deposit_report.html
  upload_manifest.tsv          path, bytes, sha256, kind (isa|maf|mzml|raw|mztab|mzqc|report)
```

Files are hard-linked when possible (large data), copied otherwise.

## 8. Reports

`deposit-report --public` writes `deposit_report.html` from an allow-list of fields (design §9.6);
`--private` writes `deposit_report.private.html` with details, hits and mappings.

## 9. Check results

Every check writes `check_<ID>.json` (`msdeposit/check/1`):

```json
{"schema": "msdeposit/check/1", "study": "S1", "check": "C5", "scope": "study", "run": null,
 "status": "pass", "blocking": true, "counts": {"files": 12, "missing": 0}, "summary": "public one-liner"}
```

and, when there are details, `check_<ID>.private.json` with `{"details": [...]}` (may contain paths, tokens).
`status`: `pass`, `warn`, `fail`, `skipped`. `blocking` reflects the parameters (e.g. C6 is blocking only with
`fail_on_validation_errors`). `counts` holds numbers only; `summary` must not contain paths or tokens.

`gate` combines all check files of a study into `gate.json` (`msdeposit/gate/1`): `study`, `mode`, `status`
(`passed`|`blocked`), `checks` ({id: worst status}), `blocking_failures`, `missing_checks`, `skipped_blocking`, `runs`
({id: {status: n}} for per-run checks), `reasons`, `notes`, `summary`. Blocked when a blocking check failed or was
skipped (fail closed), an expected check is missing, or (sensitive) any of S1–S5, S7 is missing, skipped or failed.

Additional artefacts: `fixity.tsv` (`raw-fixity`, private: `study, raw_id, kind (file|companion|tree), member, files,
bytes, sha256`; tree hash = SHA-256 over sorted `relpath\tsha256\n` lines) and `<raw_id>.raw_package.json`
(`msdeposit/raw_package/1`: `files [{deposited, bytes, sha256}]`, `members`, `member_bytes`, `verified`).

**Errors in sensitive mode:** with `MSDEPOSIT_REDACT_ERRORS` set (by the `sensitive` profile), the CLI prints only the
exception type and code locations, never messages.

## 10. Validator wrappers and bundled tools

External tools are looked up in `$MSDEPOSIT_TOOLS_DIR` (container: `/opt/msdeposit/tools`):
`mtbls_validation_bundle.tar.gz` (v2.2.1), `opa` (1.21.1), `jmztabm-cli/jmztabm-cli-1.0.6.jar`,
`mzTab_2_0-M_summary-mapping.xml`, `jmztabm-validator-amd64-linux` (2.1 dev validator), optional `psi-ms.obo`
(mzQC semantic validation; fallback: the copy shipped with psims). Java comes from `PATH`.
The mzTab 2.0 semantic pass needs OLS (network); offline its CV lookups are folded into one `semantic_incomplete` warning.

Each wrapper returns the same structure and never raises for validation findings:

```python
{"available": True,           # False when the tool is missing (the check is then "skipped", never "pass")
 "tool": "name version",
 "errors": 0, "warnings": 2,
 "messages": [{"level": "error"|"warning"|"info", "rule": "rule_a_200_300_001_02", "message": "..."}]}
```

| Module | Function |
|---|---|
| `msdeposit_tools.validate_mtbls` | `validate(study_dir: Path) -> dict` (offline `mtbls local-validate` equivalent on the assembled set) |
| `msdeposit_tools.validate_mztab` | `validate(path: Path, version: str) -> dict` (`2.0.0-M`: jmzTab-m 1.0.6 + mapping; `2.1.0-M`: dev validator) |
| `msdeposit_tools.validate_mzqc` | `validate(path: Path) -> dict` (pymzqc syntax + semantic) |

## 11. ISA conventions (writer `describe/isa.py`)

- Assay `protocol_keys`: `<Protocol name>=<key>`, or a key starting with the protocol slug
  (`chromatography_hilic` → Chromatography). Unmatched keys or unknown protocol names raise an input error.
- The assay `injection_system` is written as Chromatography/CE/DI/FIA Instrument.
- Study config keys read: `title`, `description`, `license`, `release_date`, `design_descriptors`, `factors`,
  `protocols`, `related_studies`, `funders`, `omics_type`, `publication`/`publications`.
- Only samples with runs are written to the sample table.
- Validator wrapper messages may carry `file`, `column` and `values` in addition to level/rule/message.
