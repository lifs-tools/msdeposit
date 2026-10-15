process MZML_FINALISE {
    tag "${meta.id}"
    label 'process_low'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:dev'

    input:
    tuple val(meta), path(mzml, stageAs: 'input/*'), path(plan), path(denylist)
    val mode
    val timestamp_policy
    val body_rewrite

    output:
    tuple val(meta), path("final/*.mzML")                , emit: mzml, optional: true   // absent when C4 failed
    tuple val(meta), path("final/finalise.json")         , emit: json
    tuple val(meta), path("final/finalise.private.json") , emit: private_json
    tuple val(meta), path("final/check_C4*.json")        , emit: checks
    tuple val("${task.process}"), val('msdeposit-tools'), eval('msdeposit-tools --version | cut -d" " -f2'), topic: versions, emit: versions_msdeposit_tools

    script:
    def deny = denylist ? "--denylist ${denylist} --body-rewrite ${body_rewrite}" : ''
    """
    msdeposit-tools mzml-finalise --mzml ${mzml} --plan ${plan} --run '${meta.run}' --mode ${mode} \\
        --timestamp-policy ${timestamp_policy} ${deny} --outdir final
    """

    stub:
    """
    mkdir final
    touch final/${meta.run}.mzML final/finalise.json final/finalise.private.json final/check_C4.${meta.run}.json
    """
}
