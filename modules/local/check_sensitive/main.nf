process CHECK_SENSITIVE {
    tag "${study}"
    label 'process_low'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:dev'

    input:
    tuple val(study), path(upload_set), path(plan), path(denylist)
    path runs_normalised
    val private_dir
    val timestamp_policy

    output:
    tuple val(study), path("checks/check_S*.json"), emit: checks
    tuple val("${task.process}"), val('msdeposit-tools'), eval('msdeposit-tools --version | cut -d" " -f2'), topic: versions, emit: versions_msdeposit_tools

    script:
    """
    msdeposit-tools check-sensitive --study ${study} --upload-set ${upload_set} --plan ${plan} \
        --private-dir '${private_dir}' --runs-normalised ${runs_normalised} --denylist ${denylist} \
        --timestamp-policy ${timestamp_policy} --checks S3,S4,S5,S7 --check-dir checks
    """

    stub:
    """
    mkdir checks
    touch checks/check_S3.json
    """
}
