import zipfile

from newsroom.sources import nhtsa


def row(**values):
    """One tab-separated NHTSA row; values are keyed by CMPL.txt field number."""
    fields = [""] * 51
    for number, value in values.items():
        fields[int(number.removeprefix("f")) - 1] = value
    return "\t".join(fields)


def write_zip(path, rows):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("COMPLAINTS_RECEIVED_2025-2026.txt", "\r\n".join(rows) + "\r\n")
    return path


def test_rows_for_one_complaint_merge_into_one(tmp_path):
    rows = [
        row(f1="1", f2="11600001", f3="General Motors LLC", f4="CHEVROLET", f5="COBALT", f6="2006", f7="N", f9="N",
            f10="0", f11="0", f12="ELECTRICAL SYSTEM", f13="SPRINGFIELD", f17="20250105", f20="Engine shut off.",
            f51="JANE DOE"),
        row(f1="2", f2="11600001", f3="General Motors LLC", f4="CHEVROLET", f5="COBALT", f6="2006", f7="Y", f9="N",
            f10="1", f11="0", f12="AIR BAGS", f13="SPRINGFIELD", f17="20250105", f20="Engine shut off.", f51="JANE DOE"),
        row(f1="3", f2="11600002", f3="Ford Motor Company", f4="FORD", f5="EXPLORER", f6="9999", f9="Y", f10="0",
            f11="0", f12="ENGINE", f17="20250210", f20="Fire in the engine bay."),
    ]
    complaints = {c["id"]: c for c in nhtsa.parse(write_zip(tmp_path / "c.zip", rows))}

    assert set(complaints) == {"nhtsa:11600001", "nhtsa:11600002"}
    cobalt = complaints["nhtsa:11600001"]
    assert cobalt["product"] == "2006 CHEVROLET COBALT"
    assert cobalt["received"] == "2025-01-05"
    assert cobalt["fields"]["components"] == ["ELECTRICAL SYSTEM", "AIR BAGS"]
    assert cobalt["severe"] is True  # one of its rows reported an injury

    explorer = complaints["nhtsa:11600002"]
    assert explorer["product"] == "FORD EXPLORER (year unknown)"
    assert explorer["severe"] is True  # fire


def test_personal_fields_are_never_kept(tmp_path):
    rows = [row(f2="11600003", f4="FORD", f5="F-150", f6="2020", f13="SPRINGFIELD", f17="20250101",
                f20="Brakes failed.", f41="Main St Ford", f51="JANE DOE")]
    complaint = next(nhtsa.parse(write_zip(tmp_path / "c.zip", rows)))
    stored = repr(complaint)
    for personal in ("SPRINGFIELD", "JANE DOE", "Main St Ford"):
        assert personal not in stored
