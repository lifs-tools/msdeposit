process PWIZ_MSCONVERT {
    tag "${meta.id}"
    label 'process_medium'
    label 'pwiz'
    // pwiz 3.0.26121 with vendor readers (Wine); pulled by digest, its use requires --accept_vendor_licenses
    container 'docker.io/proteowizard/pwiz-skyline-i-agree-to-the-vendor-licenses@sha256:52d830174eec595fc9fb17628effbbacec8b931fc13620a87fb44c77263daaef'

    input:
    tuple val(meta), path(raw, stageAs: 'raw/*'), path(companions, stageAs: 'raw/*')

    output:
    tuple val(meta), path("${meta.conv}.mzML"), emit: mzml
    tuple val("${task.process}"), val('pwiz'), eval('cat pwiz_version.txt'), topic: versions, emit: versions_pwiz

    script:
    // ext.args: per-vendor options from conf/modules.config (e.g. --ignoreCalibrationScans for Waters)
    def args = task.ext.args ?: ''
    def run_index = meta.run_index ? "--runIndexSet ${(meta.run_index as int) - 1}" : ''
    """
    export WINEDEBUG=-all
    rc=0
    wine msconvert ${raw} --mzML --zlib --64 --filter "peakPicking vendor msLevel=1-" ${args} ${run_index} \\
        -o . --outfile ${meta.conv}.mzML || rc=\$?
    # the Wine prefix is root-owned, so the container runs as root: hand the outputs back to the task owner
    sed -n 's/.*<software id="pwiz" version="\\([^"]*\\)".*/\\1/p' ${meta.conv}.mzML 2>/dev/null | head -n 1 > pwiz_version.txt || true
    chown -R --reference=. . 2>/dev/null || true
    exit \$rc
    """

    stub:
    """
    touch ${meta.conv}.mzML
    echo 3.0.26121 > pwiz_version.txt
    """
}
