"""The aorta report: TotalSegmentator's aorta measurements, from a saved revision's masks.

Research use only, not for diagnostic use. `report_worker.aorta` measures with upstream's own code;
this module says which masks it needs and turns upstream's centimetre values into the result.
"""

from __future__ import annotations

from .reports import Definition, Need

LANDMARKS = (
    (1, "annulus", "Annulus"),
    (2, "sinuses", "Sinuses of Valsalva"),
    (3, "sinotubular_junction", "Sinotubular junction"),
    (4, "mid_ascending", "Mid ascending aorta"),
    (5, "distal_ascending", "Distal ascending aorta"),
    (6, "mid_arch", "Mid aortic arch"),
    (7, "proximal_descending", "Proximal descending aorta"),
    (8, "mid_descending", "Mid descending aorta"),
    (9, "descending_t12", "Descending aorta at T12"),
    (10, "abdominal_celiac", "Abdominal aorta at the celiac trunk"),
    (11, "abdominal_bifurcation", "Abdominal aorta above the bifurcation"),
)
SECTIONS = (
    ("aorta_ascending", "Ascending aorta"),
    ("aorta_arch", "Aortic arch"),
    ("aorta_descending", "Descending aorta"),
    ("aorta_abdominal", "Abdominal aorta"),
    ("aorta_total", "Whole aorta"),
)


def _mm(cm):
    # Upstream rounds centimetres to one decimal, so millimetres are whole: no precision is added.
    return None if cm is None else float(round(cm * 10))


def _area(cm2):
    return None if cm2 is None else round(float(cm2), 1)


def _lumen(landmark, suffix):
    return {
        "diameter_mm": _mm(landmark[f"diameter_{suffix}"]),
        "perpendicular_mm": _mm(landmark[f"diameter_{suffix}_pd"]),
        "area_cm2": _area(landmark[f"area_{suffix}"]),
    }


def _point(values):
    return None if values is None else [round(float(value), 2) for value in values]


def shape(raw):
    """The result's report fields from the worker's output."""
    dissection = bool(raw["dissection"])
    landmarks = []
    for number, identifier, name in LANDMARKS:
        upstream = raw["upstream"]["landmarks"][str(number)]
        geometry = raw["geometry"].get(str(number))
        entry = {"number": number, "id": identifier, "name": name, "available": False, "unavailable_reason": None}
        entry.update(dict.fromkeys(("centre", "normal", "diameter_mm", "perpendicular_mm", "area_cm2", "ends", "perpendicular_ends", "outline")))
        entry.update(true_lumen=None, false_lumen=None, picture=None)
        if geometry is None:
            missing = raw["missing"].get(str(number)) or []
            entry["unavailable_reason"] = f"Not measured: {' and '.join(missing)}" if missing else "Could not be placed on the centreline"
        elif upstream.get("start") is None or not upstream.get("diameter"):
            entry["unavailable_reason"] = "No cross-section of the aorta was found here"
        else:
            entry.update(
                available=True,
                centre=_point(geometry["centre"]),
                normal=[round(float(value), 4) for value in geometry["normal"]],
                diameter_mm=_mm(upstream["diameter"]),
                perpendicular_mm=_mm(upstream["diameter_pd"]),
                area_cm2=_area(upstream["area"]),
                ends=[_point(upstream["start"]), _point(upstream["end"])],
                perpendicular_ends=[_point(upstream["start_pd"]), _point(upstream["end_pd"])] if upstream.get("start_pd") else None,
                outline=[_point(point) for point in geometry["outline"]],
                picture=geometry.get("picture"),
            )
            if dissection:
                entry.update(true_lumen=_lumen(upstream, "tl"), false_lumen=_lumen(upstream, "fl"))
        landmarks.append(entry)
    sections = []
    for identifier, name in SECTIONS:
        stats = raw["upstream"]["section_stats"][identifier]
        available = stats.get("length") is not None
        sections.append(
            {
                "id": identifier,
                "name": name,
                "available": available,
                "length_mm": _mm(stats.get("length")),
                "max_diameter_mm": _mm(stats.get("max_diameter")),
                "volume_ml": _area(stats.get("volume")),
                "true_lumen_volume_ml": _area(stats.get("volume_true_lumen")) if dissection else None,
                "false_lumen_volume_ml": _area(stats.get("volume_false_lumen")) if dissection else None,
            }
        )
    return {
        "contrast": raw["contrast"],
        "dissection": dissection,
        "landmarks": landmarks,
        "sections": sections,
        "centreline": [_point(point) for point in raw["centreline"]],
        "warnings": list(raw["warnings"]),
    }


def summary(result):
    measured = [entry for entry in result["landmarks"] if entry["available"]]
    widest = max(measured, key=lambda entry: entry["diameter_mm"], default=None)
    return {
        "max_diameter_mm": widest and widest["diameter_mm"],
        "max_landmark": widest and widest["number"],
        "landmarks_available": len(measured),
        "landmarks_total": len(LANDMARKS),
    }


DEFINITION = Definition(
    id="aorta",
    title="Aorta",
    engine="TotalSegmentator aorta report",
    module="totalsegmentator.aorta_report",
    # Bump when the worker or this module changes a number or a picture, so saved results are not reused.
    version=1,
    needs=(
        Need("aorta", "Aorta", "total", "aorta", True),
        Need("annulus", "Aortic annulus", "aorta_annulus", "annulus_proper", True),
        Need("sinotubular_junction", "Sinotubular junction", "aorta_annulus", "sinotubular_junction"),
        Need("brachiocephalic_trunk", "Brachiocephalic trunk", "total", "brachiocephalic_trunk"),
        Need("subclavian_artery_left", "Left subclavian artery", "total", "subclavian_artery_left"),
        Need("vertebrae_T12", "T12 vertebra", "total", "vertebrae_T12"),
        Need("iliac_artery_right", "Right iliac artery", "total", "iliac_artery_right"),
        Need("celiac_trunk", "Celiac trunk", "renal_arteries", "celiac_trunk"),
        Need("aorta_true_lumen", "True lumen", "aortic_dissection", "aorta_true_lumen"),
        Need("aorta_false_lumen", "False lumen", "aortic_dissection", "aorta_false_lumen"),
    ),
    recipe=("total", "aorta_annulus", "renal_arteries", "aortic_dissection"),
    shape=shape,
    summary=summary,
)
