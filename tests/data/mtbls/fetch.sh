#!/usr/bin/env bash
# Downloads the MetaboLights test files into data/ next to this script and verifies them against SHA256SUMS
# (the checksums MetaboLights publishes in <study>/HASHES/data_sha256.json). Files that are already present and
# valid are not downloaded again.
#   tests/data/mtbls/fetch.sh            # files for -profile test_mtbls (about 53 MB)
#   tests/data/mtbls/fetch.sh --vendor   # also the SCIEX .wiff/.wiff.scan files for runs_vendor.csv (about 0.8 MB)
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
base="${MTBLS_FTP:-https://ftp.ebi.ac.uk/pub/databases/metabolights/studies/public}"
mkdir -p "$here/data"
pattern='\.(mzML|raw)$'
[ "${1:-}" = "--vendor" ] && pattern='.'
grep -E "$pattern" "$here/SHA256SUMS" | while read -r sum rel; do
    study="${rel%%/*}"; name="${rel#*/}"; dest="$here/data/$name"
    if [ -f "$dest" ] && echo "$sum  $dest" | sha256sum --check --status; then
        echo "ok      $name"; continue
    fi
    echo "fetch   $study/FILES/$name"
    curl --fail --silent --show-error --location --retry 3 -o "$dest.part" "$base/$study/FILES/$name"
    echo "$sum  $dest.part" | sha256sum --check --status || { echo "checksum mismatch: $name" >&2; rm -f "$dest.part"; exit 1; }
    mv "$dest.part" "$dest"
done
