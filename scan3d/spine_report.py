"""The spine fractures report: TotalSegmentator's heights, volume and density of each vertebral body.

Research use only, not for diagnostic use. `report_worker.spine` measures with upstream's own code
(`totalseg_spine_report`); this module says which masks it needs and turns upstream's numbers into
the result. Upstream's contrast phase classifier and front page are left out, so the result carries
its three density thresholds and the reader chooses the phase.
"""

from __future__ import annotations

from .reports import Definition, Need, point as _point

TASK = "vertebrae_pp_refined"
# Upstream's vertebrae_pp label ids, C1 = 1 to L5 = 24.
LEVELS = tuple(enumerate((*(f"C{n}" for n in range(1, 8)), *(f"T{n}" for n in range(1, 13)), *(f"L{n}" for n in range(1, 6))), start=1))
POSITIONS = ("posterior", "middle", "anterior")
_MEASURED = ("height_difference_percent", "height_flag", "volume_ml", "small_body", "mean_hu", "median_hu", "centre", "normal", "outline")


def _one(value):
    return None if value is None else round(float(value), 1)


def shape(raw):
    """The result's report fields from the worker's output."""
    upstream = raw["upstream"]
    vertebrae = []
    for number, name in LEVELS:
        identifier = f"vertebrae_{name}"
        geometry = raw["geometry"].get(identifier)
        if geometry is None and identifier not in raw["unavailable"]:
            continue  # not in the revision
        entry = {"number": number, "id": identifier, "name": name, "available": False, "unavailable_reason": None}
        entry["heights"] = [{"id": position, "mm": None, "ends": None} for position in POSITIONS]
        entry.update(dict.fromkeys(_MEASURED))
        if geometry is None:
            entry["unavailable_reason"] = raw["unavailable"][identifier]
            vertebrae.append(entry)
            continue
        for height in entry["heights"]:
            # The worker's unrounded height, rounded once as the picture prints it. None where upstream found
            # no line across the body, which its JSON gives as 0.
            value = geometry["heights"].get(height["id"])
            ends = geometry["ends"].get(height["id"])
            if value and ends:
                height.update(mm=_one(value), ends=[_point(end) for end in ends])
        # Upstream gives a difference of 0 with fewer than two lines, which is not a measured difference.
        lines = sum(height["mm"] is not None for height in entry["heights"])
        mean, median = upstream[f"{identifier}_intensity"], upstream[f"{identifier}_intensity_median"]
        # Upstream writes both 0.0 when its 4 mm eroded body is empty: not measured.
        empty = mean == 0.0 and median == 0.0
        entry.update(
            available=True,
            height_difference_percent=round(upstream[f"{identifier}_height_difference"] * 100) if lines >= 2 else None,
            height_flag=bool(geometry["height_flag"]),
            volume_ml=_one(upstream[f"{identifier}_volume_ml"]),
            small_body=bool(geometry["small_body"]),
            mean_hu=None if empty else _one(mean),
            median_hu=None if empty else _one(median),
            centre=_point(geometry["centre"]),
            normal=[round(float(value), 4) for value in geometry["normal"]],
            outline=[_point(value) for value in geometry["outline"]],
        )
        vertebrae.append(entry)
    lumbar = upstream.get("LWS_max")
    return {
        "vertebrae": vertebrae,
        "lumbar_max_difference_percent": None if lumbar is None else round(lumbar * 100),
        "density_thresholds_hu": dict(raw["thresholds"]),
        "warnings": list(raw["warnings"]),
    }


def summary(result):
    measured = [entry for entry in result["vertebrae"] if entry["available"]]
    differing = [entry for entry in measured if entry["height_difference_percent"] is not None]
    largest = max(differing, key=lambda entry: entry["height_difference_percent"], default=None)
    return {
        "vertebrae_measured": len(measured),
        "vertebrae_total": len(result["vertebrae"]),
        "flagged": [entry["name"] for entry in measured if entry["height_flag"]],
        "max_difference_percent": largest and largest["height_difference_percent"],
        "max_vertebra": largest and largest["name"],
        "lumbar_max_difference_percent": result["lumbar_max_difference_percent"],
    }


DEFINITION = Definition(
    id="spine",
    title="Spine fractures",
    engine="TotalSegmentator spine report",
    module="totalsegmentator.spine_report.measure_verte_height",
    # Bump when the worker or this module changes a number or a picture, so saved results are not reused.
    version=1,
    needs=tuple(Need(f"vertebrae_{name}", f"{name} vertebral body", TASK, f"vertebrae_{name}") for _, name in LEVELS),
    recipe=(TASK,),
    shape=shape,
    summary=summary,
    # Upstream measures each body it has, so any one of them gives a report.
    any_of=tuple(f"vertebrae_{name}" for _, name in LEVELS),
    any_of_reason="No vertebral body was found",
    any_of_title="Vertebral bodies",
)
