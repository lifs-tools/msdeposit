process GATE {
    tag "${study}"
    label 'process_single'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:dev'

    input:
    tuple val(study), path(checks, stageAs: 'checks/??/*')
    val expected
    val mode

    output:
    tuple val(study), path("gate.json"), emit: json
    tuple val(study), env('GATE_STATUS'), emit: status
    tuple val("${task.process}"), val('msdeposit-tools'), eval('msdeposit-tools --version | cut -d" " -f2'), topic: versions, emit: versions_msdeposit_tools

    script:
    """
    msdeposit-tools gate --study ${study} --checks checks --expected ${expected} --mode ${mode} --out gate.json
    GATE_STATUS=\$(python -c "import json; print(json.load(open('gate.json'))['status'])")
    """

    stub:
    """
    echo '{"status": "passed"}' > gate.json
    GATE_STATUS=passed
    """
}
