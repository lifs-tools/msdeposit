# lifs-tools/msdeposit: Output

## Introduction

The pipeline writes to two places:

- **`--outdir` (public):** only the **upload sets of studies that passed the gate**, one directory per study, ready for MetaboLights.
- **`--private_outdir` (private):** everything else:
  - check results and their details, the gate decision and the private deposit report;
  - mappings to the original files;
  - in sensitive mode, the denylists and identifier hits;
  - the upload sets of blocked studies.

  The default in standard mode is `<outdir>/private`. In sensitive mode it is required and must not be nested in `--outdir` or the other way round.

Both may be local directories or object-store URIs (`s3://…`).

## Public output: upload sets

```
<outdir>/
  <study>/
    i_Investigation.txt
    s_<prefix>.txt                      sample table
    a_<prefix>_<assay>.txt              one assay table per assay
    m_<prefix>_<assay>_maf.tsv          metabolite assignment files (--maf)
    FILES/
      DERIVED_FILES/<name>.mzML         centroided, indexed mzML (anonymised in sensitive mode)
      RAW_FILES/<name>[.zip]            vendor raw files (standard mode with --deposit_raw)
      RESULTS/<prefix>.mztab-<version>.mzTab
      QC/<prefix>.mzQC                  run-level metrics
      QC/deposit_report.html            public deposit report (--deposit_report)
    upload_manifest.tsv                 path, bytes, SHA-256 and kind of every file
  pipeline_info/                        standard mode only (in sensitive mode: private)
```

`<prefix>` is the MetaboLights accession once it is set in `--studies`, otherwise the study key. File names follow the MetaboLights rules (`.-_A-Za-z0-9` only).

- **mzML:** the files are named after the original files in standard mode and `<sample>_<POL>[_<acquisition>]` in sensitive mode. Their `mzML/@id` and `run/@id` equal the file stem.
- **Raw files:** single-file formats are copied with their companions. Directory formats are packed into deterministic ZIP64 archives (stored, fixed timestamps), whose members are verified against the original tree.
- **ISA assay tables:** they reference the deposited files. In sensitive mode `Raw Spectral Data File` is empty, and `Comment[Raw data file identifier]` carries the public raw-file identifier, which links the deposited mzML to the raw file kept by the study coordination.
- **Public deposit report (`FILES/QC/deposit_report.html`):** it summarises check results C1–C9, file counts and sizes, tool and validator versions, and the deposition parameters. It is written before the sensitive checks and the gate, so it does not show the final gate status. In sensitive mode it is built only from fields known to be safe (statuses, counts, versions, parameter names, deposited names and raw-file identifiers), and S2 scans it like every other file.

Publishing hard-links the files on local storage (no second copy) and copies them to object stores.

## Private output

```
<private_outdir>/
  <study>/
    gate.json                           gate decision: status, check statuses, reasons
    deposit_report.private.html         full report: check details, hits, mappings, original names
    checks/check_<ID>[.<run>].json      public part of each check (statuses and counts)
    checks/check_<ID>[.<run>].private.json   details (messages, file names, tokens)
    plan.json                           deposit plan (deposited names, assays, raw files)
    mapping.tsv                         run → raw id → original path/name → deposited mzML
    finalise/<run>.private.json         what the finalisation changed (old → new values)
    fixity/<raw_id>.fixity.tsv          SHA-256 per raw file and directory tree hash
    denylist.tsv                        identifier tokens (sensitive mode / --identifier_scan)
    hits.tsv                            identifier scan hits with context (sensitive mode)
  blocked/<study>/                      upload sets of blocked studies, for review
  runs.normalised.csv                   run sheet with resolved paths, vendors and raw ids
  raw_ids.tsv                           raw id → original path
  gate_status.tsv                       study → passed | blocked
  pipeline_info/                        sensitive mode: Nextflow reports and parameters
```

**Keep the private output for as long as the raw data is kept.** In sensitive mode it holds the only link between the deposited files and the original files and labs. Do not share or publish it.

## Checks

Each check writes `check_<ID>.json` with its status (`pass`, `warn`, `fail`, `skipped`), whether it was blocking, and counts. Messages and file names go to `check_<ID>.private.json`. `gate.json` combines them per study. A study is **blocked** when:

