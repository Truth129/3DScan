"""The aorta report: TotalSegmentator's aorta measurements, from a saved revision's masks.

Research use only, not for diagnostic use. `report_worker.aorta` measures with upstream's own code;
this module says which masks it needs and turns upstream's centimetre values into the result.
"""

from __future__ import annotations

from .reports import Definition, Need, area as _area, landmark, landmark_summary, mm as _mm, point as _point

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


def _lumen(upstream, suffix):
    return {
        "diameter_mm": _mm(upstream[f"diameter_{suffix}"]),
        "perpendicular_mm": _mm(upstream[f"diameter_{suffix}_pd"]),
        "area_cm2": _area(upstream[f"area_{suffix}"]),
    }


def shape(raw):
    """The result's report fields from the worker's output."""
    dissection = bool(raw["dissection"])
    landmarks = []
    for number, identifier, name in LANDMARKS:
        entry = {**landmark(number, identifier, name, raw, "aorta"), "true_lumen": None, "false_lumen": None}
        if entry["available"] and dissection:
            upstream = raw["upstream"]["landmarks"][str(number)]
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
    return landmark_summary(result, len(LANDMARKS))


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
