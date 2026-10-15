//
// Subworkflow with functionality specific to the lifs-tools/msdeposit pipeline
//

/*
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    IMPORT FUNCTIONS / MODULES / SUBWORKFLOWS
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
*/

include { UTILS_NFSCHEMA_PLUGIN     } from '../../nf-core/utils_nfschema_plugin'
include { paramsSummaryMap          } from 'plugin/nf-schema'
include { samplesheetToList         } from 'plugin/nf-schema'
include { paramsHelp                } from 'plugin/nf-schema'
include { completionSummary         } from '../../nf-core/utils_nfcore_pipeline'
include { UTILS_NFCORE_PIPELINE     } from '../../nf-core/utils_nfcore_pipeline'
include { UTILS_NEXTFLOW_PIPELINE   } from '../../nf-core/utils_nextflow_pipeline'

/*
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    SUBWORKFLOW TO INITIALISE PIPELINE
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
*/

workflow PIPELINE_INITIALISATION {

    take:
    version           // boolean: Display version and exit
    validate_params   // boolean: Boolean whether to validate parameters against the schema at runtime
    monochrome_logs   // boolean: Do not use coloured log outputs
    nextflow_cli_args //   array: List of positional nextflow CLI args
    _outdir           //  string: unused (pipeline_info goes to infoParent(): private in sensitive mode)
    input             //  string: Path to input samplesheet
    help              // boolean: Display help message and exit
    help_full         // boolean: Show the full help message
    show_hidden       // boolean: Show hidden parameters in the help message

    main:

    ch_versions = channel.empty()

    //
    // Print version and exit if required and dump pipeline parameters to JSON file
    // (to private_outdir/pipeline_info in sensitive mode: parameter values contain paths)
    //
    UTILS_NEXTFLOW_PIPELINE (
        version,
        true,
        infoParent(),
        workflow.profile.tokenize(',').intersect(['conda', 'mamba']).size() >= 1
    )

    //
    // Validate parameters and generate parameter summary to stdout
    //
    def before_text = ""
    def after_text = ""
    if (monochrome_logs) {
        before_text = before_text.replaceAll(/\033\[[0-9;]*m/, '')
    }
    command = "nextflow run ${workflow.manifest.name} -profile docker --input runs.csv --assays assays.csv --samples samples.csv --studies studies.yml --outdir <OUTDIR>"
    UTILS_NFSCHEMA_PLUGIN (
        workflow,
        validate_params,
        null,
        help,
        help_full,
        show_hidden,
        before_text,
        after_text,
        command,
        false
    )

    //
    // Check config provided to the pipeline
    //
    UTILS_NFCORE_PIPELINE (
        nextflow_cli_args
    )

    //
    // Mode preset, leak-boundary rules and the private-launch guard (design §5, §9): fail before any task runs
    //
    def cfg = resolveMode(params, workflow)
    checkVendorLicenses(samplesheetToList(input, "${projectDir}/assets/schema_input.json"), cfg)

    emit:
    versions   = ch_versions
}

/*
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    SUBWORKFLOW FOR PIPELINE COMPLETION
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
*/

workflow PIPELINE_COMPLETION {

    take:
    monochrome_logs // boolean: Disable ANSI colour codes in log output
    outdir          //  string: public output directory
    cfg             //     map: resolved mode switches

    main:
    //
    // Completion summary; in sensitive mode also the outdir inventory check (S6, design §8.2)
    //
    workflow.onComplete {
        completionSummary(monochrome_logs)
        if (cfg.sensitive) {
            def problems = outdirInventory(outdir, cfg.private_outdir)
            if (problems) {
                log.error("S6 outdir inventory check FAILED: ${problems.size()} unexpected entries in the public outdir " +
                          "(only upload sets of passed studies may be there): " + problems.join(', '))
            } else {
                log.info("S6 outdir inventory check passed")
            }
        }
    }

    workflow.onError {
        log.error "Pipeline failed. Please refer to troubleshooting docs for common issues: https://nf-co.re/docs/running/troubleshooting"
    }
}

/*
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    FUNCTIONS
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
*/

//
// S6 (sensitive): the public outdir may only contain the upload sets of studies that passed the gate.
// Returns the names of unexpected entries (study keys and file names only, no paths).
//
def outdirInventory(outdir, private_outdir) {
    def summary = file("${private_outdir}/gate_status.tsv")
    def passed = summary.exists() ? summary.readLines().findAll { l -> l.endsWith('\tpassed') }.collect { l -> l.tokenize('\t')[0] } : []
    def root = file(outdir)
    if (!root.exists()) {
        return []
    }
    return root.listFiles().findAll { f -> !(f.isDirectory() && f.name in passed) }.collect { f -> f.name }.sort()
}

//
// Paths of the run sheet (vendor files, directories, companions); staged into VALIDATE_INPUTS only so that the
// container can see them under their original paths
//
def rawInputs(input) {
    def base = file(input).toAbsolutePath().parent
    def resolve = { p -> p.startsWith('/') ? file(p) : file(base.resolve(p).normalize().toString()) }
    return samplesheetToList(input, "${projectDir}/assets/schema_input.json").collectMany { row ->
        def r = row[0]
        [resolve.call(r.path)] + (r.companions ? r.companions.tokenize(';').collect { c -> resolve.call(c.trim()) } : [])
    }.unique()
}

//
// Input sheets as staged process inputs ([] when not given)
//
def inputSheets(params) {
    def names = ['input', 'assays', 'samples', 'studies', 'contacts', 'publication', 'features', 'values',
                 'study_variables', 'study_variable_members', 'transitions', 'denylist', 'allowlist']
    return names.collectEntries { name -> [name, params[name] ? file(params[name], checkIfExists: true) : []] }
}

//
// Directory that receives pipeline_info: private_outdir in sensitive mode (design §9)
//
def infoParent() {
    return params.sensitive_data ? absolute(params.private_outdir) : params.outdir
}

def absolute(path) {
    return path ? file(path).toAbsolutePath().normalize().toString() : null
}

def isWithin(child, parent) {
    def c = file(child).toAbsolutePath().normalize()
    def p = file(parent).toAbsolutePath().normalize()
    return c == p || c.startsWith(p)
}

//
// Resolve the mode preset into explicit switches; reject contradicting overrides instead of forcing them (design §5)
//
def resolveMode(params, workflow) {
    def sensitive = params.sensitive_data as boolean
    def errors = []
    def required = { name, value ->
        if (params[name] != null && params[name] != value) {
            errors << "--${name} ${params[name]} contradicts --sensitive_data (requires ${value})"
        }
        value
    }
    def pick = { name, standard_default -> params[name] != null ? params[name] : standard_default }
    def cfg = [sensitive: sensitive]
    if (sensitive) {
        cfg.deposit_raw = required.call('deposit_raw', false)
        cfg.finalise_mode = required.call('finalise_mode', 'anonymise')
        cfg.deposited_naming = required.call('deposited_naming', 'neutral')
        cfg.identifier_scan = required.call('identifier_scan', true)
        cfg.enforce_private_launch = pick.call('enforce_private_launch', true)
        if (!params.private_outdir) {
            errors << "--sensitive_data requires --private_outdir"
        }
    } else {
        cfg.deposit_raw = pick.call('deposit_raw', true)
        cfg.finalise_mode = pick.call('finalise_mode', 'rename')
        cfg.deposited_naming = pick.call('deposited_naming', 'original')
        cfg.identifier_scan = pick.call('identifier_scan', false)
        cfg.enforce_private_launch = pick.call('enforce_private_launch', false)
    }
    def outdir = absolute(params.outdir)
    cfg.private_outdir = absolute(params.private_outdir) ?: "${outdir}/private"
    if (params.private_outdir && (isWithin(params.private_outdir, params.outdir) || isWithin(params.outdir, params.private_outdir))) {
        errors << "--private_outdir and --outdir must be different and not nested in each other"
    }
    if (cfg.enforce_private_launch && params.private_outdir) {
        // Nextflow writes .nextflow.log, .nextflow/ (history with command lines) and work/ below the launch directory
        if (!isWithin(workflow.launchDir, params.private_outdir) || !isWithin(workflow.workDir, params.private_outdir)) {
            errors << ("sensitive mode: launch Nextflow from a directory inside --private_outdir and keep the work " +
                       "directory there (cd <private_outdir>/run && nextflow run ... -w <private_outdir>/work), or set " +
                       "--enforce_private_launch false at your own risk (design §9)")
        }
    }
    if (errors) {
        error("Invalid mode settings:\n  - " + errors.join("\n  - "))
    }
    cfg.info_dir = "${infoParent()}/pipeline_info"
    cfg.mzml_body_rewrite = params.mzml_body_rewrite
    cfg.deposit_report = params.deposit_report
    cfg.timestamp_policy = params.timestamp_policy
    cfg.thermo_converter = params.thermo_converter
    cfg.mztab_versions = params.mztab_versions
    cfg.maf = params.maf
    cfg.lipid_annotation = params.lipid_annotation
    cfg.swisslipids_cache = params.swisslipids_cache ? file(params.swisslipids_cache) : []
    cfg.allow_network = params.allow_network
    cfg.protocols = params.protocols ? file(params.protocols, checkIfExists: true) : []
    cfg.controlled_value_profile = params.controlled_value_profile
    cfg.study_category = params.mtbls_study_category
    cfg.fail_on_validation_errors = params.fail_on_validation_errors
    cfg.fail_on_todo_placeholders = params.fail_on_todo_placeholders
    // parameters shown in the deposit report: names and non-path values only (design §9.6)
    cfg.report_params = [
        sensitive_data: sensitive, deposit_raw: cfg.deposit_raw, finalise_mode: cfg.finalise_mode,
        deposited_naming: cfg.deposited_naming, identifier_scan: cfg.identifier_scan,
        mzml_body_rewrite: cfg.mzml_body_rewrite, timestamp_policy: cfg.timestamp_policy,
        mztab_versions: cfg.mztab_versions, maf: cfg.maf, lipid_annotation: cfg.lipid_annotation,
        thermo_converter: cfg.thermo_converter, mtbls_study_category: cfg.study_category,
        controlled_value_profile: cfg.controlled_value_profile,
        fail_on_validation_errors: cfg.fail_on_validation_errors, fail_on_todo_placeholders: cfg.fail_on_todo_placeholders,
    ]
    return cfg
}

//
// The ProteoWizard image bundles vendor libraries: their licences must be accepted explicitly (design §10)
//
def checkVendorLicenses(rows, cfg) {
    def needs_pwiz = rows.any { row ->
        def r = row[0]
        def vendor = r.vendor ?: (r.path.toLowerCase().endsWith('.raw') ? 'thermo-or-waters' : 'other')
        r.format == 'vendor_raw' && !(vendor == 'thermo' && cfg.thermo_converter == 'thermorawfileparser')
    }
    if (needs_pwiz && !params.accept_vendor_licenses) {
        error("The run sheet contains vendor files converted with the ProteoWizard vendor-reader image. " +
              "Its use requires accepting the vendor licences: add --accept_vendor_licenses " +
              "(see https://proteowizard.sourceforge.io/licenses.html)")
    }
}

//
// Generate methods description for MultiQC
//
def toolCitationText() {
    // TODO nf-core: Optionally add in-text citation tools to this list.
    // Can use ternary operators to dynamically construct based conditions, e.g. params["run_xyz"] ? "Tool (Foo et al. 2023)" : "",
    // Uncomment function in methodsDescriptionText to render in MultiQC report
    def citation_text = [
            "Tools used in the workflow included:",
            "."
        ].join(' ').trim()

    return citation_text
}

def toolBibliographyText() {
    // TODO nf-core: Optionally add bibliographic entries to this list.
    // Can use ternary operators to dynamically construct based conditions, e.g. params["run_xyz"] ? "<li>Author (2023) Pub name, Journal, DOI</li>" : "",
    // Uncomment function in methodsDescriptionText to render in MultiQC report
    def reference_text = [
        ].join(' ').trim()

    return reference_text
}

def methodsDescriptionText(mqc_methods_yaml) {
    // Convert  to a named map so can be used as with familiar NXF ${workflow} variable syntax in the MultiQC YML file
    def meta = [:]
    meta.workflow = workflow.toMap()
    meta["manifest_map"] = workflow.manifest.toMap()

    // Pipeline DOI
    if (meta.manifest_map.doi) {
        // Using a loop to handle multiple DOIs
        // Removing `https://doi.org/` to handle pipelines using DOIs vs DOI resolvers
        // Removing ` ` since the manifest.doi is a string and not a proper list
        def temp_doi_ref = ""
        def manifest_doi = meta.manifest_map.doi.tokenize(",")
        manifest_doi.each { doi_ref ->
            temp_doi_ref += "(doi: <a href=\'https://doi.org/${doi_ref.replace("https://doi.org/", "").replace(" ", "")}\'>${doi_ref.replace("https://doi.org/", "").replace(" ", "")}</a>), "
        }
        meta["doi_text"] = temp_doi_ref.substring(0, temp_doi_ref.length() - 2)
    } else meta["doi_text"] = ""
    meta["nodoi_text"] = meta.manifest_map.doi ? "" : "<li>If available, make sure to update the text to include the Zenodo DOI of version of the pipeline used. </li>"

    // Tool references
    meta["tool_citations"] = ""
    meta["tool_bibliography"] = ""

    // TODO nf-core: Only uncomment below if logic in toolCitationText/toolBibliographyText has been filled!
    // meta["tool_citations"] = toolCitationText().replaceAll(", \\.", ".").replaceAll("\\. \\.", ".").replaceAll(", \\.", ".")
    // meta["tool_bibliography"] = toolBibliographyText()


    def methods_text = mqc_methods_yaml.text

    def engine =  new groovy.text.SimpleTemplateEngine()
    def description_html = engine.createTemplate(methods_text).make(meta)

    return description_html.toString()
}
