process RAW_FIXITY {
    tag "${meta.raw_id}"
    label 'process_low'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:dev'

    input:
    tuple val(meta), path(raw, stageAs: 'raw/*'), path(companions, stageAs: 'companions/*')

    output:
    tuple val(meta), path("${meta.raw_id}.fixity.tsv"), emit: fixity
    tuple val("${task.process}"), val('msdeposit-tools'), eval('msdeposit-tools --version | cut -d" " -f2'), topic: versions, emit: versions_msdeposit_tools

    script:
    def comp = companions ? "--companions ${companions.join(' ')}" : ''
    """
    msdeposit-tools raw-fixity --raw ${raw} ${comp} --raw-id ${meta.raw_id} --study ${meta.study} \\
        --jobs ${task.cpus} --out ${meta.raw_id}.fixity.tsv
    """

    stub:
    """
    touch ${meta.raw_id}.fixity.tsv
    """
}
