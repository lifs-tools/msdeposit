process MAF {
    tag "${study}"
    label 'process_single'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:dev'

    input:
    tuple val(study), path(study_json), path(plan), path(metadata, stageAs: 'metadata/??/*'), path(annotations)

    output:
    tuple val(study), path("maf/m_*_maf.tsv")  , emit: maf
    tuple val(study), path("maf/maf_todo.tsv")  , emit: todo, optional: true
    tuple val("${task.process}"), val('msdeposit-tools'), eval('msdeposit-tools --version | cut -d" " -f2'), topic: versions, emit: versions_msdeposit_tools

    script:
    def ann = annotations ? "--annotations ${annotations}" : ''
    """
    msdeposit-tools maf --study-json ${study_json} --plan ${plan} --metadata metadata ${ann} --outdir maf
    """

    stub:
    """
    mkdir maf
    touch maf/m_${study}_maf.tsv
    """
}
