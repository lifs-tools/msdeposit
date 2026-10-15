process DEPOSIT_PLAN {
    tag "${study}"
    label 'process_single'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:dev'

    input:
    tuple val(study), path(study_json), path(metadata, stageAs: 'metadata/??/*')
    path runs_normalised
    val deposited_naming
    val deposit_raw

    output:
    tuple val(study), path("plan/plan.json")       , emit: plan
    tuple val(study), path("plan/mapping.tsv")     , emit: mapping
    tuple val(study), path("plan/check_C3*.json")  , emit: checks
    tuple val("${task.process}"), val('msdeposit-tools'), eval('msdeposit-tools --version | cut -d" " -f2'), topic: versions, emit: versions_msdeposit_tools

    script:
    """
    msdeposit-tools deposit-plan --study-json ${study_json} --metadata metadata \\
        --runs-normalised ${runs_normalised} --deposited-naming ${deposited_naming} \\
        --deposit-raw ${deposit_raw} --outdir plan
    """

    stub:
    """
    mkdir plan
    touch plan/plan.json plan/mapping.tsv plan/check_C3.json
    """
}
