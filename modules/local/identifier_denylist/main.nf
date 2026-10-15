process IDENTIFIER_DENYLIST {
    tag "${study}"
    label 'process_single'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:dev'

    input:
    tuple val(study), path(candidates, stageAs: 'candidates/*'), path(metadata, stageAs: 'metadata/??/*')
    path runs_normalised
    path user_denylist
    path allowlist
    val base_dir

    output:
    tuple val(study), path("denylist.tsv"), emit: denylist
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
        ${user} ${allow} --out denylist.tsv ${args}
    """

    stub:
    """
    touch denylist.tsv
    """
}
