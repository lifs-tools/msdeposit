# MetaboLights test data

Three real runs from two public MetaboLights studies of the LipidCreator publication
([doi:10.1038/s41467-020-15960-z](https://doi.org/10.1038/s41467-020-15960-z)), deposited again under neutral keys:

| Study key | Source | Instrument | Runs | Input format | Converter |
| --- | --- | --- | --- | --- | --- |
| `SRM` | [MTBLS1381](https://www.ebi.ac.uk/metabolights/MTBLS1381), targeted lipid mediators in platelets | SCIEX QTRAP 6500 | `QTrap007485_031618-Unst1`, `QTrap007549_031618-1CRP2` | `mzml` (the public mzML) | none, re-indexed only |
| `DIA` | [MTBLS1382](https://www.ebi.ac.uk/metabolights/MTBLS1382), DIA validation in platelets | Thermo Q Exactive HF | `QExHF03_HPLC_0000195` | `vendor_raw` (`.raw`) | ThermoRawFileParser |

The data files are not part of the repository. `fetch.sh` downloads them into `data/` and checks them against
`SHA256SUMS`, which holds the checksums MetaboLights publishes in `<study>/HASHES/data_sha256.json`:

```bash
tests/data/mtbls/fetch.sh            # about 53 MB, for -profile test_mtbls
tests/data/mtbls/fetch.sh --vendor   # also the SCIEX .wiff/.wiff.scan files for runs_vendor.csv
```

Set `MTBLS_FTP` to use a mirror instead of `https://ftp.ebi.ac.uk/pub/databases/metabolights/studies/public`.

## Why these files

The studies are public, but their files still carry everything a ring trial would have to hide, so they work as a
realistic test of the sensitive path:

- **SCIEX mzML (MTBLS1381):** the original file names hold the instrument run number and the sample, and the source
  file location holds a path on the acquisition PC (`D:\Metabolights-MTBLS1381-…\Qtrap_mediators`).
- **Thermo raw file (MTBLS1382):** the header holds the logon account, the workstation name, the data and method
  paths (`D:\Data\HPLC12`, `D:\Methods\…`), the method name and the sample list folder.

`expected_identifiers.txt` lists these strings. It is **not** passed to the pipeline. `tests/mtbls.nf.test` uses it
after the run:

1. **Positive control:** every token must occur in one of the input files, as ASCII or UTF-16LE, so a clean scan
   is not vacuous.
2. **Post-hoc leak scan:** no token may occur in the path or the content of any file in `--outdir`. The base64
   peak data of mzML files is skipped, because short tokens occur in it by chance.

The test also checks the gate (S1–S5 and S7 must pass), that the pipeline found the identifiers itself (they are in
the private `denylist.tsv`), the neutral mzML names, and that no raw files are published.

## What real data needed

Two adjustments to the input tables were needed. A real submission typically needs the same:

- **Polarity:** MetaboLights lists MTBLS1381 as negative mode, but the SRM method has a second experiment with 6
  positive transitions. Check C2 reports such runs as `alternating`, so the run sheet and the assay say
  `alternating`.
- **False positives:** words of the original folder names (`Platelets`, `Mediator`, `Profiling`, `Validation`) are
  harvested as identifier candidates, and they also describe the study. Without `allowlist.txt`, S2 blocks both
  studies because of their own titles. Review such hits in `<private_outdir>/<study>/hits.tsv` before adding
  them to the allowlist.

## Vendor conversion with ProteoWizard

`runs_vendor.csv` uses the SCIEX `.wiff` files instead of the mzML. The `.wiff.scan` companions are found
automatically. It needs the ProteoWizard vendor-reader image and therefore `--accept_vendor_licenses`, so it is
not part of CI:

```bash
tests/data/mtbls/fetch.sh --vendor
nextflow run . -profile test_mtbls,docker --outdir results/public --private_outdir results/private \
    --input tests/data/mtbls/runs_vendor.csv --thermo_converter pwiz --accept_vendor_licenses
```
