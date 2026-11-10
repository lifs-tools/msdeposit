process TEXT_TO_MZML {
    tag "${meta.id}"
    label 'process_low'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:0.1.0'

    input:
    tuple val(meta), path(text)

    output:
    tuple val(meta), path("${meta.conv}.mzML"), emit: mzml
    tuple val("${task.process}"), val('msdeposit-tools'), eval('msdeposit-tools --version | cut -d" " -f2'), topic: versions, emit: versions_msdeposit_tools

    script:
    def args = task.ext.args ?: ''
    def polarity = meta.polarity in ['positive', 'negative'] ? "--polarity ${meta.polarity}" : ''
    """
    msdeposit-tools text-to-mzml --text ${text} --out ${meta.conv}.mzML \\
        --run-id ${meta.raw_id} --vendor ${meta.vendor} ${polarity} ${args}
    """

    stub:
    """
    touch ${meta.conv}.mzML
    """
}
