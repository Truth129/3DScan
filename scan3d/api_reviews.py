"""Interactive correction sessions: prompts, predictions and acceptance."""

from .api_models import InteractiveCreate, InteractiveDiscard, InteractiveGeneration, InteractiveNewStructure, InteractivePredict, InteractiveUpdate
from .store import public_job


def public_review(record):
    # review.py imports the imaging stack; load it on first use, not at startup.
    from .review import public_review as view

    return view(record)


def add_routes(app, service):
    @app.get("/api/interactive/status")
    def interactive_status():
        from .inference_paths import rows
        from .interactive import status

        capabilities = {"point": True, "bbox2d": True, "bbox3d": False, "scribble": True, "lasso": True, "initial_label": True}
        return {**status(), "capabilities": capabilities, "inference_paths": rows()}

    @app.get("/api/cases/{case_id}/reviews")
    def interactive_reviews(case_id: str):
        return [public_review(record) for record in service.reviews.list(case_id)]

    @app.post("/api/cases/{case_id}/reviews")
    def interactive_create(case_id: str, body: InteractiveCreate):
        return public_review(
            service.reviews.create(
                case_id,
                body.revision_id,
                body.structure_id,
                relationship=body.relationship.model_dump(exclude_none=True) if body.relationship else None,
                method=body.method,
            )
        )

    @app.post("/api/cases/{case_id}/reviews/new-structure")
    def interactive_new_structure(case_id: str, body: InteractiveNewStructure):
        return public_review(
            service.reviews.create_structure(
                case_id,
                body.revision_id,
                body.name,
                body.group,
                body.color,
                relationship=body.relationship.model_dump(exclude_none=True) if body.relationship else None,
                method=body.method,
            )
        )

    @app.get("/api/cases/{case_id}/reviews/{session_id}")
    def interactive_review(case_id: str, session_id: str):
        return public_review(service.reviews.get(case_id, session_id))

    @app.patch("/api/cases/{case_id}/reviews/{session_id}")
    def interactive_update(case_id: str, session_id: str, body: InteractiveUpdate):
        return public_review(
            service.reviews.update(
                case_id,
                session_id,
                body.expected_generation,
                [prompt.model_dump() for prompt in body.prompts],
                options=body.options.model_dump() if body.options else None,
                relationship=body.relationship.model_dump(exclude_none=True) if body.relationship else None,
            )
        )

    @app.post("/api/cases/{case_id}/reviews/{session_id}/predict")
    def interactive_predict(case_id: str, session_id: str, body: InteractivePredict):
        return public_job(service.reviews.predict(case_id, session_id, body.generation, full_refine=body.full_refine, inference_path=body.inference_path))

    @app.post("/api/cases/{case_id}/reviews/{session_id}/accept")
    def interactive_accept(case_id: str, session_id: str, body: InteractiveGeneration):
        return public_job(service.reviews.accept(case_id, session_id, body.generation))

    @app.post("/api/cases/{case_id}/reviews/{session_id}/discard")
    def interactive_discard(case_id: str, session_id: str, body: InteractiveDiscard):
        return public_review(service.reviews.discard(case_id, session_id, body.expected_generation))
