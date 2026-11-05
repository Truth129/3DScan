"""Synthetic DICOMweb transport; no network, archive or patient data."""

from scan3d.archive import dicomjson


def test_person_name_rebuilds_dicom_groups():
    def name(*values):
        return dicomjson.person_name({"00100010": {"vr": "PN", "Value": list(values)}}, "00100010")

    assert name({"Alphabetic": "Yamada^Tarou", "Ideographic": "山田^太郎", "Phonetic": "やまだ^たろう"}) == "Yamada^Tarou=山田^太郎=やまだ^たろう"
    assert name({"Alphabetic": "Yamada^Tarou", "Ideographic": "山田^太郎", "Phonetic": ""}) == "Yamada^Tarou=山田^太郎"
    assert name({"Ideographic": "山田^太郎"}) == "=山田^太郎"
    assert name({"Alphabetic": "Synthetic^One"}, {"Alphabetic": "Synthetic^Two"}) == "Synthetic^One\\Synthetic^Two"
    assert name("Not^AnObject") == "" and dicomjson.person_name({}, "00100010") == ""
