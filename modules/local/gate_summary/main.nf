process GATE_SUMMARY {
    tag "all studies"
    label 'process_single'
    label 'msdeposit_tools'
    container 'ghcr.io/lifs-tools/msdeposit-tools:0.1.0'

    input:
    val lines   // ["<study>\t<passed|blocked>", ...]

    output:
    path "gate_summary.tsv", emit: tsv

    script:
    def body = lines.sort().join('\n')
    """
    printf 'study\tstatus\n${body}\n' > gate_summary.tsv
    cat gate_summary.tsv
    if grep -q 'blocked\$' gate_summary.tsv; then
        echo "One or more studies are blocked; they were not published. See gate.json and the private deposit report in private_outdir." >&2
        exit 3
    fi
    """

    stub:
    """
    printf 'study\tstatus\n' > gate_summary.tsv
    """
}
