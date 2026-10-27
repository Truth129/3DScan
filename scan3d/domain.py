"""Closed domain values with stable strings at storage and API boundaries."""

from enum import StrEnum, unique


@unique
class Modality(StrEnum):
    CT = "CT"
    MR = "MR"
    DX = "DX"
    CR = "CR"
    MG = "MG"
    US = "US"
    XA = "XA"
    RF = "RF"
    PT = "PT"
    NM = "NM"
    OT = "OT"


@unique
class ImageKind(StrEnum):
    IMAGE2D = "image2d"
    STACK = "stack"
    CINE = "cine"
    VOLUME3D = "volume3d"


@unique
class ImageCapability(StrEnum):
    MPR = "mpr"
    VOLUME_RENDERING = "volume_rendering"
    SEGMENTATION = "segmentation"
    VOXEL_MEASUREMENTS = "voxel_measurements"


@unique
class PhotometricInterpretation(StrEnum):
    MONOCHROME1 = "MONOCHROME1"
    MONOCHROME2 = "MONOCHROME2"


@unique
class VolumeQuality(StrEnum):
    PREVIEW = "preview"
    FULL = "full"


@unique
class StudyProfile(StrEnum):
    CHEST = "chest"
    ABDOMEN = "abdomen"
    CHEST_ABDOMEN = "chest_abdomen"
    CORCTA = "corcta"
    CARDIAC = "cardiac"
    CHEST_ABDOMEN_PELVIS = "chest_abdomen_pelvis"
    ABDOMEN_PELVIS = "abdomen_pelvis"
    PELVIS = "pelvis"
    HEAD_NECK = "head_neck"
    BREAST = "breast"
    WHOLE_BODY = "whole_body"
    OTHER = "other"
    UNKNOWN = "unknown"


@unique
class CardiacPhase(StrEnum):
    UNKNOWN = "unknown"
    BEST_DIASTOLIC = "best_diastolic"
    BEST_SYSTOLIC = "best_systolic"
    DIASTOLIC = "diastolic"
    SYSTOLIC = "systolic"
    OTHER = "other"
