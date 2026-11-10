"""Synthetic test data: small indexed mzML files shaped like msconvert output, with planted identifying content.

Nothing here derives from real study data. The planted tokens (`PLANTED`) appear in the places where real files
leak lab identity: instrument serial, source file name/location, contact, sample name, header userParams,
spectrum titles and SRM chromatogram ids.
"""

import base64
import csv
import hashlib
import re
import struct
import zlib
from pathlib import Path

PLANTED = {
    "serial": "SN-ZX9917",
    "operator": "Quentin Marbleford",
    "initials": "QMF",
    "lab": "Haldenbrook Lipid Lab",
    "path": r"D:\Users\qmarbleford\HaldenbrookProject\Batch7",
    "raw_name": "HLB_qmf_plasma_007",
    "transition": "PC 34:1_HLB_quan",
    "email": "q.marbleford@haldenbrook.example",
}

NATIVE_ID = {
    "thermo": ("MS:1000768", "Thermo nativeID format", "MS:1000563", "Thermo RAW format", ".raw"),
    "sciex": ("MS:1000770", "WIFF nativeID format", "MS:1000562", "ABI WIFF format", ".wiff"),
    "waters": ("MS:1000769", "Waters nativeID format", "MS:1000526", "Waters raw format", ".raw"),
    "agilent": ("MS:1001508", "Agilent MassHunter nativeID format", "MS:1001509", "Agilent MassHunter format", ".d"),
}


def _b64(values, double=True, compress=True) -> str:
    raw = struct.pack(f"<{len(values)}{'d' if double else 'f'}", *values)
    return base64.b64encode(zlib.compress(raw) if compress else raw).decode()


def _array(values, acc, name, unit=None) -> str:
    enc = _b64(values)
    unit_attr = f' unitCvRef="MS" unitAccession="{unit[0]}" unitName="{unit[1]}"' if unit else ""
    return (f'            <binaryDataArray encodedLength="{len(enc)}">\n'
            f'              <cvParam cvRef="MS" accession="MS:1000523" name="64-bit float" value=""/>\n'
            f'              <cvParam cvRef="MS" accession="MS:1000574" name="zlib compression" value=""/>\n'
            f'              <cvParam cvRef="MS" accession="{acc}" name="{name}" value=""{unit_attr}/>\n'
            f'              <binary>{enc}</binary>\n'
            f'            </binaryDataArray>\n')


