"""Synthetic vendor raw data and upload sets for the sensitive-path tests. Nothing derives from real data; the
identifying content reuses the invented values of `synthetic.PLANTED`."""

import json
import sqlite3
import struct
from pathlib import Path

from synthetic import PLANTED, mzml_text

from msdeposit_tools.io import write_json
from msdeposit_tools.sensitive.denylist import Entry, write_denylist

GENERIC_NOISE = ["Q Exactive Plus Orbitrap", "Xcalibur", "Spray Voltage (kV): 3.50", "Lipids", "Plasma",
                 "Ion Source Type: HESI", "Sheath gas flow rate: 40"]


def u16(s: str) -> bytes:
    return s.encode("utf-16-le")


def pascal16(s: str) -> bytes:
    return struct.pack("<I", len(s)) + u16(s)


def thermo_raw(path: Path) -> Path:
    """A Thermo-like file: magic, 'Finnigan' signature, two audit tags (creator logon), sample info strings,
    method strings with repeated settings, then binary 'scan data'."""
    head = b"\x01\xa1" + u16("Finnigan\x00") + bytes(16) + struct.pack("<I", 66)
    for _ in range(2):   # audit start/end: time, tag1 (id), tag2 (logon), unknown
        head += bytes(8) + u16("qmarbleford").ljust(50, b"\x00") + u16("HALDEN-PC07").ljust(50, b"\x00") + bytes(4)
    head = head.ljust(1356, b"\x00")
    strings = [PLANTED["path"] + "\\" + PLANTED["raw_name"] + ".raw", "Comment: QC run, Haldenbrook Lipid Lab",
               "Instrument Method", "Spray Voltage (kV): 3.50", "Spray Voltage (kV): 3.50",
               "Capillary Temperature (C): 320", "Spray Voltage (kV): 3.50", "Operator: Quentin Mürbleford",
               *GENERIC_NOISE]
    body = b"".join(pascal16(s) + bytes(7) for s in strings)
    noise = bytes(range(256)) * 64
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(head + body + noise + u16("AfterNoise: ignored-setting"))
    return path


def sciex_wiff(path: Path) -> Path:
    data = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(504) + b"Computer Name: HLB-MS-PC07\x00\x00"
            + u16("Acquired by: HALDEN\\qmarbleford") + bytes(4) + b"Sample List: HLB_sequence_2024\x00"
            + u16("Q1 Mass (Da)") + bytes(64))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def bruker_tdf(path: Path, outside: Path | None = None) -> Path:
    d = Path(path)
    (d / "lipids_pos.m").mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(d / "analysis.tdf")
    con.execute("CREATE TABLE GlobalMetadata (Key TEXT PRIMARY KEY, Value TEXT)")
    con.executemany("INSERT INTO GlobalMetadata VALUES (?, ?)", [
        ("OperatorName", PLANTED["operator"]), ("InstrumentSerialNumber", "1838271.10234"),
        ("InstrumentName", "timsTOF Pro"), ("SampleName", PLANTED["raw_name"]),
        ("MethodName", r"D:\Methods\Haldenbrook\lipids_pos.m"), ("AcquisitionSoftware", "timsControl"),
        ("AcquisitionDateTime", "2024-03-05T09:41:17.123+01:00")])
    con.commit()
    con.close()
    (d / "analysis.tdf_bin").write_bytes(b"\x00SECRETINBINARY\x00" * 10)
    (d / "SampleInfo.xml").write_text(
        f'<?xml version="1.0"?><SampleTable><Sample SampleID="{PLANTED["raw_name"]}" Operator="QMF" '
        f'Comment="from the Haldenbrook Lipid Lab"/></SampleTable>')
    (d / "lipids_pos.m" / "submethods.xml").write_text(
        '<root><para_string permname="Operator" value="qmf"/><para_double permname="Capillary" value="4500"/></root>')
    (d / "lipids_pos.m" / "lipids_pos.method").write_text(
        f"<method><Comment>contact {PLANTED['email']}</Comment></method>")
    if outside is not None:
        (outside / "evil.m").mkdir(parents=True, exist_ok=True)
        (outside / "evil.m" / "x.method").write_text("<m><Operator>OutsideOperatorName</Operator></m>")
        (outside / "SampleInfo.xml").write_text("<s><Operator>OutsideSampleInfo</Operator></s>")
        (d / "evil.m").symlink_to(outside / "evil.m")
    return d


