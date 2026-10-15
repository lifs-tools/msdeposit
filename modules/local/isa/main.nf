process ISA {
    tag "${study}"
    label 'process_single'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:dev'

    input:
    tuple val(study), path(study_json), path(plan), path(metadata, stageAs: 'metadata/??/*'), path(finalise, stageAs: 'finalise/??/*')
    path protocols, stageAs: 'protocols'
    val profile
    val category
    val timestamp_policy

    output:
    tuple val(study), path("isa"), emit: dir
    tuple val("${task.process}"), val('msdeposit-tools'), eval('msdeposit-tools --version | cut -d" " -f2'), topic: versions, emit: versions_msdeposit_tools

    script:
    def prot = protocols ? '--protocols protocols' : ''
    """
    msdeposit-tools isa --study-json ${study_json} --plan ${plan} --metadata metadata --finalise finalise ${prot} \
        --controlled-value-profile ${profile} --study-category ${category} --timestamp-policy ${timestamp_policy} \
        --outdir isa
    """

    stub:
    """
    mkdir isa
    touch isa/i_Investigation.txt
    """
}
