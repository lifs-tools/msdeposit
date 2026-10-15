/*
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    MSDEPOSIT: inputs → converted, finalised and described upload set per study → checks → gate (design §7)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    All study-level channels are keyed by the study key; run-level channels carry `meta` with
    [id, study, run, assay, sample, format, vendor, raw_id, run_index, polarity, conv].
*/

include { softwareVersionsToYAML } from '../subworkflows/nf-core/utils_nfcore_pipeline'
include { CONVERT                } from '../subworkflows/local/convert'
include { VALIDATE_INPUTS        } from '../modules/local/validate_inputs'
include { RAW_FIXITY             } from '../modules/local/raw_fixity'
include { MZML_METADATA          } from '../modules/local/mzml_metadata'
include { VENDOR_METADATA        } from '../modules/local/vendor_metadata'
include { IDENTIFIER_DENYLIST    } from '../modules/local/identifier_denylist'
include { DEPOSIT_PLAN           } from '../modules/local/deposit_plan'
include { MZML_FINALISE          } from '../modules/local/mzml_finalise'
include { RAW_PACKAGE            } from '../modules/local/raw_package'
include { ISA                    } from '../modules/local/isa'
include { LIPID_ANNOTATE         } from '../modules/local/lipid_annotate'
include { MAF                    } from '../modules/local/maf'
include { MZTAB                  } from '../modules/local/mztab'
include { MZQC                   } from '../modules/local/mzqc'
include { ASSEMBLE               } from '../modules/local/assemble'
include { CHECK                  } from '../modules/local/check'
include { DEPOSIT_REPORT as DEPOSIT_REPORT_PUBLIC  } from '../modules/local/deposit_report'
include { DEPOSIT_REPORT as DEPOSIT_REPORT_PRIVATE } from '../modules/local/deposit_report'
include { ASSEMBLE_REPORT        } from '../modules/local/assemble_report'
include { CHECK_SENSITIVE        } from '../modules/local/check_sensitive'
include { IDENTIFIER_SCAN        } from '../modules/local/identifier_scan'
include { GATE                   } from '../modules/local/gate'

