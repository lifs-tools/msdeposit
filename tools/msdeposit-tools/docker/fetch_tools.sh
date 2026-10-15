#!/bin/sh
# Fetch the external validators bundled in the msdeposit-tools image into DEST (CONTRACTS.md §10), each pinned by
# version and SHA-256. A file of the same name in LOCAL (default: docker/tools/ of the build context, gitignored) is
# used instead of downloading, e.g. offline or when a download URL disappears; it must match the same checksum
# unless the checksum is overridden (env / --build-arg, e.g. JMZTABM_VALIDATOR_SHA256 for a newer dev build).
#
#   sh docker/fetch_tools.sh /opt/msdeposit/tools [docker/tools]
#
# Layout of DEST: opa, mtbls_validation_bundle.tar.gz, jmztabm-cli/jmztabm-cli-1.0.6.jar (+ examples, cv-mapping),
# mzTab_2_0-M_summary-mapping.xml, jmztabm-validator-amd64-linux, tools.versions.yml.
set -eu

DEST=${1:?usage: fetch_tools.sh <dest> [<local dir>]}
LOCAL=${2:-}

# OPA (Open Policy Agent), static Linux build
OPA_VERSION=${OPA_VERSION:-1.21.1}
OPA_URL=${OPA_URL:-https://github.com/open-policy-agent/opa/releases/download/v${OPA_VERSION}/opa_linux_amd64_static}
OPA_SHA256=${OPA_SHA256:-668506eb17a2eaa1fce6cc0d1f42ef85125d4ac5bda5fc74d1152d0c77145031}
# MetaboLights validation bundle, validation_version 2.2.1, template version 1.0: the build of commit bd31e44
# (2026-09-30, branch `test`) that msdeposit was developed and tested with. The bundle of tag v2.2.1 (commit 6ddfeeb,
# sha256 f7e77dbe…) carries the same version label but older rules and templates.
MTBLS_BUNDLE_VERSION=${MTBLS_BUNDLE_VERSION:-2.2.1 (bd31e44)}
MTBLS_BUNDLE_URL=${MTBLS_BUNDLE_URL:-https://raw.githubusercontent.com/EBI-Metabolights/mtbls-validation/bd31e44f94b8f40846673ab95ba8498fb9a72185/docs/bundle.tar.gz}
MTBLS_BUNDLE_SHA256=${MTBLS_BUNDLE_SHA256:-eb5f47423a3061e8a431a3fffcb1770d6dfb906ec9edfeabe1bc82c291b7d7bd}
# jmzTab-m 1.0.6 command line (Maven Central)
JMZTABM_CLI_VERSION=${JMZTABM_CLI_VERSION:-1.0.6}
JMZTABM_CLI_URL=${JMZTABM_CLI_URL:-https://repo1.maven.org/maven2/de/isas/mztab/jmztabm-cli/${JMZTABM_CLI_VERSION}/jmztabm-cli-${JMZTABM_CLI_VERSION}-bin.zip}
JMZTABM_CLI_SHA256=${JMZTABM_CLI_SHA256:-d77abaedbf87f38dc5298ee756859dc6d465c76b623254139d26e346795c95a4}
# mzTab-M 2.0 summary-file semantic mapping (HUPO-PSI/mzTab-M, pinned commit)
MZTAB_MAPPING_URL=${MZTAB_MAPPING_URL:-https://raw.githubusercontent.com/HUPO-PSI/mzTab-M/2c646f15b824346b2de9f4562d5ae002d7b3af15/schema/mzTab_2_0-M_summary-mapping.xml}
MZTAB_MAPPING_SHA256=${MZTAB_MAPPING_SHA256:-6068bc77c22037914f9ff99d4d716d4f79a579ec368141493a8837e90246bd14}
# jmzTab-m 2.1 development validator (2.1.0-SNAPSHOT). Only published under the moving release tag `dev-latest`:
# the checksum pins the build tested with msdeposit; when upstream replaces it, the download fails the checksum and
# the build needs the tested binary in docker/tools/ or a new JMZTABM_VALIDATOR_SHA256.
JMZTABM_VALIDATOR_URL=${JMZTABM_VALIDATOR_URL:-https://github.com/lifs-tools/jmzTab-m/releases/download/dev-latest/jmztabm-validator-amd64-linux}
JMZTABM_VALIDATOR_SHA256=${JMZTABM_VALIDATOR_SHA256:-29a66cb58916f4ad98ca470b25f0d83aece1c25f57daefb469b05ec229a365c3}

mkdir -p "$DEST"

# fetch <file name in DEST and LOCAL> <url> <sha256>
fetch() {
    name=$1 url=$2 sha=$3
    if [ -n "$LOCAL" ] && [ -f "$LOCAL/$name" ]; then
        echo "using $LOCAL/$name"
        cp "$LOCAL/$name" "$DEST/$name.part"
    else
        echo "downloading $url"
        curl -fsSL --retry 3 --retry-delay 5 -o "$DEST/$name.part" "$url"
    fi
    if ! echo "$sha  $DEST/$name.part" | sha256sum -c - >/dev/null; then
        echo "SHA-256 mismatch for $name (expected $sha, got $(sha256sum "$DEST/$name.part" | cut -d' ' -f1))" >&2
        rm -f "$DEST/$name.part"
        exit 1
    fi
    mv "$DEST/$name.part" "$DEST/$name"
}

fetch opa "$OPA_URL" "$OPA_SHA256"
fetch mtbls_validation_bundle.tar.gz "$MTBLS_BUNDLE_URL" "$MTBLS_BUNDLE_SHA256"
fetch "jmztabm-cli-${JMZTABM_CLI_VERSION}-bin.zip" "$JMZTABM_CLI_URL" "$JMZTABM_CLI_SHA256"
fetch mzTab_2_0-M_summary-mapping.xml "$MZTAB_MAPPING_URL" "$MZTAB_MAPPING_SHA256"
fetch jmztabm-validator-amd64-linux "$JMZTABM_VALIDATOR_URL" "$JMZTABM_VALIDATOR_SHA256"

python3 -m zipfile -e "$DEST/jmztabm-cli-${JMZTABM_CLI_VERSION}-bin.zip" "$DEST/"
rm "$DEST/jmztabm-cli-${JMZTABM_CLI_VERSION}-bin.zip"
test -f "$DEST/jmztabm-cli/jmztabm-cli-${JMZTABM_CLI_VERSION}.jar"
chmod 0755 "$DEST/opa" "$DEST/jmztabm-validator-amd64-linux"
chmod -R a+rX "$DEST"

cat > "$DEST/tools.versions.yml" <<EOF
opa: "${OPA_VERSION}"
mtbls_validation_bundle: "${MTBLS_BUNDLE_VERSION}"
jmztabm-cli: "${JMZTABM_CLI_VERSION}"
jmztabm-validator: "2.1.0-SNAPSHOT (dev-latest, sha256 ${JMZTABM_VALIDATOR_SHA256})"
mztab_2_0_summary_mapping: "sha256 ${MZTAB_MAPPING_SHA256}"
EOF
echo "tools in $DEST:"
ls -l "$DEST"