def mzml_text(*, vendor="thermo", polarity="positive", n_spectra=6, srm=False, plant=True, sample="sample_1",
              model=("MS:1001911", "Q Exactive"), start="2024-03-05T09:41:17Z", run_id=None) -> bytes:
    """An indexed mzML document as bytes (index offsets and SHA-1 checksum are correct)."""
    nid_acc, nid_name, fmt_acc, fmt_name, ext = NATIVE_ID[vendor]
    p = PLANTED if plant else {k: "" for k in PLANTED}
    raw_name = (p["raw_name"] or "file") + ext
    location = "file:///" + (p["path"] or "C:\\data").replace("\\", "/")
    pol_acc, pol_name = ("MS:1000130", "positive scan") if polarity == "positive" else ("MS:1000129", "negative scan")
    run_id = run_id or (p["raw_name"] or "run")
    head = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<indexedmzML xmlns="http://psi.hupo.org/ms/mzml" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://psi.hupo.org/ms/mzml http://psidev.info/files/ms/mzML/xsd/mzML1.1.2_idx.xsd">',
        f'  <mzML xmlns="http://psi.hupo.org/ms/mzml" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'xsi:schemaLocation="http://psi.hupo.org/ms/mzml http://psidev.info/files/ms/mzML/xsd/mzML1.1.0.xsd" '
        f'id="{run_id}" version="1.1.0">',
        '    <cvList count="2">',
        '      <cv id="MS" fullName="Proteomics Standards Initiative Mass Spectrometry Ontology" version="4.1.200" '
        'URI="https://raw.githubusercontent.com/HUPO-PSI/psi-ms-CV/master/psi-ms.obo"/>',
        '      <cv id="UO" fullName="Unit Ontology" version="09:04:2014" '
        'URI="https://raw.githubusercontent.com/bio-ontology-research-group/unit-ontology/master/unit.obo"/>',
        '    </cvList>',
        '    <fileDescription>',
        '      <fileContent>',
        '        <cvParam cvRef="MS" accession="MS:1000579" name="MS1 spectrum" value=""/>',
        '        <cvParam cvRef="MS" accession="MS:1000127" name="centroid spectrum" value=""/>',
        *(['        <cvParam cvRef="MS" accession="MS:1001473" name="selected reaction monitoring chromatogram" '
           'value=""/>'] if srm else []),
        '      </fileContent>',
        '      <sourceFileList count="1">',
        f'        <sourceFile id="{raw_name}" name="{raw_name}" location="{location}">',
        f'          <cvParam cvRef="MS" accession="{nid_acc}" name="{nid_name}" value=""/>',
        f'          <cvParam cvRef="MS" accession="{fmt_acc}" name="{fmt_name}" value=""/>',
        '          <cvParam cvRef="MS" accession="MS:1000569" name="SHA-1" '
        'value="0123456789abcdef0123456789abcdef01234567"/>',
        '        </sourceFile>',
        '      </sourceFileList>',
        *(['      <contact>',
           f'        <cvParam cvRef="MS" accession="MS:1000586" name="contact name" value="{p["operator"]}"/>',
           f'        <cvParam cvRef="MS" accession="MS:1000590" name="contact affiliation" value="{p["lab"]}"/>',
           f'        <cvParam cvRef="MS" accession="MS:1000589" name="contact email" value="{p["email"]}"/>',
           '      </contact>'] if plant else []),
        '    </fileDescription>',
        '    <sampleList count="1">',
        f'      <sample id="{sample}" name="{sample}">',
        *([f'        <userParam name="Operator" value="{p["initials"]}" type="xsd:string"/>'] if plant else []),
        '      </sample>',
        '    </sampleList>',
        '    <softwareList count="2">',
        '      <software id="Xcalibur" version="4.1.31.9">',
        '        <cvParam cvRef="MS" accession="MS:1000532" name="Xcalibur" value=""/>',
        '      </software>',
        '      <software id="pwiz" version="3.0.26121">',
        '        <cvParam cvRef="MS" accession="MS:1000615" name="ProteoWizard software" value=""/>',
        '      </software>',
        '    </softwareList>',
        '    <instrumentConfigurationList count="1">',
        '      <instrumentConfiguration id="IC1">',
        f'        <cvParam cvRef="MS" accession="{model[0]}" name="{model[1]}" value=""/>',
        *([f'        <cvParam cvRef="MS" accession="MS:1000529" name="instrument serial number" '
           f'value="{p["serial"]}"/>'] if plant else []),
        '        <componentList count="3">',
        '          <source order="1">',
        '            <cvParam cvRef="MS" accession="MS:1000073" name="electrospray ionization" value=""/>',
        '          </source>',
        '          <analyzer order="2">',
        '            <cvParam cvRef="MS" accession="MS:1000484" name="orbitrap" value=""/>',
        '          </analyzer>',
        '          <detector order="3">',
        '            <cvParam cvRef="MS" accession="MS:1000624" name="inductive detector" value=""/>',
        '          </detector>',
        '        </componentList>',
        '        <softwareRef ref="Xcalibur"/>',
        '      </instrumentConfiguration>',
        '    </instrumentConfigurationList>',
        '    <dataProcessingList count="1">',
        '      <dataProcessing id="pwiz_Reader_conversion">',
        '        <processingMethod order="0" softwareRef="pwiz">',
        '          <cvParam cvRef="MS" accession="MS:1000544" name="Conversion to mzML" value=""/>',
        '        </processingMethod>',
        '      </dataProcessing>',
        '    </dataProcessingList>',
        f'    <run id="{run_id}" defaultInstrumentConfigurationRef="IC1" startTimeStamp="{start}" '
        f'defaultSourceFileRef="{raw_name}" sampleRef="{sample}">',
    ]
    body: list[str] = []
    spectra_offsets, chrom_offsets = [], []
    if n_spectra:
        body.append(f'      <spectrumList count="{n_spectra}" defaultDataProcessingRef="pwiz_Reader_conversion">')
        for i in range(n_spectra):
            mz = [100.0 + i + k * 50.5 for k in range(5)]
            inten = [1000.0 * (k + 1) * (i + 1) for k in range(5)]
            sid = f"controllerType=0 controllerNumber=1 scan={i + 1}"
            title = (f'          <userParam name="spectrum title" value="{p["raw_name"]}.{i + 1}.{i + 1}.1" '
                     f'type="xsd:string"/>\n') if plant else ""
            body.append(
                f'        <spectrum index="{i}" id="{sid}" defaultArrayLength="5">\n'
                '          <cvParam cvRef="MS" accession="MS:1000511" name="ms level" value="1"/>\n'
                '          <cvParam cvRef="MS" accession="MS:1000579" name="MS1 spectrum" value=""/>\n'
                f'          <cvParam cvRef="MS" accession="{pol_acc}" name="{pol_name}" value=""/>\n'
                '          <cvParam cvRef="MS" accession="MS:1000127" name="centroid spectrum" value=""/>\n'
                f'          <cvParam cvRef="MS" accession="MS:1000285" name="total ion current" value="{sum(inten)}"/>\n'
                f'          <cvParam cvRef="MS" accession="MS:1000505" name="base peak intensity" value="{max(inten)}"/>\n'
                + title +
                '          <scanList count="1">\n'
                '            <cvParam cvRef="MS" accession="MS:1000795" name="no combination" value=""/>\n'
                '            <scan>\n'
                f'              <cvParam cvRef="MS" accession="MS:1000016" name="scan start time" value="{0.1 * (i + 1):.4f}" '
                'unitCvRef="UO" unitAccession="UO:0000031" unitName="minute"/>\n'
                '              <scanWindowList count="1">\n'
                '                <scanWindow>\n'
                '                  <cvParam cvRef="MS" accession="MS:1000501" name="scan window lower limit" value="100" '
                'unitCvRef="MS" unitAccession="MS:1000040" unitName="m/z"/>\n'
                '                  <cvParam cvRef="MS" accession="MS:1000500" name="scan window upper limit" value="1200" '
                'unitCvRef="MS" unitAccession="MS:1000040" unitName="m/z"/>\n'
                '                </scanWindow>\n'
                '              </scanWindowList>\n'
                '            </scan>\n'
                '          </scanList>\n'
                '          <binaryDataArrayList count="2">\n'
                + _array(mz, "MS:1000514", "m/z array", ("MS:1000040", "m/z"))
                + _array(inten, "MS:1000515", "intensity array", ("MS:1000131", "number of detector counts")) +
                '          </binaryDataArrayList>\n'
                '        </spectrum>')
            spectra_offsets.append((sid, len(body) - 1))
        body.append('      </spectrumList>')
    chroms = [("TIC", "MS:1000235", "total ion current chromatogram", None)]
    if srm:
        name = p["transition"] or "T1"
        chroms += [(f"- SRM SIC Q1=760.6 Q3=184.1 sample=1 period=1 experiment=1 transition=0 name={name}",
                    "MS:1001473", "selected reaction monitoring chromatogram", (760.6, 184.1)),
                   ("- SRM SIC Q1=786.6 Q3=184.1 sample=1 period=1 experiment=1 transition=1 name=PC 36:2",
                    "MS:1001473", "selected reaction monitoring chromatogram", (786.6, 184.1))]
    body.append(f'      <chromatogramList count="{len(chroms)}" defaultDataProcessingRef="pwiz_Reader_conversion">')
    for j, (cid, acc, cname, tr) in enumerate(chroms):
        times = [0.1 * (k + 1) for k in range(max(n_spectra, 4))]
        inten = [500.0 * (k + 1) for k in range(len(times))]
        prec = ""
        if tr:
            prec = ('          <precursor>\n            <isolationWindow>\n'
                    f'              <cvParam cvRef="MS" accession="MS:1000827" name="isolation window target m/z" '
                    f'value="{tr[0]}" unitCvRef="MS" unitAccession="MS:1000040" unitName="m/z"/>\n'
                    '            </isolationWindow>\n            <activation>\n'
                    '              <cvParam cvRef="MS" accession="MS:1000133" name="collision-induced dissociation" value=""/>\n'
                    '              <cvParam cvRef="MS" accession="MS:1000045" name="collision energy" value="35" '
                    'unitCvRef="UO" unitAccession="UO:0000266" unitName="electronvolt"/>\n'
                    '            </activation>\n          </precursor>\n'
                    '          <product>\n            <isolationWindow>\n'
                    f'              <cvParam cvRef="MS" accession="MS:1000827" name="isolation window target m/z" '
                    f'value="{tr[1]}" unitCvRef="MS" unitAccession="MS:1000040" unitName="m/z"/>\n'
                    '            </isolationWindow>\n          </product>\n')
        body.append(
            f'        <chromatogram index="{j}" id="{cid}" defaultArrayLength="{len(times)}">\n'
            f'          <cvParam cvRef="MS" accession="{acc}" name="{cname}" value=""/>\n'
            + (f'          <cvParam cvRef="MS" accession="{pol_acc}" name="{pol_name}" value=""/>\n' if tr else "")
            + prec +
            '          <binaryDataArrayList count="2">\n'
            + _array(times, "MS:1000595", "time array", ("UO:0000031", "minute"))
            + _array(inten, "MS:1000515", "intensity array", ("MS:1000131", "number of detector counts")) +
            '          </binaryDataArrayList>\n'
            '        </chromatogram>')
        chrom_offsets.append((cid, len(body) - 1))
    body.append('      </chromatogramList>')
    tail = ['    </run>', '  </mzML>']

    text = "\n".join(head) + "\n"
    offsets = {}
    for i, line in enumerate(body):
        offsets[i] = len(text.encode())
        text += line + "\n"
    text += "\n".join(tail) + "\n"
    index = [f'  <indexList count="{1 + bool(n_spectra)}">']
    if n_spectra:
        index.append('    <index name="spectrum">')
        index += [f'      <offset idRef="{sid}">{offsets[k] + 8}</offset>' for sid, k in spectra_offsets]
        index.append('    </index>')
    index.append('    <index name="chromatogram">')
    index += [f'      <offset idRef="{_xml_attr(cid)}">{offsets[k] + 8}</offset>' for cid, k in chrom_offsets]
    index += ['    </index>', '  </indexList>']
    index_offset = len(text.encode())
    text += "\n".join(index) + "\n" + f"  <indexListOffset>{index_offset}</indexListOffset>\n  <fileChecksum>"
    data = text.encode()
    return data + hashlib.sha1(data).hexdigest().encode() + b"</fileChecksum>\n</indexedmzML>\n"


