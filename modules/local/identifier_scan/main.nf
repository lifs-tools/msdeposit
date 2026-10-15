process IDENTIFIER_SCAN {
    tag "${study}"
    label 'process_medium'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:dev'

    input:
    tuple val(study), path(upload_set), path(denylist)

    output:
    tuple val(study), path("scan/check_S*.json"), emit: checks
    tuple val(study), path("scan/hits.tsv")      , emit: hits
    tuple val("${task.process}"), val('msdeposit-tools'), eval('msdeposit-tools --version | cut -d" " -f2'), topic: versions, emit: versions_msdeposit_tools

    script:
    """
    msdeposit-tools identifier-scan --denylist ${denylist} --files ${upload_set} --out-dir scan --study ${study} \
        --self-test --jobs ${task.cpus}
    """

    stub:
    """
    mkdir scan
    touch scan/check_S1.json scan/check_S2.json scan/hits.tsv
    """
}
