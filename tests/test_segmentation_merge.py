"""Anatomical name replacement changes metadata, never mask voxel content."""

from copy import deepcopy

import pytest

from scan3d.segmentation_merge import merge_task_outputs


def structure(task, label, *, name=None, relationship=None):
    entry = {
        "id": f"{task}:{label}",
        "task": task,
        "name": name if name is not None else label.replace("_", " ").capitalize(),
        "mask_file": f"/{task}/{label}.nii.gz",
        "volume_ml": 12.5,
    }
    if relationship is not None:
        entry["relationship"] = relationship
    return entry


def test_cross_task_replacement_uses_exact_anatomical_names_and_preserves_unmatched():
    previous = [structure("total", "liver"), structure("total", "spleen"), structure("total", "vertebrae_S1")]
    incoming = [structure("total_v3", "LIVER"), structure("total_v3", "vertebrae_L6")]
    before, outputs = deepcopy(previous), deepcopy(incoming)
    merged, operation = merge_task_outputs(previous, incoming, ["total_v3"])
    assert {entry["id"] for entry in merged} == {"total:spleen", "total:vertebrae_S1", "total_v3:LIVER", "total_v3:vertebrae_L6"}
    assert operation == {
        "replacement_policy": "same_task_or_exact_name_v1",
        "replaced_structure_ids": ["total:liver"],
        "replacement_id_map": {"total:liver": "total_v3:LIVER"},
    }
    assert previous == before and incoming == outputs
    assert next(entry for entry in merged if entry["id"] == "total:spleen") == previous[1]


def test_installed_total_and_v3_catalogs_replace_all_116_common_labels():
    # Registry metadata alone: no model import, weight download or inference.
    from totalsegmentator.registry import get_task_classes

    total = set(get_task_classes("total").values())
    v3 = set(get_task_classes("total_v3").values())
    assert len(total) == len(v3) == 117
    assert total - v3 == {"vertebrae_S1"} and v3 - total == {"vertebrae_L6"}
    previous = [structure("total", label) for label in sorted(total)]
    incoming = [structure("total_v3", label) for label in sorted(v3)]
    merged, operation = merge_task_outputs(previous, incoming, ["total_v3"])
    assert len(merged) == 118
    assert {entry["id"] for entry in merged if entry["task"] == "total"} == {"total:vertebrae_S1"}
    assert {entry["id"] for entry in merged if entry["task"] == "total_v3"} == {entry["id"] for entry in incoming}
    assert len(operation["replacement_id_map"]) == len(operation["replaced_structure_ids"]) == 116
    assert operation["replacement_id_map"] == {f"total:{label}": f"total_v3:{label}" for label in total & v3}


def test_native_label_identity_and_custom_display_name_normalize_only_case_and_spacing():
    previous = [
        structure("total", "lung_upper_lobe_left", name="Renamed by reviewer"),
        structure("custom", "user-created-id", name="  Lung_upper   Lobe_RIGHT "),
    ]
    incoming = [structure("total_v3", "lung_upper_lobe_left"), structure("total_v3", "lung_upper_lobe_right")]
    merged, operation = merge_task_outputs(previous, incoming, ["total_v3"])
    assert {entry["id"] for entry in merged} == {entry["id"] for entry in incoming}
    assert operation["replacement_id_map"] == {
        "total:lung_upper_lobe_left": "total_v3:lung_upper_lobe_left",
        "custom:user-created-id": "total_v3:lung_upper_lobe_right",
    }


def test_similar_names_and_opposite_laterality_are_never_fuzzy_matched():
    previous = [structure("total", "heart"), structure("total", "lung_upper_lobe_left"), structure("custom", "lesion", name="Heart lesion")]
    incoming = [
        structure("heartchambers_highres", "heart_atrium_right"),
        structure("total_v3", "lung_lower_lobe_left"),
        structure("total_v3", "lung_upper_lobe_right"),
    ]
    merged, operation = merge_task_outputs(previous, incoming, ["heartchambers_highres", "total_v3"])
    assert len(merged) == 6 and not operation["replacement_id_map"]
    assert not operation["replaced_structure_ids"]


def test_same_task_removes_omitted_outputs_and_does_not_fabricate_replacements():
    previous = [structure("total", "liver"), structure("total", "spleen"), structure("lung_vessels", "lung_arteries")]
    incoming = [structure("total", "liver")]
    merged, operation = merge_task_outputs(previous, incoming, ["total"])
    assert {entry["id"] for entry in merged} == {"total:liver", "lung_vessels:lung_arteries"}
    assert set(operation["replaced_structure_ids"]) == {"total:liver", "total:spleen"}
    assert operation["replacement_id_map"] == {"total:liver": "total:liver"}


