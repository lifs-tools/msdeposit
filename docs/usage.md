# lifs-tools/msdeposit: Usage

> _Documentation of pipeline parameters is generated automatically from the pipeline schema (`nextflow run lifs-tools/msdeposit --help`, or the parameter page of the pipeline). This page explains the inputs, the two modes and how to run the pipeline._

## Introduction

For a step-by-step walkthrough of a deposition, see [Preparing a submission](submission.md).

msdeposit turns mass spectrometry runs plus study metadata into upload sets for [MetaboLights](https://www.ebi.ac.uk/metabolights/), one per study. A **study** becomes one MetaboLights study, and an **assay** becomes one ISA assay file. An assay groups the runs that share technique, polarity and instrument.

You describe the study in a few tables, the _input contract_. The pipeline never reads lab-specific templates or sample lists itself. If your raw information comes in another form, such as lab sequence templates, write a small **study adapter** that produces the tables; see [Study adapters](#study-adapters). The synthetic test data in [`tests/data/standard`](../tests/data/standard) is a complete minimal example, and [`tests/data/sensitive`](../tests/data/sensitive) is the same study for sensitive mode.

## Inputs

All tables are CSV files with a header row. Empty cells mean "not given". Study, run and assay **keys** may only contain `A-Z a-z 0-9 . _ -` (at most 64 characters), because they appear in file names, process tags and commands.

### Run sheet (`--input`)

One row per **run**, that is, per resulting mzML file.

```csv title="runs.csv"
study,run,assay,sample,format,path,vendor,polarity
S1,r001,POS,QC_01,vendor_raw,raw/QC_01_pos.raw,,
S1,r002,POS,P1_A,vendor_raw,raw/P1_A_pos.raw,,
S1,r003,NEG,QC_01,mzml,converted/QC_01_neg.mzML,thermo,negative
```

| Column           | Required               | Description                                                                                                                                                              |
| ---------------- | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `study`          | yes                    | study key (defined in `--studies`)                                                                                                                                       |
| `run`            | yes                    | run key, unique per study. **Sensitive mode:** it must not identify the lab or repeat the original file name (checked), because run keys appear in logs and process tags |
| `assay`          | yes                    | assay key (defined in `--assays`)                                                                                                                                        |
| `sample`         | yes                    | ISA Sample Name (defined in `--samples`)                                                                                                                                 |
| `format`         | yes                    | `vendor_raw` (vendor file or directory), `mzml`, or `pwiz_text` (ProteoWizard text dump)                                                                                 |
| `path`           | yes                    | the file or directory. Relative paths resolve against the run sheet's own directory; absolute paths and object-store URIs (`s3://…`) are allowed                         |
| `vendor`         | for `mzml`/`pwiz_text` | `thermo`, `sciex`, `agilent`, `bruker_tdf`, `bruker_baf`, `waters`, `shimadzu`; detected automatically for `vendor_raw`                                                  |
| `companions`     | no                     | `;`-separated extra files needed with the run. For SCIEX, `<name>.wiff.scan` files are found automatically                                                               |
| `run_index`      | no                     | 1-based sample index inside a multi-sample file (SCIEX `.wiff`). Several rows then share one `path`                                                                      |
| `polarity`       | no                     | expected polarity (`positive`, `negative`, `alternating`); checked against the mzML                                                                                      |
| `deposited_name` | no                     | deposited mzML file stem (overrides the naming rule)                                                                                                                     |
| `raw_id`         | no                     | public identifier of the raw file. Default `<study>_RAW_<nnn>`, numbered by sorted path                                                                                  |

### Assays (`--assays`)

One row per assay, i.e. per ISA assay file `a_<study or accession>_<assay>.txt`. All runs of an assay must share vendor, instrument model and polarity (checked).

```csv title="assays.csv"
study,assay,technique,polarity,acquisition,column_model,column_type,injection_system,measurement_type,instrument_fallback,protocol_keys
S1,POS,LC-MS,positive,,Acquity UPLC BEH C18 100 x 2.1 mm,reverse phase,Vanquish UHPLC,targeted,,extraction;chromatography
```

| Column                        | Description                                                                                                |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `technique`                   | `LC-MS`, `DI-MS`, `FIA-MS`, `GC-MS`, `CE-MS`, `MALDI-MS` (an ISA assay template must exist)                |
| `polarity`                    | expected polarity of all runs                                                                              |
| `acquisition`                 | e.g. `DDA`, `DIA`, `SRM`; it becomes an assay descriptor and, with neutral naming, part of the file name   |
| `column_model`, `column_type` | chromatography column; `column_type` uses the MetaboLights controlled values (`reverse phase`, `HILIC`, …) |
| `injection_system`            | written as the Chromatography, DI or FIA instrument                                                        |
| `measurement_type`            | `targeted`, `untargeted`, …                                                                                |
| `instrument_fallback`         | instrument name used when the mzML does not name the model (marked as inferred)                            |
| `protocol_keys`               | `;`-separated keys of protocol texts, see [Protocols](#protocols-protocols)                                |

### Samples (`--samples`)

One row per ISA sample. ISA headers are encoded in the column names, so the table stays a plain CSV:

| Column                                | ISA header                                                                                                    |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `study`, `source_name`, `sample_name` | Source Name, Sample Name                                                                                      |
| `characteristics:<Name>`              | `Characteristics[<Name>]`; add `::source` and `::accession` columns for the ontology term, `::unit` for units |
| `factor:<Name>`                       | `Factor Value[<Name>]` (+ `::source`, `::accession`, `::unit`)                                                |
| `comment:<Name>`                      | `Comment[<Name>]`                                                                                             |

```csv title="samples.csv"
study,source_name,sample_name,characteristics:Organism,characteristics:Organism::source,characteristics:Organism::accession,characteristics:Organism part,factor:Sample material,comment:Sequence position
S1,NIST SRM 1950,QC_01,Homo sapiens,NCBITaxon,http://purl.obolibrary.org/obo/NCBITaxon_9606,blood plasma,NIST SRM 1950,1
```

The MetaboLights sample template defines Organism, Organism part, Variant, Sample type, Disease and Cell type. Characteristics you do not give are written empty, never invented. Check C9 lists them.

### Studies (`--studies`)

A YAML file with an optional `defaults` block, deep-merged into every study, and one entry per study key:

```yaml title="studies.yml"
defaults:
  license: CC BY 4.0
  release_date: "2027-01-15"
  omics_type: lipidomics
  design_descriptors:
    - { term: lipidomics, source: EFO, accession: http://www.ebi.ac.uk/efo/EFO_0010333 }
  protocols:
    Extraction: extraction_folch # protocol name -> text key (see --protocols)
studies:
  S1:
    title: Interlaboratory lipid quantification in human plasma, method 1
    description: Long description, at least 150 characters for MetaboLights ...
    accession: MTBLS1234 # optional; ISA/MAF file names use it once known
    factors:
      - { name: Sample material, type: { term: material, source: EFO, accession: ... } }
    related_studies: [MTBLS1235]
    funders:
      - { name: Example Foundation, ror_id: https://ror.org/..., grant_ids: [G-1] }
```

Terms can be written as a string, as `[term, source, accession]`, or as `{term, source, accession}`.

### Optional inputs

| Parameter                  | Content                                                                                                                                                                                                                                                                           |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--contacts`               | CSV: `study` (empty = all studies), `first_name`, `last_name`, `email`, `affiliation`, `address`, `orcid`, `roles` (`;`-separated, e.g. `Principal Investigator;Submitter`)                                                                                                       |
| `--publication`            | YAML with `title`, `doi`, `pubmed_id`, `authors` (list or string), `status`                                                                                                                                                                                                       |
| `--protocols`              | Directory of protocol texts, see below                                                                                                                                                                                                                                            |
| `--features`               | Reported features (CSV): `study`, `assay` (empty = all assays), `feature`, `name`, `level`, `formula`, `mz`, `rt`, `adduct`, `charge`, `database_id`, `quantifier`                                                                                                                |
| `--values`                 | Reported values (CSV): `study`, `feature`, either `run` (one value per run) or `study_variable` (aggregate), `value`, `unit` (UO accession, e.g. `UO:0000064`), `variation`, `variation_type` (`sd`, `cv_percent`, `none`). A variation is written only when its type is declared |
| `--study_variables`        | CSV: `study`, `study_variable`, `description`, `average_function`                                                                                                                                                                                                                 |
| `--study_variable_members` | CSV: `study`, `study_variable`, `sample` (one row per member sample)                                                                                                                                                                                                              |
| `--transitions`            | SRM transitions (CSV): `study`, `assay`, `transition`, `q1`, `q3`, `polarity`, `feature`, `collision_energy`. Only needed when the mzML does not name its transitions or names them in a way that cannot be matched to features                                                   |
| `--denylist`               | Additional identifier tokens, one per line (or TSV with a `token` column). They are never filtered                                                                                                                                                                                |
| `--allowlist`              | Tokens that may appear although they were harvested as identifier candidates                                                                                                                                                                                                      |

### Protocols (`--protocols`)

A directory of Markdown text blocks: `<key>.md` applies to all studies, and `<study>/<key>.md` applies to one study and takes precedence. A protocol gets the keys given for it in the studies YAML (`protocols: {<Protocol name>: key}`) and the assay `protocol_keys` that belong to it. Keys are matched by `<Protocol name>=<key>`, or by keys starting with the protocol slug (`chromatography_hilic` → Chromatography). Without a key, the slug is used (`extraction.md`, `chromatography.md`, …).

Texts that are not found become `[[TODO-PROTOCOL:<key>]]` placeholders. The mass spectrometry, chromatography and data transformation protocols are drafted from the mzML metadata and marked as drafts. Check C9 counts all remaining placeholders.

## Modes

### Standard mode (default)

Deposits the vendor raw files (directory formats such as Agilent/Bruker `.d` and Waters `.raw` as ZIP64 archives) and the converted mzML. Deposited mzML names are the sanitised original file stems (`--deposited_naming original`).

`--identifier_scan` adds the identifier denylist and scan (S1, S2) to a standard-mode run as an audit. Hits block the study. Since standard mode does not anonymise, use `--denylist`/`--allowlist` to define what must not appear.

### Sensitive mode (`--sensitive_data`)

For ring trials and other multi-site studies, where file headers and vendor metadata reveal who measured the data: user and computer names, paths, instrument serial numbers, lab codes, and time zones.

`--sensitive_data` is a **preset**. It sets the switches below and rejects contradicting settings instead of overriding them silently:

| Switch                     | Standard default   | Sensitive preset                                                                   |
| -------------------------- | ------------------ | ---------------------------------------------------------------------------------- |
| `--deposit_raw`            | true               | false (required): raw files stay with the study coordination                       |
| `--finalise_mode`          | `rename`           | `anonymise` (required)                                                             |
| `--deposited_naming`       | `original`         | `neutral` (required): `<sample>_<POL>[_<acquisition>]`                             |
| `--identifier_scan`        | false              | true (required)                                                                    |
| `--mzml_body_rewrite`      | –                  | true. When false, identifying spectrum or chromatogram identifiers block the study |
| `--enforce_private_launch` | false              | true                                                                               |
| `--private_outdir`         | `<outdir>/private` | required, and neither nested in `--outdir` nor the other way round                 |

What the sensitive path does:

1. **Denylist per study.** It is built from the vendor metadata (Thermo headers and methods, Bruker `GlobalMetadata`, Waters `_HEADER.TXT`/`_extern.inf`/`_inlet.inf`, Agilent `AcqData` XML, SCIEX headers), the mzML metadata, the original file and folder names (below the run sheet's directory), and your `--denylist`. Transliteration variants (ü→ue) are added.
2. **Anonymised mzML.** The header loses serial numbers, contacts, free-text user parameters and source file paths; sample and run identifiers are set to the deposited names; timestamps follow `--timestamp_policy`. Denylist tokens in spectrum and chromatogram identifiers, titles and user parameters are replaced by neutral placeholders. Binary data is verified to be unchanged.
3. **Checks S1–S7.** These are listed in [output.md](output.md#checks). Any failure blocks the study, and nothing of it reaches `--outdir`.
4. **Leak boundary.**
   - Nextflow reports, the parameter file, mappings, check details, the denylist and the scan hits go to `--private_outdir`.
   - Tool errors are printed without values.
   - The **private-launch guard** refuses to start unless the launch directory and the work directory are inside `--private_outdir`, because Nextflow keeps `.nextflow.log`, its command history and `work/` there.

```bash
mkdir -p /secure/trial/private/run && cd /secure/trial/private/run
nextflow run lifs-tools/msdeposit -profile docker -w /secure/trial/private/work \
   --sensitive_data --private_outdir /secure/trial/private --outdir /secure/trial/public \
   --input runs.csv --assays assays.csv --samples samples.csv --studies studies.yml \
   --denylist lab_identifiers.txt --accept_vendor_licenses
```

Keep the console output private too: run inside `tmux`/`screen` on the secure host, or redirect it into the private directory.

## Conversion

- **ProteoWizard image.** Vendor files are converted with the ProteoWizard `msconvert` image, which includes the vendor reader libraries:
  - pulled by digest;
  - runs under Wine as root inside the container, with its outputs handed back to your user;
  - its use requires **`--accept_vendor_licenses`** (see the [ProteoWizard licences](https://proteowizard.sourceforge.io/licenses.html)).
- **Default `msconvert` options:**
  - centroiding by vendor peak picking for all MS levels, zlib compression and 64-bit arrays;
  - Waters: lock-mass scans dropped;
  - Agilent and Bruker timsTOF: ion-mobility frames combined.
  - Override them per vendor in a custom config via `ext.args` of `PWIZ_MSCONVERT`.
- **Thermo without Wine.** `--thermo_converter thermorawfileparser` converts Thermo `.raw` files with ThermoRawFileParser instead.
- **Already converted data.** mzML from elsewhere can be given with `format=mzml` and the original `vendor`; it is re-indexed and checksummed. Raw files are then not deposited.

Conversion is slow for some instruments (Agilent ion-mobility files can take hours each). `PWIZ_MSCONVERT` runs at most 8 tasks in parallel and retries with longer time limits. Use `-resume` after interruptions; finished conversions are reused.

## Results and QC files

- **MAF:** one per assay (`--maf`, on by default). It holds m/z, retention time, formula, identifiers and per-run abundances from `--features`/`--values`. MetaboLights requires m/z and, for chromatography, retention times; missing values are left empty and listed by C9 and in `maf_todo.tsv`.
- **mzTab-M:** `--mztab_versions 2.0.0-M` (default), `2.1.0-M`, or `2.0.0-M,2.1.0-M` for both. One summary file per study, with study variables from `--study_variables`.
- **mzQC:** run-level metrics of every deposited mzML.
- **Lipid annotation** (`--lipid_annotation goslin|goslin+swisslipids`):
  - Goslin normalises lipid names and adds formula and mass.
  - SwissLipids identifiers are looked up by exact name only, from `--swisslipids_cache` or, with `--allow_network`, online.

## Checks and the gate

Every check result goes into the private output. Blocking checks decide whether a study is published:

- **Always blocking:** C1–C5, and S1–S7 in sensitive mode.
- **Optionally blocking:**
  - validator errors (C6–C8) with `--fail_on_validation_errors`;
  - remaining placeholders and empty required fields (C9) with `--fail_on_todo_placeholders`. Use this for the final, submission-ready run.

A blocking check that could not run, for example because a validator is missing, blocks too. When a study is blocked, the run finishes all other studies and then exits with an error. See [output.md](output.md#checks).

## Accession numbers

Once MetaboLights has assigned an accession, add `accession: MTBLS…` to the study in `--studies` and re-run with `-resume`. Only the description steps onwards are recomputed: ISA and MAF file names contain the accession, but mzML names never do.

## Object stores (S3)

Inputs, `--outdir` and `--private_outdir` may be S3 or S3-compatible URIs (`s3://bucket/prefix`), for example de.NBI object storage or MinIO. Performance is lower, because every task downloads its inputs.

```groovy title="s3.config (passed with -c)"
aws {
    accessKey = '...'
    secretKey = '...'
    region    = 'us-east-1'
    client {
        endpoint          = 'https://objectstore.example.org'   // S3-compatible endpoint
        s3PathStyleAccess = true
    }
}
```

- **Run sheet paths:** relative paths resolve against the run sheet's location, so `s3://trial-in/runs.csv` with `path` `data/x.raw` means `s3://trial-in/data/x.raw`.
- **Copy mode:** `--publish_dir_mode` must be `copy` (the default).
- **Sensitive mode with an object-store `--private_outdir`:** the Nextflow launch and work directories must stay local and private. Name their local parent with `--private_launch_dir` (checked).
- **Bucket permissions:** keep the private bucket private with its bucket policy. Credentials stay with Nextflow and are never passed into the task containers.

## Study adapters

A study adapter is a small, study-specific script, kept outside this repository, that writes the input tables. Typical jobs:

- read lab sequence templates;
- map raw files to samples;
- fix template errors;
- choose neutral run keys;
- export the reported results as features and values;
- collect contacts, protocol texts and identifier tokens.

The CLIG ring trial adapter is an example. It lives with the private trial data and also passes the trial's raw-file identifiers and deposited names through `raw_id` and `deposited_name`, so earlier draft files stay valid.

Validate adapter output quickly without running the pipeline:

```bash
docker run --rm -v "$PWD:$PWD" -w "$PWD" ghcr.io/lifs-tools/msdeposit-tools:0.1.0 \
    msdeposit-tools validate-inputs --input runs.csv --assays assays.csv --samples samples.csv \
    --studies studies.yml --mode sensitive --outdir /tmp/validated
```

## Running the pipeline

```bash
nextflow run lifs-tools/msdeposit -profile docker -params-file params.yml
```

with a `params.yml` holding the parameters, for example:

```yaml title="params.yml"
input: "runs.csv"
assays: "assays.csv"
samples: "samples.csv"
studies: "studies.yml"
contacts: "contacts.csv"
protocols: "protocols/"
outdir: "./results/"
mztab_versions: "2.0.0-M,2.1.0-M"
accept_vendor_licenses: true
```

### Test profiles

| Profile          | Content                                                                                                                                                                                                                                                                                                  |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test`           | standard mode, synthetic mzML (one study, two assays, four runs)                                                                                                                                                                                                                                         |
| `test_sensitive` | sensitive mode, synthetic mzML with planted identifiers. They must all be removed for the study to pass; with `--mzml_body_rewrite false` the study is blocked                                                                                                                                           |
| `test_mtbls`     | sensitive mode on real data: two SCIEX runs of MTBLS1381 (mzML) and one Thermo `.raw` file of MTBLS1382 (ThermoRawFileParser). Run `tests/data/mtbls/fetch.sh` first. The nf-test scans the output for identifiers known to be in the original files ([tests/data/mtbls](../tests/data/mtbls/README.md)) |

`test_sensitive` and `test_mtbls` switch the private-launch guard off, because test runners launch from their own directories. Never do this for real data.

### Updating the pipeline

```bash
nextflow pull lifs-tools/msdeposit
```

### Reproducibility

Specify the pipeline version with `-r`, for example `-r 0.1.0`. The containers are pinned by tag or digest, the validators inside the `msdeposit-tools` image are pinned by checksum, and the MetaboLights validation bundle is pinned to a commit.

## Core Nextflow arguments

> [!NOTE]
> These options are part of Nextflow and use a _single_ hyphen (pipeline parameters use a double-hyphen).

### `-profile`

Use `docker`; it is the only container engine supported in 1.0. Apptainer is planned for a later release (the Wine-based ProteoWizard image needs a writable prefix). Combine `docker` with `test` or `test_sensitive` for the tests, and with institutional profiles from [nf-core/configs](https://github.com/nf-core/configs) for your infrastructure.

### `-resume`

Resumes from cached results. Conversions are cached leniently, so re-syncing a raw-data mirror does not trigger reconversion.

### `-c`

Custom configuration (resources, `ext.args`, object-store credentials). Do not use it for pipeline parameters; use `-params-file` instead.

## Running in the background

Long conversions can take days. Run Nextflow inside `screen`/`tmux` or with `nextflow -bg`, and keep the console log in a private location in sensitive mode.