def waters_raw(path: Path) -> Path:
    d = Path(path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "_HEADER.TXT").write_text("$$ Job Code: HLB-2024-07\n$$ User Name: qmarbleford\n"
                                   "$$ Instrument: Xevo TQ-S#WAA123\n"
                                   f"$$ Acquired Name: {PLANTED['raw_name']}\n$$ Sample Description: Plasma QC\n")
    (d / "_extern.inf").write_text("Source Temp (C)\t150\nCapillary (kV)\t3.0\n")
    (d / "_inlet.inf").write_text("Method path: C:\\MassLynx\\Haldenbrook.PRO\\ACQUDB\\lipids\n")
    (d / "_FUNC001.DAT").write_bytes(b"Operator: InTheSpectraFile\x00" * 4)
    return d


def agilent_d(path: Path) -> Path:
    d = Path(path) / "AcqData"
    (d / "lipids.m").mkdir(parents=True, exist_ok=True)
    (d / "sample_info.xml").write_text(
        "<SampleInfo><Field><Name>Operator Name</Name><Value>Quentin Marbleford</Value></Field>"
        f"<Field><Name>Sample Name</Name><Value>{PLANTED['raw_name']}</Value></Field></SampleInfo>")
    (d / "Devices.xml").write_text("<Devices><Device><Name>QTOF</Name><SerialNumber>SG1234X567</SerialNumber>"
                                   "</Device></Devices>")
    (d / "lipids.m" / "AcqMethod.xml").write_text(f"<Method><Comment>contact {PLANTED['email']}</Comment></Method>")
    (d / "MSScan.bin").write_bytes(b"Operator: InTheSpectraFile" * 4)
    return Path(path)


# --- upload sets --------------------------------------------------------------------------------------------------
RUNS = [("r001", "QC_01", "POS", "QC_01_POS", "S1_RAW_001"), ("r002", "P1_A", "POS", "P1_A_POS", "S1_RAW_002")]


def clean_mzml(stem: str, raw_id: str, sample: str, start: str = "2024-03-05T09:41:17Z") -> bytes:
    """An mzML shaped like the anonymised output: no serial, contact or header userParams, neutral source file.
    (Index offsets are not recomputed; the checks here do not use them.)"""
    data = mzml_text(plant=False, run_id=stem, sample=sample, start=start)
    data = data.replace(b'id="file.raw" name="file.raw" location="file:///C:/data"',
                        f'id="SF1" name="{raw_id}.raw" location="file:///"'.encode())
    return data.replace(b'defaultSourceFileRef="file.raw"', b'defaultSourceFileRef="SF1"')


