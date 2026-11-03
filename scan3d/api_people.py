"""People: the accounts an administrator adds, changes and signs out. Only with sign-in on."""

from . import identity
from .api_models import NewPerson, PersonArchive, PersonRole


def add_routes(app, service):
    if not service.settings.sign_in_required:
        # With sign-in off there is one built-in person, and nobody to add.
        return
    accounts = service.accounts

    @app.get("/api/people")
    def people():
        return accounts.people()

    @app.post("/api/people")
    def add_person(body: NewPerson):
        return accounts.invite(identity.actor_id(), body.username, body.display_name, body.role)

    @app.post("/api/people/{user_id}/link")
    def new_link(user_id: str):
        return accounts.new_link(identity.actor_id(), user_id)

    @app.post("/api/people/{user_id}/role")
    def set_role(user_id: str, body: PersonRole):
        return accounts.set_role(identity.actor_id(), user_id, body.role)

    @app.post("/api/people/{user_id}/archive")
    def set_archive(user_id: str, body: PersonArchive):
        return accounts.set_archive(identity.actor_id(), user_id, body.archive_access)

    @app.post("/api/people/{user_id}/disable")
    def disable(user_id: str):
        return accounts.disable(identity.actor_id(), user_id)

    @app.post("/api/people/{user_id}/enable")
    def enable(user_id: str):
        return accounts.enable(identity.actor_id(), user_id)

    @app.post("/api/people/{user_id}/sign-out")
    def sign_out_everywhere(user_id: str):
        return accounts.sign_out_everywhere(identity.actor_id(), user_id)
