"""The pulmonary artery report: TotalSegmentator's five landmark planes on the pulmonary trunk and its branches.

Research use only, not for diagnostic use. `report_worker.pulmonary` measures with upstream's own
code; this module says which masks it needs and turns upstream's centimetre values into the result.
Upstream has no centreline, sections, curved view, contrast handling or lumens for this report.
"""

from __future__ import annotations

from .reports import Definition, Need, landmark, landmark_summary

LANDMARKS = (
    (1, "proximal_main", "Proximal main pulmonary artery"),
    (2, "sinotubular_junction", "Sinotubular junction"),
    (3, "bifurcation", "Bifurcation"),
    (4, "left", "Left pulmonary artery"),
    (5, "right", "Right pulmonary artery"),
)


def shape(raw):
    """The result's report fields from the worker's output."""
    return {
        "landmarks": [landmark(number, identifier, name, raw, "pulmonary artery") for number, identifier, name in LANDMARKS],
        "warnings": list(raw["warnings"]),
    }


def summary(result):
    return landmark_summary(result, len(LANDMARKS))


DEFINITION = Definition(
    id="pulmonary",
    title="Pulmonary arteries",
    engine="TotalSegmentator pulmonary artery report",
    module="totalsegmentator.pulmonary_report",
    # Bump when the worker or this module changes a number or a picture, so saved results are not reused.
    version=1,
    needs=(
        Need("pulmonary_artery", "Pulmonary artery", "heartchambers_highres", "pulmonary_artery", True),
        Need("pul_annulus", "Pulmonary valve annulus", "pulmonary_artery_landmarks", "pul_annulus"),
        Need("pul_sinotubular_junction", "Pulmonary sinotubular junction", "pulmonary_artery_landmarks", "pul_sinotubular_junction"),
        Need("pul_bifurcation", "Pulmonary bifurcation", "pulmonary_artery_landmarks", "pul_bifurcation"),
        Need("pul_left_start", "Start of the left pulmonary artery", "pulmonary_artery_landmarks", "pul_left_start"),
        Need("pul_right_start", "Start of the right pulmonary artery", "pulmonary_artery_landmarks", "pul_right_start"),
    ),
    recipe=("heartchambers_highres", "pulmonary_artery_landmarks"),
    shape=shape,
    summary=summary,
    # Upstream measures each landmark it has and leaves the rest empty, so any one of them gives a report.
    any_of=("pul_annulus", "pul_sinotubular_junction", "pul_bifurcation", "pul_left_start", "pul_right_start"),
    any_of_reason="None of the five pulmonary landmarks was found",
)
