process IDENTIFIER_DENYLIST {
    tag "${study}"
    label 'process_single'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:0.1.0'

    input:
    tuple val(study), path(candidates, stageAs: 'candidates/*'), path(metadata, stageAs: 'metadata/??/*')
    path runs_normalised
    path user_denylist, stageAs: 'user/*'    // staged in sub-directories: the output name must never hit an input
    path allowlist    , stageAs: 'allow/*'
    val base_dir

    output:
    tuple val(study), path("out/denylist.tsv"), emit: denylist
    tuple val("${task.process}"), val('msdeposit-tools'), eval('msdeposit-tools --version | cut -d" " -f2'), topic: versions, emit: versions_msdeposit_tools

    script:
    // ext.args: e.g. '--frequency-filter zipf=3.3' (off by default, design §6)
    def args = task.ext.args ?: ''
    def cand = candidates ? '--candidates candidates' : ''
    def meta_ = metadata ? '--metadata metadata' : ''
    def user = user_denylist ? "--user-denylist ${user_denylist}" : ''
    def allow = allowlist ? "--allowlist ${allowlist}" : ''
    """
    msdeposit-tools identifier-denylist --study ${study} ${cand} ${meta_} --run-keys ${runs_normalised} --base-dir '${base_dir}' \
        ${user} ${allow} --out out/denylist.tsv ${args}
    """

    stub:
    """
    mkdir out
    touch out/denylist.tsv
    """
}
