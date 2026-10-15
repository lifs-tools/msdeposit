/*
    CONVERT: every run to an indexed mzML (design §6). Vendor files go through ProteoWizard msconvert (Wine image)
    or, for Thermo with --thermo_converter thermorawfileparser, ThermoRawFileParser; text dumps through
    text-to-mzml; foreign mzML is normalised by mzml-index.
*/

include { PWIZ_MSCONVERT      } from '../../../modules/local/pwiz_msconvert'
include { THERMORAWFILEPARSER } from '../../../modules/nf-core/thermorawfileparser'
include { TEXT_TO_MZML        } from '../../../modules/local/text_to_mzml'
include { MZML_INDEX          } from '../../../modules/local/mzml_index'

workflow CONVERT {
    take:
    ch_runs           // channel: [ meta, path, [companions] ]
    thermo_converter  // value: 'pwiz' | 'thermorawfileparser'

    main:
    def ch_branched = ch_runs.branch { meta, _path, _companions ->
        mzml: meta.format == 'mzml'
        text: meta.format == 'pwiz_text'
        trfp: meta.vendor == 'thermo' && thermo_converter == 'thermorawfileparser'
        pwiz: true
    }

    PWIZ_MSCONVERT(ch_branched.pwiz)
    THERMORAWFILEPARSER(ch_branched.trfp.map { meta, path, _companions -> [meta, path] })
    TEXT_TO_MZML(ch_branched.text.map { meta, path, _companions -> [meta, path] })
    MZML_INDEX(ch_branched.mzml.map { meta, path, _companions -> [meta, path] })

    // ThermoRawFileParser writes <stem>.mzML; MZML_INDEX normalises it like any foreign mzML would be
    def ch_mzml = PWIZ_MSCONVERT.out.mzml
        .mix(THERMORAWFILEPARSER.out.spectra)
        .mix(TEXT_TO_MZML.out.mzml)
        .mix(MZML_INDEX.out.mzml)

    emit:
    mzml = ch_mzml    // channel: [ meta, mzML ]
}