- a blocking check failed;
- a blocking check could not run, for example because a validator is missing;
- an expected check is missing;
- in sensitive mode, any of S1–S5 or S7 did not pass.

### All modes

| ID  | Check                                                                                                                                | Blocking                           | What to do when it fails                                                                            |
| --- | ------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------- | --------------------------------------------------------------------------------------------------- |
| C1  | Input sheets: schema, references between sheets, unique keys, vendors, files present                                                 | yes (run stops)                    | fix the sheets (details in `check_C1.private.json` and the console)                                 |
| C2  | Converted mzML: complete, indexed, checksum, spectra or chromatograms present, expected polarity                                     | yes (per run)                      | check the conversion log of the run; reconvert or exclude the run                                   |
| C3  | Deposit plan: unique names, every run planned, homogeneous assays, polarity consistent                                               | yes                                | split assays, fix polarity or `deposited_name` in the sheets                                        |
| C4  | Finalisation: binary data unchanged, element counts, index, checksum, independent re-read                                            | yes (per run)                      | report a bug, attaching `finalise/<run>.private.json`                                               |
| C5  | Upload set: every referenced file present, no unreferenced files, SHA-256 manifest, MetaboLights file names, no empty files or links | yes                                | usually follows from an earlier problem; see the details                                            |
| C6  | MetaboLights validation (pinned validation bundle, offline)                                                                          | with `--fail_on_validation_errors` | complete the metadata; most errors at first are missing descriptions, publication or protocol texts |
| C7  | mzTab-M validation for each selected version (the 2.0 semantic pass needs network access)                                            | with `--fail_on_validation_errors` | see the messages                                                                                    |
| C8  | mzQC schema and semantic validation                                                                                                  | with `--fail_on_validation_errors` | see the messages                                                                                    |
| C9  | Completeness: `[[TODO-…]]` placeholders, empty required ISA fields, MAF rows without m/z or retention time                           | with `--fail_on_todo_placeholders` | add protocol texts, characteristics and feature m/z/RT                                              |

### Sensitive mode (all blocking)

| ID  | Check                                                                                                                                                                               | What to do when it fails                                                                                                                                                                       |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| S1  | Scanner self-test: invented tokens planted into copies of each file type must all be found                                                                                          | report a bug; the scan cannot be trusted                                                                                                                                                       |
| S2  | Identifier scan of every file of the upload set, including file names, all mzML text outside binary data, ISA, MAF, mzTab-M, mzQC and the report                                    | look up the hits in `hits.tsv`. Either the anonymisation missed a field (report it), the token comes from your metadata sheets (fix them), or it is a false positive (add it to `--allowlist`) |
| S3  | No residual identifying fields in the anonymised mzML: serial numbers, source paths, contacts, free-text parameters, timestamps against the policy, ids equal to the deposited name | report a bug                                                                                                                                                                                   |
| S4  | No vendor raw data in the upload set; the raw-file column is empty and the identifier comment present                                                                               | check `deposited_name` and the inputs                                                                                                                                                          |
| S5  | Private files (mappings, denylists, hits) neither in nor referenced by the upload set                                                                                               | report a bug                                                                                                                                                                                   |
| S6  | `--outdir` contains only the upload sets of passed studies (checked at the end of the run)                                                                                          | remove the unexpected entries, find out how they got there                                                                                                                                     |
| S7  | Run and assay keys and published paths contain no denylist token                                                                                                                    | choose neutral run and assay keys                                                                                                                                                              |

Identifier hits are never printed or written to public files. The counts are in the public check file, the hits in `hits.tsv` and in the private report.

## Pipeline information

<details markdown="1">
<summary>Output files</summary>

- `pipeline_info/` (in `--outdir` in standard mode, in `--private_outdir` in sensitive mode)
  - Reports generated by Nextflow: `execution_report.html`, `execution_timeline.html`, `execution_trace.txt` and `pipeline_dag.html`.
  - Parameters used by the pipeline run: `params.json`.
  - Software versions of every step: `msdeposit_software_versions.yml`.

</details>

[Nextflow](https://docs.seqera.io/platform-cloud/reports/overview) provides excellent functionality for generating various reports relevant to the running and execution of the pipeline. This will allow you to troubleshoot errors with the running of the pipeline, and also provide you with other information such as launch commands, run times and resource usage.
