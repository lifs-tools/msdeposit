process RAW_PACKAGE {
    tag "${meta.raw_id}"
    label 'process_low'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:0.1.0'

    input:
    tuple val(meta), path(raw, stageAs: 'raw/*'), path(companions, stageAs: 'companions/*'), path(plan)

    output:
    tuple val(meta), path("package/*", arity: '1..*'), emit: files
    tuple val("${task.process}"), val('msdeposit-tools'), eval('msdeposit-tools --version | cut -d" " -f2'), topic: versions, emit: versions_msdeposit_tools

    script:
    def comp = companions ? "--companions ${companions.join(' ')}" : ''
    """
    msdeposit-tools raw-package --raw ${raw} ${comp} --plan ${plan} --raw-id ${meta.raw_id} --outdir package
    """

    stub:
    """
    mkdir package
    touch package/${meta.raw_id}.raw_package.json
    """
}