def _xml_attr(s: str) -> str:
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def write_mzml(path: Path, **kw) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(mzml_text(**kw))
    return path


def check_offsets(data: bytes) -> bool:
    """True when every index offset points at the start of its element (used by tests of writers)."""
    for m in re.finditer(rb'<offset idRef="([^"]*)">(\d+)</offset>', data):
        at = int(m.group(2))
        if not (data.startswith(b"<spectrum ", at) or data.startswith(b"<chromatogram ", at)):
            return False
    return True


def write_example_inputs(root: Path, *, mode: str = "standard", srm: bool = False) -> dict[str, Path]:
    """A two-assay study 'S1' with four runs given as mzML (format=mzml) plus the sheets of design §4.

    Returns the paths of the sheets (keys as the validate-inputs options) and of the mzML files ("mzml").
    """
    root = Path(root)
    data = root / "data"
    runs = []
    for i, (sample, pol, assay) in enumerate([("QC_01", "positive", "POS"), ("P1_A", "positive", "POS"),
                                              ("QC_01", "negative", "NEG"), ("P1_A", "negative", "NEG")], 1):
        stem = f"{PLANTED['raw_name']}_{i:02d}" if mode == "standard" else f"lab_file_{i:02d}"
        path = write_mzml(data / f"{stem}.mzML", polarity=pol, sample=sample, srm=srm)
        runs.append(dict(study="S1", run=f"r{i:03d}", assay=assay, sample=sample, format="mzml",
                         path=str(path.relative_to(root)), vendor="thermo", polarity=pol))
    sheets = {}

    def write(name, rows, fields=None):
        fields = fields or list(rows[0])
        p = root / name
        with open(p, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        return p

    sheets["input"] = write("runs.csv", runs)
    sheets["assays"] = write("assays.csv", [
        dict(study="S1", assay="POS", technique="LC-MS", polarity="positive", column_model="C18 100 x 2.1 mm",
             column_type="reverse phase", measurement_type="targeted", protocol_keys="extraction;chromatography"),
        dict(study="S1", assay="NEG", technique="LC-MS", polarity="negative", column_model="C18 100 x 2.1 mm",
             column_type="reverse phase", measurement_type="targeted", protocol_keys="extraction;chromatography")])
    sheets["samples"] = write("samples.csv", [
        {"study": "S1", "source_name": "NIST SRM 1950", "sample_name": "QC_01",
         "characteristics:Organism": "Homo sapiens", "characteristics:Organism::source": "NCBITaxon",
         "characteristics:Organism::accession": "http://purl.obolibrary.org/obo/NCBITaxon_9606",
         "characteristics:Organism part": "blood plasma", "factor:Sample material": "NIST SRM 1950",
         "comment:Sequence position": "1"},
        {"study": "S1", "source_name": "Plasma 1", "sample_name": "P1_A",
         "characteristics:Organism": "Homo sapiens", "characteristics:Organism::source": "NCBITaxon",
         "characteristics:Organism::accession": "http://purl.obolibrary.org/obo/NCBITaxon_9606",
         "characteristics:Organism part": "blood plasma", "factor:Sample material": "Plasma 1",
         "comment:Sequence position": "2"}])
    (root / "studies.yml").write_text(
        "defaults:\n  license: CC BY 4.0\n  release_date: '2027-01-15'\n"
        "studies:\n  S1:\n    title: Synthetic lipidomics test study\n    description: Two assays, four runs.\n"
        "    protocols:\n      Extraction: extraction\n")
    sheets["studies"] = root / "studies.yml"
    sheets["features"] = write("features.csv", [
        dict(study="S1", assay="POS", feature="f1", name="PC 34:1", level="species", formula="C42H82NO8P",
             mz="760.5851", rt="", adduct="[M+H]1+", charge="1", database_id="", quantifier=""),
        dict(study="S1", assay="NEG", feature="f2", name="PE 36:2", level="species", formula="C41H78NO8P",
             mz="740.5236", rt="5.1", adduct="[M-H]1-", charge="-1", database_id="", quantifier="")])
    sheets["study_variables"] = write("study_variables.csv", [
        dict(study="S1", study_variable="NIST", description="NIST SRM 1950 QC", average_function="median"),
        dict(study="S1", study_variable="P1", description="Plasma 1", average_function="median")])
    sheets["study_variable_members"] = write("members.csv", [
        dict(study="S1", study_variable="NIST", sample="QC_01"), dict(study="S1", study_variable="P1", sample="P1_A")])
    sheets["values"] = write("values.csv", [
        dict(study="S1", feature="f1", run="", study_variable="NIST", value="12.5", unit="UO:0000064",
             variation="4.2", variation_type="cv_percent"),
        dict(study="S1", feature="f1", run="r001", study_variable="", value="12.1", unit="UO:0000064",
             variation="", variation_type=""),
        dict(study="S1", feature="f2", run="", study_variable="P1", value="3.3", unit="UO:0000064",
             variation="", variation_type="none")])
    sheets["contacts"] = write("contacts.csv", [
        dict(study="", first_name="Ada", last_name="Example", email="ada@example.org", affiliation="Example Institute",
             address="", orcid="", roles="Principal Investigator;Submitter")])
    return sheets
