process MZTAB {
    tag "${study}"
    label 'process_single'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:0.1.0'

    input:
    tuple val(study), path(study_json), path(plan), path(metadata, stageAs: 'metadata/??/*'), path(finalise, stageAs: 'finalise/??/*'), path(annotations)
    val versions

    output:
    tuple val(study), path("mztab/*.mzTab"), emit: mztab, optional: true   // none without runs of known polarity (C7 blocks)
    tuple val("${task.process}"), val('msdeposit-tools'), eval('msdeposit-tools --version | cut -d" " -f2'), topic: versions, emit: versions_msdeposit_tools

    script:
    def ann = annotations ? "--annotations ${annotations}" : ''
    """
    msdeposit-tools mztab --study-json ${study_json} --plan ${plan} --metadata metadata --finalise finalise ${ann} \
        --versions ${versions} --outdir mztab
    """

    stub:
    """
    mkdir mztab
    touch mztab/${study}.mztab-2.0.0-M.mzTab
    """
}
