process CHECK {
    tag "${study}"
    label 'process_low'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:0.1.0'

    input:
    tuple val(study), path(upload_set), path(plan)
    val mztab_versions
    val fail_on_validation_errors
    val fail_on_todo_placeholders

    output:
    tuple val(study), path("checks/check_*.json"), emit: checks
    tuple val("${task.process}"), val('msdeposit-tools'), eval('msdeposit-tools --version | cut -d" " -f2'), topic: versions, emit: versions_msdeposit_tools

    script:
    // C7 needs network: the mzTab-M 2.0 semantic pass looks up CV terms on OLS (design §10)
    """
    msdeposit-tools check --upload-set ${upload_set} --study ${study} --plan ${plan} --checks C6,C7,C8,C9 \
        --mztab-versions ${mztab_versions} --fail-on-validation-errors ${fail_on_validation_errors} \
        --fail-on-todo-placeholders ${fail_on_todo_placeholders} --outdir checks
    """

    stub:
    """
    mkdir checks
    touch checks/check_C6.json
    """
}
