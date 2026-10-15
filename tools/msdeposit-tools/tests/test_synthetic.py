import hashlib

from lxml import etree

from synthetic import PLANTED, check_offsets, mzml_text


def test_synthetic_mzml_is_wellformed_and_indexed():
    for srm in (False, True):
        data = mzml_text(srm=srm)
        etree.fromstring(data)
        assert check_offsets(data)
        head, _, rest = data.partition(b"<fileChecksum>")
        assert rest.startswith(hashlib.sha1(head + b"<fileChecksum>").hexdigest().encode())
        assert PLANTED["serial"].encode() in data


def test_unplanted_has_no_tokens():
    data = mzml_text(plant=False, srm=True)
    for tok in PLANTED.values():
        assert tok.encode() not in data
