process DEPOSIT_REPORT {
    tag "${study}"
    label 'process_single'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:dev'

    input:
    tuple val(study), path(plan), path(checks, stageAs: 'checks/??/*'), path(manifest), path(gate), path(mapping), path(hits)
    val mode
    val report_params

    output:
    tuple val(study), path("deposit_report*.html"), emit: html
    tuple val("${task.process}"), val('msdeposit-tools'), eval('msdeposit-tools --version | cut -d" " -f2'), topic: versions, emit: versions_msdeposit_tools

    script:
    // ext.args: '--public deposit_report.html' or '--private deposit_report.private.html' (conf/modules.config)
    def args = task.ext.args ?: ''
    def g = gate ? "--gate ${gate}" : ''
    def mp = mapping ? "--mapping ${mapping}" : ''
    def h = hits ? "--hits ${hits}" : ''
    def json = groovy.json.JsonOutput.toJson(report_params)
    """
    cat > report_params.json <<'PARAMS'
${json}
PARAMS
    versions=""
    if [ -f "\${MSDEPOSIT_TOOLS_DIR:-/opt/msdeposit/tools}/tools.versions.yml" ]; then
        versions="--versions \${MSDEPOSIT_TOOLS_DIR:-/opt/msdeposit/tools}/tools.versions.yml"
    fi
    msdeposit-tools deposit-report --study ${study} --plan ${plan} ${g} --checks checks --upload-manifest ${manifest} \
        ${mp} ${h} \${versions} --params report_params.json --mode ${mode} ${args}
    """

    stub:
    """
    touch deposit_report.html
    """
}
