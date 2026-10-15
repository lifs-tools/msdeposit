process VALIDATE_INPUTS {
    tag "inputs"
    label 'process_single'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:dev'

    input:
    path input                 , stageAs: 'sheets/input/*'
    path assays                , stageAs: 'sheets/assays/*'
    path samples               , stageAs: 'sheets/samples/*'
    path studies               , stageAs: 'sheets/studies/*'
    path contacts              , stageAs: 'sheets/contacts/*'
    path publication           , stageAs: 'sheets/publication/*'
    path features              , stageAs: 'sheets/features/*'
    path values                , stageAs: 'sheets/values/*'
    path study_variables       , stageAs: 'sheets/study_variables/*'
    path study_variable_members, stageAs: 'sheets/study_variable_members/*'
    path transitions           , stageAs: 'sheets/transitions/*'
    path denylist              , stageAs: 'sheets/denylist/*'
    path raw_inputs            , stageAs: 'raw_inputs/??/*'   // staged only so that the container can see the run sheet paths
    val mode
    val base_dir

    output:
    path "validated/runs.normalised.csv", emit: runs
    path "validated/raw_ids.tsv"        , emit: raw_ids
    path "validated/*/study.json"       , emit: study_json
    path "validated/*/check_C1*.json"   , emit: checks
    tuple val("${task.process}"), val('msdeposit-tools'), eval('msdeposit-tools --version | cut -d" " -f2'), topic: versions, emit: versions_msdeposit_tools

    script:
    def args = task.ext.args ?: ''
    def optional = [contacts: contacts, publication: publication, features: features, values: values,
                    'study-variables': study_variables, 'study-variable-members': study_variable_members,
                    transitions: transitions, denylist: denylist]
        .findAll { _option, file -> file }
        .collect { option, file -> "--${option} ${file}" }
        .join(' ')
    """
    msdeposit-tools validate-inputs \\
        --input ${input} --assays ${assays} --samples ${samples} --studies ${studies} \\
        ${optional} \\
        --mode ${mode} \\
        --base-dir '${base_dir}' \\
        --outdir validated \\
        ${args}
    """

    stub:
    """
    mkdir -p validated/S1
    touch validated/runs.normalised.csv validated/raw_ids.tsv validated/S1/study.json validated/S1/check_C1.json
    """
}
