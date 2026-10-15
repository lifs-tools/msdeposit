process ASSEMBLE_REPORT {
    tag "${study}"
    label 'process_low'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:dev'

    input:
    tuple val(study), path(plan), path(upload_set, stageAs: 'input/*'), path(report)

    output:
    tuple val(study), path("final/${study}")         , emit: upload_set
    tuple val(study), path("final/check_C5*.json")    , emit: checks
    tuple val("${task.process}"), val('msdeposit-tools'), eval('msdeposit-tools --version | cut -d" " -f2'), topic: versions, emit: versions_msdeposit_tools

    script:
    // hard-links the assembled set and adds the public report; only the report is hashed again
    """
    msdeposit-tools assemble --plan ${plan} --extend ${upload_set} --report ${report} --jobs ${task.cpus} --outdir final
    """

    stub:
    """
    mkdir -p final/${study}
    touch final/check_C5.json
    """
}