def test_multiple_existing_aliases_collapse_to_one_incoming_segment():
    previous = [structure("total", "liver"), structure("custom", "manual-liver", name="LIVER")]
    incoming = [structure("total_v3", "liver")]
    merged, operation = merge_task_outputs(previous, incoming, ["total_v3"])
    assert len(merged) == 1 and merged[0]["id"] == "total_v3:liver"
    assert operation["replacement_id_map"] == {"total:liver": "total_v3:liver", "custom:manual-liver": "total_v3:liver"}
    assert set(operation["replaced_structure_ids"]) == {"total:liver", "custom:manual-liver"}


def test_preserved_and_inherited_relationships_are_remapped_to_new_ids():
    previous = [
        structure("total", "lung_upper_lobe_left", relationship={"kind": "subdivision", "exclusive_with": ["total:lung_lower_lobe_left"]}),
        structure("total", "lung_lower_lobe_left", relationship={"kind": "subdivision", "exclusive_with": ["total:lung_upper_lobe_left"]}),
        structure(
            "custom", "subregion", name="Upper lobe lesion", relationship={"kind": "subregion", "parent_id": "total:lung_upper_lobe_left", "exclusive_with": []}
        ),
    ]
    incoming = [structure("total_v3", "lung_upper_lobe_left"), structure("total_v3", "lung_lower_lobe_left")]
    before = deepcopy(previous)
    merged, _ = merge_task_outputs(previous, incoming, ["total_v3"])
    by_id = {entry["id"]: entry for entry in merged}
    assert by_id["custom:subregion"]["relationship"] == {"kind": "subregion", "parent_id": "total_v3:lung_upper_lobe_left", "exclusive_with": []}
    assert by_id["total_v3:lung_upper_lobe_left"]["relationship"] == {"kind": "subdivision", "exclusive_with": ["total_v3:lung_lower_lobe_left"]}
    assert by_id["total_v3:lung_lower_lobe_left"]["relationship"] == {"kind": "subdivision", "exclusive_with": ["total_v3:lung_upper_lobe_left"]}
    assert previous == before


def test_conflicting_relationships_for_existing_aliases_are_rejected_without_mutation():
    previous = [
        structure("total", "liver", relationship={"kind": "independent", "exclusive_with": []}),
        structure("custom", "manual-liver", name="Liver", relationship={"kind": "subregion", "parent_id": "total:spleen", "exclusive_with": []}),
        structure("total", "spleen"),
    ]
    before = deepcopy(previous)
    with pytest.raises(ValueError, match="(?i)(conflict|ambiguous)"):
        merge_task_outputs(previous, [structure("total_v3", "liver")], ["total_v3"])
    assert previous == before


def test_duplicate_incoming_names_cannot_select_a_silent_winner():
    previous = [structure("total", "liver")]
    incoming = [structure("total_v3", "liver"), structure("other_task", "LIVER")]
    before, outputs = deepcopy(previous), deepcopy(incoming)
    with pytest.raises(ValueError, match="(?i)(ambiguous|duplicate|same.*name)"):
        merge_task_outputs(previous, incoming, ["total_v3", "other_task"])
    assert previous == before and incoming == outputs


@pytest.mark.parametrize(
    "relationship",
    [
        {"kind": "subregion", "parent_id": "total:liver", "exclusive_with": []},
        {"kind": "subdivision", "exclusive_with": ["total:liver"]},
    ],
)
def test_alias_replacement_cannot_make_self_referential_relationship(relationship):
    previous = [structure("total", "liver"), structure("custom", "alias", name="Liver", relationship=relationship)]
    with pytest.raises(ValueError, match="(?i)(self|parent|sibling|relationship|conflict)"):
        merge_task_outputs(previous, [structure("total_v3", "liver")], ["total_v3"])


def test_omitted_same_task_parent_cannot_leave_dangling_custom_relationship():
    previous = [
        structure("total", "liver"),
        structure("total", "spleen"),
        structure("custom", "child", name="Liver finding", relationship={"kind": "subregion", "parent_id": "total:liver", "exclusive_with": []}),
    ]
    with pytest.raises(ValueError, match="(?i)(relationship|parent|missing|omit)"):
        merge_task_outputs(previous, [structure("total", "spleen")], ["total"])


def test_alias_collapse_cannot_create_a_parent_cycle():
    # The original chain is liver -> spleen -> manual alias, with no cycle.
    # Replacing both liver aliases by one ID would close the chain into a cycle.
    previous = [
        structure("total", "liver", relationship={"kind": "subregion", "parent_id": "total:spleen", "exclusive_with": []}),
        structure("total", "spleen", relationship={"kind": "subregion", "parent_id": "custom:alias", "exclusive_with": []}),
        structure("custom", "alias", name="Liver"),
    ]
    with pytest.raises(ValueError, match="(?i)(cycle|relationship)"):
        merge_task_outputs(previous, [structure("total_v3", "liver")], ["total_v3"])