workflow MSDEPOSIT {
    take:
    sheets      // map of input files (path or []) by parameter name
    raw_inputs  // value: list of all raw/companion paths of the run sheet (staged so the container sees them)
    cfg         // map: resolved mode switches (see resolveMode in utils_nfcore_msdeposit_pipeline)

    main:
    def mode = cfg.sensitive ? 'sensitive' : 'standard'
    def base_dir = sheets.input.toAbsolutePath().parent.toString()   // relative run sheet paths resolve against it

    //
    // PREPARE: validate the inputs (C1) and build the run channel from the normalised run sheet
    //
    VALIDATE_INPUTS(
        sheets.input, sheets.assays, sheets.samples, sheets.studies, sheets.contacts, sheets.publication,
        sheets.features, sheets.values, sheets.study_variables, sheets.study_variable_members, sheets.transitions,
        sheets.denylist, raw_inputs, mode, base_dir
    )
    def ch_runs_csv = VALIDATE_INPUTS.out.runs
    def ch_runs = VALIDATE_INPUTS.out.runs
        .splitCsv(header: true)
        .map { row ->
            def meta = [
                id: "${row.study}/${row.run}", study: row.study, run: row.run, assay: row.assay, sample: row.sample,
                format: row.format, vendor: row.vendor, raw_id: row.raw_id, run_index: row.run_index ?: null,
                polarity: row.polarity ?: '', conv: row.raw_id + (row.run_index ? "_${row.run_index}" : '')
            ]
            def companions = row.companions ? row.companions.tokenize(';').collect { c -> file(c) } : []
            [meta, file(row.path), companions]
        }
    def ch_study_json = VALIDATE_INPUTS.out.study_json.flatten().map { f -> [f.parent.name, f] }
    def ch_checks_c1 = VALIDATE_INPUTS.out.checks.flatten().map { f -> [f.parent.name, f] }
    // runs per study, so that per-study groups are emitted as soon as they are complete (design §7)
    def ch_counts = ch_runs.map { meta, _p, _c -> [meta.study, meta.run] }.groupTuple().map { s, runs -> [s, runs.size()] }
    // one entry per raw path (multi-sample files have several runs)
    def ch_raw_unique = ch_runs
        .filter { meta, _p, _c -> meta.format == 'vendor_raw' }
        .map { meta, path, companions -> [[id: meta.raw_id, study: meta.study, raw_id: meta.raw_id, vendor: meta.vendor], path, companions] }
        .unique { item -> item[0] }

    RAW_FIXITY(ch_raw_unique)

    //
    // CONVERT and MZML_METADATA (C2)
    //
    CONVERT(ch_runs, cfg.thermo_converter)
    MZML_METADATA(CONVERT.out.mzml)
    def ch_metadata = groupByStudy(MZML_METADATA.out.json.map { meta, j -> [meta.study, j] }, ch_counts)

    //
    // SENSITIVE (optional): identifier candidates from the vendor metadata and the per-study denylist
    //
    def ch_denylist = ch_study_json.map { s, _j -> [s, []] }
    if (cfg.sensitive) {
        VENDOR_METADATA(ch_raw_unique.map { meta, path, _c -> [meta, path] })
        def ch_candidates = VENDOR_METADATA.out.dir.map { meta, d -> [meta.study, d] }.groupTuple()
        def ch_denylist_in = ch_study_json
            .join(ch_candidates, remainder: true)
            .map { s, _j, c -> [s, c ?: []] }
            .join(ch_metadata)
        IDENTIFIER_DENYLIST(ch_denylist_in, ch_runs_csv, sheets.denylist, sheets.allowlist, base_dir)
        ch_denylist = IDENTIFIER_DENYLIST.out.denylist
    }

    //
    // DEPOSIT_PLAN (C3); studies whose plan failed go straight to the gate
    //
    DEPOSIT_PLAN(ch_study_json.join(ch_metadata), ch_runs_csv, cfg.deposited_naming, cfg.deposit_raw)
    def ch_plan = DEPOSIT_PLAN.out.plan
        .branch { _s, plan ->
            ok: readJson(plan).status == 'pass'
            failed: true
        }
    def ch_plan_ok = ch_plan.ok

    //
    // MZML_FINALISE (C4): rename (standard) or anonymise (sensitive) every run
    //
    def ch_finalise_in = CONVERT.out.mzml
        .map { meta, mzml -> [meta.study, meta, mzml] }
        .combine(ch_plan_ok, by: 0)
        .combine(ch_denylist, by: 0)
        .map { _s, meta, mzml, plan, deny -> [meta, mzml, plan, deny] }
    MZML_FINALISE(ch_finalise_in, cfg.finalise_mode, cfg.timestamp_policy, cfg.mzml_body_rewrite)
    def ch_finalise = groupByStudy(MZML_FINALISE.out.json.map { meta, j -> [meta.study, j] }, ch_counts)
    def ch_mzml_final = groupByStudy(MZML_FINALISE.out.mzml.map { meta, f -> [meta.study, f] }, ch_counts)

    //
    // RAW_PACKAGE (standard mode with deposit_raw)
    //
    def ch_raw_files = ch_plan_ok.map { s, _p -> [s, []] }
    if (cfg.deposit_raw) {
        def ch_package_in = ch_raw_unique
            .map { meta, path, companions -> [meta.study, meta, path, companions] }
            .combine(ch_plan_ok, by: 0)
            .map { _s, meta, path, companions, plan -> [meta, path, companions, plan] }
        RAW_PACKAGE(ch_package_in)
        def ch_packaged = RAW_PACKAGE.out.files.map { meta, files -> [meta.study, files] }.groupTuple().map { s, files -> [s, files.flatten()] }
        ch_raw_files = ch_plan_ok.join(ch_packaged, remainder: true).map { s, _p, files -> [s, files ?: []] }
    }

    //
    // DESCRIBE: ISA-Tab, (lipid annotation), MAF, mzTab-M, mzQC
    //
    def ch_study_plan = ch_study_json.join(ch_plan_ok)
    ISA(ch_study_plan.join(ch_metadata).join(ch_finalise), cfg.protocols, cfg.controlled_value_profile,
        cfg.study_category, cfg.timestamp_policy)
    def ch_annotations = ch_study_plan.map { s, _j, _p -> [s, []] }
    if (cfg.lipid_annotation != 'none') {
        LIPID_ANNOTATE(ch_study_plan.map { s, j, _p -> [s, j] }, cfg.lipid_annotation, cfg.swisslipids_cache, cfg.allow_network)
        ch_annotations = LIPID_ANNOTATE.out.tsv
    }
    def ch_maf = ch_study_plan.map { s, _j, _p -> [s, []] }
    if (cfg.maf) {
        MAF(ch_study_plan.join(ch_metadata).join(ch_annotations))
        ch_maf = MAF.out.maf
    }
    MZTAB(ch_study_plan.join(ch_metadata).join(ch_finalise).join(ch_annotations), cfg.mztab_versions)
    MZQC(ch_study_plan.join(ch_metadata).join(ch_finalise))

    //
    // ASSEMBLE (C5) and CHECK (C6-C9) on the assembled upload set
    //
    ASSEMBLE(
        ch_plan_ok.join(ISA.out.dir).join(ch_maf).join(MZTAB.out.mztab).join(MZQC.out.mzqc)
            .join(ch_mzml_final).join(ch_raw_files)
    )
    CHECK(ASSEMBLE.out.upload_set.join(ch_plan_ok), cfg.mztab_versions, cfg.fail_on_validation_errors,
          cfg.fail_on_todo_placeholders)

    //
    // Public deposit report from C1-C9, added to the upload set before the sensitive checks (design §15.6)
    //
    def ch_checks_c = ch_checks_c1
        .mix(MZML_METADATA.out.checks.map { meta, f -> [meta.study, f] })
        .mix(DEPOSIT_PLAN.out.checks)
        .mix(MZML_FINALISE.out.checks.map { meta, f -> [meta.study, f] })
        .mix(ASSEMBLE.out.checks)
        .mix(CHECK.out.checks)
        .transpose()
        .groupTuple()
    def no_file = []
    DEPOSIT_REPORT_PUBLIC(
        ch_plan_ok.join(ch_checks_c)
            .join(ASSEMBLE.out.upload_set)
            .map { s, plan, checks, upload -> [s, plan, checks, upload.resolve('upload_manifest.tsv'), no_file, no_file, no_file] },
        mode, cfg.report_params
    )
    ASSEMBLE_REPORT(ch_plan_ok.join(ASSEMBLE.out.upload_set).join(DEPOSIT_REPORT_PUBLIC.out.html))
    def ch_upload_set = ASSEMBLE_REPORT.out.upload_set

    //
    // CHECK_SENSITIVE (S3-S5, S7) and IDENTIFIER_SCAN (S1 self-test, S2) on the final upload set
    //
    def ch_checks_s = channel.empty()
    def ch_hits = ch_plan_ok.map { s, _p -> [s, []] }
    if (cfg.sensitive) {
        CHECK_SENSITIVE(ch_upload_set.join(ch_plan_ok).join(ch_denylist), ch_runs_csv, cfg.private_outdir,
                        cfg.timestamp_policy)
        IDENTIFIER_SCAN(ch_upload_set.join(ch_denylist))
        ch_checks_s = CHECK_SENSITIVE.out.checks.mix(IDENTIFIER_SCAN.out.checks)
        ch_hits = IDENTIFIER_SCAN.out.hits
    }

    //
    // GATE per study; blocked studies are never published to outdir
    //
    def ch_all_checks = ch_checks_c1
        .mix(MZML_METADATA.out.checks.map { meta, f -> [meta.study, f] })
        .mix(DEPOSIT_PLAN.out.checks)
        .mix(MZML_FINALISE.out.checks.map { meta, f -> [meta.study, f] })
        .mix(ASSEMBLE.out.checks)
        .mix(CHECK.out.checks)
        .mix(ASSEMBLE_REPORT.out.checks)
        .mix(ch_checks_s)
        .transpose()
        .groupTuple()
    GATE(ch_all_checks, 'C1,C2,C3,C4,C5,C6,C7,C8,C9', mode)

    DEPOSIT_REPORT_PRIVATE(
        DEPOSIT_PLAN.out.plan.join(ch_all_checks)
            .join(ASSEMBLE.out.upload_set, remainder: true)
            .join(GATE.out.json)
            .join(DEPOSIT_PLAN.out.mapping)
            .join(ch_hits, remainder: true)
            .map { s, plan, checks, upload, gate, mapping, hits ->
                [s, plan, checks, upload ? upload.resolve('upload_manifest.tsv') : no_file, gate, mapping, hits ?: no_file]
            },
        mode, cfg.report_params
    )

    def ch_published = ch_upload_set
        .join(GATE.out.status)
        .branch { _s, _set, status ->
            passed: status == 'passed'
            blocked: true
        }

    //
    // Collate software versions (private in sensitive mode: design §9)
    //
    def topic_versions = channel.topic("versions")
        .distinct()
        .branch { entry ->
            versions_file: entry instanceof Path
            versions_tuple: true
        }
    def topic_versions_string = topic_versions.versions_tuple
        .map { process, tool, version ->
            [ process[process.lastIndexOf(':')+1..-1], "  ${tool}: ${version}" ]
        }
        .groupTuple(by:0)
        .map { process, tool_versions ->
            tool_versions.unique().sort()
            "${process}:\n${tool_versions.join('\n')}"
        }
    softwareVersionsToYAML(topic_versions.versions_file)
        .mix(topic_versions_string)
        .collectFile(
            storeDir: "${cfg.info_dir}",
            name:  'msdeposit_software_versions.yml',
            sort: true,
            newLine: true
        )

    emit:
    upload_sets    = ch_published.passed.map { s, set, _status -> [s, set] }       // public
    blocked        = ch_published.blocked.map { s, set, _status -> [s, set] }      // private
    gate           = GATE.out.json                                                // private
    report_private = DEPOSIT_REPORT_PRIVATE.out.html                              // private
    checks         = ch_all_checks                                                // private (incl. details)
    plan           = DEPOSIT_PLAN.out.plan                                        // private copy
    mapping        = DEPOSIT_PLAN.out.mapping                                     // private
    raw_ids        = VALIDATE_INPUTS.out.raw_ids                                  // private
    runs           = VALIDATE_INPUTS.out.runs                                     // private
    fixity         = RAW_FIXITY.out.fixity.map { meta, f -> [meta.study, f] }    // private
    finalise       = MZML_FINALISE.out.private_json.map { meta, f -> [meta.study, meta.run, f] }  // private
    denylist       = ch_denylist.filter { _s, d -> d }                            // private
    hits           = ch_hits.filter { _s, h -> h }                                // private
    gate_status    = GATE.out.status
}

/*
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    FUNCTIONS
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
*/

// [study, item] → [study, [items]] once all runs of the study arrived; incomplete groups (failed runs) are emitted
// at the end instead of being dropped (design §7, review finding 16)
def groupByStudy(ch, ch_counts) {
    return ch
        .combine(ch_counts, by: 0)
        .map { s, item, n -> [groupKey(s, n), item] }
        .groupTuple(remainder: true)
        .map { key, items -> [key.toString(), items] }
}

def readJson(path) {
    return new groovy.json.JsonSlurper().parse(path.toFile())
}
