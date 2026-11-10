process MZML_METADATA {
    tag "${meta.id}"
    label 'process_low'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:0.1.0'

    input:
    tuple val(meta), path(mzml)

    output:
    tuple val(meta), path("metadata.json")      , emit: json
    tuple val(meta), path("check_C2*.json")     , emit: checks
    tuple val("${task.process}"), val('msdeposit-tools'), eval('msdeposit-tools --version | cut -d" " -f2'), topic: versions, emit: versions_msdeposit_tools

    script:
    def polarity = meta.polarity ? "--expected-polarity ${meta.polarity}" : ''
    """
    msdeposit-tools mzml-metadata --mzml ${mzml} --run '${meta.run}' --study '${meta.study}' ${polarity} \\
        --out metadata.json --check-dir .
    """

    stub:
    """
    touch metadata.json check_C2.${meta.run}.json
    """
}
