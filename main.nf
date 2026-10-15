#!/usr/bin/env nextflow
/*
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    lifs-tools/msdeposit
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    Github : https://github.com/lifs-tools/msdeposit
----------------------------------------------------------------------------------------
*/

/*
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    IMPORT FUNCTIONS / MODULES / SUBWORKFLOWS / WORKFLOWS
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
*/

include { MSDEPOSIT  } from './workflows/msdeposit'
include { PIPELINE_INITIALISATION } from './subworkflows/local/utils_nfcore_msdeposit_pipeline'
include { PIPELINE_COMPLETION     } from './subworkflows/local/utils_nfcore_msdeposit_pipeline'
include { resolveMode             } from './subworkflows/local/utils_nfcore_msdeposit_pipeline'
include { inputSheets             } from './subworkflows/local/utils_nfcore_msdeposit_pipeline'
include { rawInputs               } from './subworkflows/local/utils_nfcore_msdeposit_pipeline'
include { GATE_SUMMARY            } from './modules/local/gate_summary'
/*
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    RUN MAIN WORKFLOW
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
*/

workflow {

    main:
    //
    // SUBWORKFLOW: Run initialisation tasks (help, parameter validation, mode preset checks)
    //
    PIPELINE_INITIALISATION (
        params.version,
        params.validate_params,
        params.monochrome_logs,
        args,
        params.outdir,
        params.input,
        params.help,
        params.help_full,
        params.show_hidden
    )

    //
    // WORKFLOW: Run main workflow
    //
    def cfg = resolveMode(params, workflow)
    MSDEPOSIT (
        inputSheets(params),
        rawInputs(params.input),
        cfg
    )
    // a blocked study makes the run fail at the end, after all other studies were processed (design §8);
    // the summary is stored directly so that it exists even then (read by the S6 inventory check)
    def ch_gate_lines = MSDEPOSIT.out.gate_status.map { s, status -> "${s}\t${status}" }
    ch_gate_lines.collectFile(name: 'gate_status.tsv', storeDir: cfg.private_outdir, newLine: true, sort: true)
    GATE_SUMMARY(ch_gate_lines.collect())

    //
    // SUBWORKFLOW: Run completion tasks
    //
    PIPELINE_COMPLETION (
        params.monochrome_logs,
        params.outdir,
        cfg
    )

    publish:
    upload_sets    = MSDEPOSIT.out.upload_sets
    blocked        = MSDEPOSIT.out.blocked
    gate           = MSDEPOSIT.out.gate
    report_private = MSDEPOSIT.out.report_private
    checks         = MSDEPOSIT.out.checks
    plan           = MSDEPOSIT.out.plan
    mapping        = MSDEPOSIT.out.mapping
    raw_ids        = MSDEPOSIT.out.raw_ids
    runs           = MSDEPOSIT.out.runs
    fixity         = MSDEPOSIT.out.fixity
    finalise       = MSDEPOSIT.out.finalise
    denylist       = MSDEPOSIT.out.denylist
    hits           = MSDEPOSIT.out.hits
}

/*
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    OUTPUTS: the only place that decides what is public (outdir) and what is private (design §9)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    Public: only the upload sets of studies that passed the gate, one directory per study.
    Everything else goes below private_outdir (default in standard mode: <outdir>/private).
*/

output {
    upload_sets    { path { t -> t[1] >> "${t[0]}" } }
    blocked        { path { t -> t[1] >> "${privateDir()}/blocked/${t[0]}" } }
    gate           { path { t -> t[1] >> "${privateDir()}/${t[0]}/gate.json" } }
    report_private { path { t -> t[1] >> "${privateDir()}/${t[0]}/deposit_report.private.html" } }
    checks         { path { t -> t[1].each { f -> f >> "${privateDir()}/${t[0]}/checks/${f.name}" } } }
    plan           { path { t -> t[1] >> "${privateDir()}/${t[0]}/plan.json" } }
    mapping        { path { t -> t[1] >> "${privateDir()}/${t[0]}/mapping.tsv" } }
    raw_ids        { path { f -> f >> "${privateDir()}/raw_ids.tsv" } }
    runs           { path { f -> f >> "${privateDir()}/runs.normalised.csv" } }
    fixity         { path { t -> t[1] >> "${privateDir()}/${t[0]}/fixity/${t[1].name}" } }
    finalise       { path { t -> t[2] >> "${privateDir()}/${t[0]}/finalise/${t[1]}.private.json" } }
    denylist       { path { t -> t[1] >> "${privateDir()}/${t[0]}/denylist.tsv" } }
    hits           { path { t -> t[1] >> "${privateDir()}/${t[0]}/hits.tsv" } }
}

def privateDir() {
    return params.private_outdir ? file(params.private_outdir).toAbsolutePath().normalize().toString()
                                 : file(params.outdir).toAbsolutePath().normalize().resolve('private').toString()
}

/*
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    THE END
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
*/
