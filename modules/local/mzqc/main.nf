process MZQC {
    tag "${study}"
    label 'process_single'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:dev'

    input:
    tuple val(study), path(study_json), path(plan), path(metadata, stageAs: 'metadata/??/*'), path(finalise, stageAs: 'finalise/??/*')

    output:
    tuple val(study), path("mzqc/*.mzQC"), emit: mzqc
    tuple val("${task.process}"), val('msdeposit-tools'), eval('msdeposit-tools --version | cut -d" " -f2'), topic: versions, emit: versions_msdeposit_tools

    script:
    """
    msdeposit-tools mzqc --study-json ${study_json} --plan ${plan} --metadata metadata --finalise finalise --outdir mzqc
    """

    stub:
    """
    mkdir mzqc
    touch mzqc/${study}.mzQC
    """
}
