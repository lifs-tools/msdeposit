process LIPID_ANNOTATE {
    tag "${study}"
    label 'process_single'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:0.1.0'

    input:
    tuple val(study), path(study_json)
    val mode
    path cache
    val allow_network

    output:
    tuple val(study), path("lipid_annotation.tsv"), emit: tsv
    tuple val("${task.process}"), val('msdeposit-tools'), eval('msdeposit-tools --version | cut -d" " -f2'), topic: versions, emit: versions_msdeposit_tools

    script:
    def c = cache ? "--cache ${cache}" : ''
    """
    msdeposit-tools lipid-annotate --study-json ${study_json} --mode ${mode} ${c} --allow-network ${allow_network} \
        --out lipid_annotation.tsv
    """

    stub:
    """
    touch lipid_annotation.tsv
    """
}