def upload_set(root: Path, study: str = "S1") -> dict:
    """A clean sensitive-mode upload set and its plan. Returns {'dir', 'plan', 'mzml': [...]}."""
    root = Path(root)
    up = root / study
    (up / "FILES" / "DERIVED_FILES").mkdir(parents=True, exist_ok=True)
    mzml = []
    for _, sample, _, stem, rid in RUNS:
        p = up / "FILES" / "DERIVED_FILES" / f"{stem}.mzML"
        p.write_bytes(clean_mzml(stem, rid, sample))
        mzml.append(p)
    (up / "i_Investigation.txt").write_text(
        "ONTOLOGY SOURCE REFERENCE\nTerm Source Name\t\"MS\"\nSTUDY\nStudy Identifier\t\"S1\"\n"
        "Study Title\t\"Synthetic lipidomics test study\"\nStudy Description\t\"Two assays measured on a "
        "Q Exactive Orbitrap with Xcalibur.\"\nStudy Person Last Name\t\"Example\"\n"
        "Study Person First Name\t\"Ada\"\nStudy Person Affiliation\t\"Example Institute\"\n")
    (up / f"s_{study}.txt").write_text(
        '"Source Name"\t"Characteristics[Organism]"\t"Protocol REF"\t"Sample Name"\n'
        '"NIST SRM 1950"\t"Homo sapiens"\t"Sample collection"\t"QC_01"\n'
        '"Plasma 1"\t"Homo sapiens"\t"Sample collection"\t"P1_A"\n')
    (up / f"a_{study}_POS.txt").write_text(
        '"Sample Name"\t"Protocol REF"\t"MS Assay Name"\t"Raw Spectral Data File"\t'
        '"Comment[Raw data file identifier]"\t"Derived Spectral Data File"\t"Metabolite Assignment File"\n'
        + "".join(f'"{s}"\t"Mass spectrometry"\t"{stem}"\t""\t"{rid}"\t"FILES/DERIVED_FILES/{stem}.mzML"\t'
                  f'"m_{study}_POS_maf.tsv"\n' for _, s, _, stem, rid in RUNS))
    (up / f"m_{study}_POS_maf.tsv").write_text(
        "database_identifier\tchemical_formula\tmetabolite_identification\tmass_to_charge\tQC_01_POS\n"
        "\tC42H82NO8P\tPC 34:1\t760.5851\t12.5\n")
    (up / "FILES" / "RESULTS").mkdir(parents=True, exist_ok=True)
    (up / "FILES" / "RESULTS" / f"{study}.mztab-2.0.0-M.mzTab").write_text(
        "MTD\tmzTab-version\t2.0.0-M\nMTD\tmzTab-ID\tS1\nMTD\tinstrument[1]-name\t[MS, MS:1001911, Q Exactive, ]\n"
        "SMH\tSML_ID\tchemical_name\tabundance_assay[1]\nSML\t1\tPC 34:1\t12.5\n")
    (up / "FILES" / "QC").mkdir(parents=True, exist_ok=True)
    (up / "FILES" / "QC" / f"{study}.mzQC").write_text(json.dumps(
        {"mzQC": {"version": "1.0.0", "runQualities": [{"metadata": {"label": "QC_01_POS"}}]}}, indent=1))
    (up / "FILES" / "QC" / "deposit_report.html").write_text(
        "<html><body><h1>Deposit report</h1><p>Checks S1-S7: pass. Runs: 2.</p></body></html>")
    plan = dict(schema="msdeposit/plan/1", study=study, accession=None, prefix=study, mode="sensitive",
                deposited_naming="neutral", deposit_raw=False, investigation_file="i_Investigation.txt",
                sample_file=f"s_{study}.txt",
                assays=[dict(assay="POS", file=f"a_{study}_POS.txt", maf=f"m_{study}_POS_maf.tsv",
                             runs=[r[0] for r in RUNS])],
                runs=[dict(run=r, assay=a, sample=s, ms_assay_name=stem, deposited_mzml=f"{stem}.mzML",
                           derived_path=f"FILES/DERIVED_FILES/{stem}.mzML", raw_id=rid, raw_path="")
                      for r, s, a, stem, rid in RUNS],
                raw_files=[dict(raw_id=rid, packaging="none", deposited=[]) for *_, rid in RUNS],
                issues=[], status="pass")
    write_json(root / "plan.json", plan)
    return dict(dir=up, plan=root / "plan.json", mzml=mzml)


def planted_denylist(path: Path) -> Path:
    """The tokens a correct denylist holds for synthetic.PLANTED."""
    write_denylist(path, [
        Entry("Marbleford", "candidate", False, "test"), Entry("qmarbleford", "candidate", False, "test"),
        Entry("Quentin", "candidate", False, "test"), Entry("QMF", "candidate", True, "test"),
        Entry("Haldenbrook", "user", False, "test"), Entry("SN-ZX9917", "candidate", False, "test"),
        Entry("HLB", "original_name", True, "test"), Entry(PLANTED["raw_name"], "original_name", False, "test")])
    return Path(path)
