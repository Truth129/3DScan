"""Workstation health, capability catalogs and the TotalSegmentator license."""

from fastapi import HTTPException, Response

from . import __version__
from .api_models import TotalSegmentatorLicense
from .domain import Modality


def add_routes(app, service):
    @app.get("/api/health")
    def health():
        from .doctor import diagnose

        lanes = {lane: thread.is_alive() for lane, thread in service.threads.items()}
        return {
            "status": "ok",
            "worker": bool(lanes) and all(lanes.values()),
            "lanes": lanes,
            "modalities": list(service.settings.modalities),
            "gpu": diagnose(live=False),
            "version": __version__,
        }

    @app.get("/api/segmentation-options")
    def segmentation_options(modality: Modality = Modality.CT):
        from .inference_paths import rows
        from .segmentation_options import segmentation_catalog

        return {**segmentation_catalog(modality), "inference_paths": rows()}

    @app.get("/api/imaging-capabilities")
    def imaging_capabilities():
        from .modalities import modality_capabilities

        capabilities = modality_capabilities()
        return {**capabilities, "modalities": [{**entry, "enabled": entry["modality"] in service.settings.modalities} for entry in capabilities["modalities"]]}

    @app.get("/api/totalsegmentator/license")
    def total_segmentator_license_status(response: Response):
        from .totalsegmentator_license import LicenseError, license_status

        response.headers["Cache-Control"] = "no-store"
        try:
            return license_status()
        except LicenseError as exc:
            raise HTTPException(exc.status_code, str(exc), headers={"Cache-Control": "no-store"}) from None

    def change_total_segmentator_license(change):
        from .totalsegmentator_license import LicenseError

        # TotalSegmentator rewrites its config during inference. Keep license
        # changes outside all queued/running work; the write transaction also
        # prevents a worker claim or new enqueue while validation is in flight.
        with service.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running') LIMIT 1").fetchone():
                raise HTTPException(409, "Wait for queued and running jobs to finish before changing the license.", headers={"Cache-Control": "no-store"})
            try:
                return change()
            except LicenseError as exc:
                raise HTTPException(exc.status_code, str(exc), headers={"Cache-Control": "no-store"}) from None

    @app.put("/api/totalsegmentator/license")
    def set_total_segmentator_license(body: TotalSegmentatorLicense, response: Response):
        from .totalsegmentator_license import save_license

        response.headers["Cache-Control"] = "no-store"
        return change_total_segmentator_license(lambda: save_license(body.license_key.get_secret_value()))

    @app.delete("/api/totalsegmentator/license")
    def delete_total_segmentator_license(response: Response):
        from .totalsegmentator_license import remove_license

        response.headers["Cache-Control"] = "no-store"
        return change_total_segmentator_license(remove_license)
