process VENDOR_METADATA {
    tag "${meta.raw_id}"
    label 'process_low'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:0.1.0'

    input:
    tuple val(meta), path(raw, stageAs: 'raw/*')

    output:
    tuple val(meta), path("vendor_metadata/${meta.raw_id}"), emit: dir
    tuple val("${task.process}"), val('msdeposit-tools'), eval('msdeposit-tools --version | cut -d" " -f2'), topic: versions, emit: versions_msdeposit_tools

    script:
    """
    msdeposit-tools vendor-metadata --raw ${raw} --vendor ${meta.vendor} --raw-id ${meta.raw_id} --outdir vendor_metadata
    """

    stub:
    """
    mkdir -p vendor_metadata/${meta.raw_id}
    touch vendor_metadata/${meta.raw_id}/candidates.tsv
    """
}
