process MZML_INDEX {
    tag "${meta.id}"
    label 'process_low'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:dev'

    input:
    tuple val(meta), path(mzml, stageAs: 'input/*')

    output:
    tuple val(meta), path("${meta.conv}.mzML"), emit: mzml
    tuple val("${task.process}"), val('msdeposit-tools'), eval('msdeposit-tools --version | cut -d" " -f2'), topic: versions, emit: versions_msdeposit_tools

    script:
    """
    msdeposit-tools mzml-index --mzml ${mzml} --out ${meta.conv}.mzML
    """

    stub:
    """
    touch ${meta.conv}.mzML
    """
}
