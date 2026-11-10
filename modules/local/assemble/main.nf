process ASSEMBLE {
    tag "${study}"
    label 'process_low'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:0.1.0'

    input:
    tuple val(study), path(plan), path(isa), path(maf, stageAs: 'maf/*'), path(mztab, stageAs: 'mztab/*'), path(mzqc, stageAs: 'mzqc/*'), path(mzml, stageAs: 'mzml/??/*'), path(raw, stageAs: 'raw/??/*')

    output:
    tuple val(study), path("assembled/${study}")         , emit: upload_set
    tuple val(study), path("assembled/check_C5*.json")    , emit: checks
    tuple val("${task.process}"), val('msdeposit-tools'), eval('msdeposit-tools --version | cut -d" " -f2'), topic: versions, emit: versions_msdeposit_tools

    script:
    def r = raw ? '--raw raw' : ''
    def m = maf ? '--maf maf' : ''
    def t = mztab ? '--mztab mztab' : ''
    """
    msdeposit-tools assemble --plan ${plan} --isa ${isa} ${m} ${t} --mzqc mzqc --mzml mzml ${r} \
        --jobs ${task.cpus} --outdir assembled
    """

    stub:
    """
    mkdir -p assembled/${study}
    touch assembled/check_C5.json
    """
}
