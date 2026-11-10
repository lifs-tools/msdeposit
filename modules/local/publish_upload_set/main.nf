process PUBLISH_UPLOAD_SET {
    tag "${study}"
    label 'process_single'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:0.1.0'

    // The only public output of the pipeline: the upload set of a study that passed the gate (design §9).
    // Everything else is published below private_outdir by the workflow output block in main.nf.
    publishDir "${params.outdir}", mode: params.publish_dir_mode

    input:
    tuple val(study), path(upload_set, stageAs: 'input/*')

    output:
    tuple val(study), path("${study}"), emit: upload_set

    script:
    // hard links to the finalised files (no second copy on the same filesystem)
    """
    cp -rlL ${upload_set} ${study} 2>/dev/null || cp -rL ${upload_set} ${study}
    """

    stub:
    """
    mkdir ${study}
    """
}
