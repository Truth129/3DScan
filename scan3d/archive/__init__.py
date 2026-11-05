"""Archive access beyond Orthanc's REST API: DICOMweb import and export, and direct DICOM sends.

The Orthanc REST path stays in the ``orthanc_*`` modules. Keep this file free of imports: GPU
child processes import ``scan3d.config``, which reads archive settings, and must not pull in
the archive stack.
"""
