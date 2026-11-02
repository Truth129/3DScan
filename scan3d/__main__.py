import argparse
import json
import logging
import os
from pathlib import Path


def new_password():
    # From a prompt only: a flag or variable would leave it in shell history or the process list.
    import getpass
    from .identity import check_password

    password = getpass.getpass("New password: ")
    if password != getpass.getpass("Repeat the password: "):
        raise SystemExit("The passwords do not match")
    check_password(password)
    return password


def manage_users(args):
    # Not through Service, so this runs beside a live server and loads no imaging or worker code.
    from contextlib import closing
    import sqlite3
    from .accounts import Accounts
    from .backup import DATABASE
    from .config import Settings
    from .errors import NotFound
    from .identity import check_username
    from .store import Store

    try:
        data_dir = Settings.from_env().data_dir
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    # Store creates what it opens, so a mistyped folder would otherwise pass for an empty one.
    if args.action != "add" and not (data_dir / DATABASE).is_file():
        raise SystemExit(f"There is no 3DScan database in {data_dir}. Check SCAN3D_DATA_DIR, or add the first account.")
    try:
        password = None
        if args.action == "add":
            # Before Store creates the database, so a refused add leaves nothing behind.
            check_username(args.name)
            password = new_password()
        with closing(Store(data_dir)) as store:
            accounts = Accounts(store)
            if args.action == "add":
                user_id = accounts.add_user(args.name, password, args.display_name)
                print(f"Added {args.name} ({user_id}) in {data_dir}. Cases given to the oldest admin: {accounts.adopt_orphans()}")
            elif args.action == "passwd":
                accounts.require_user(args.name)
                accounts.set_password(args.name, new_password())
                print(f"Changed the password of {args.name} and signed it out everywhere")
            else:
                for user in accounts.users():
                    print(user["id"], user["username"], user["role"], "disabled" if user["disabled_at"] else "active", sep="\t")
    except (NotFound, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("Nothing was changed") from None
    except (OSError, sqlite3.Error) as exc:
        raise SystemExit(f"Cannot use {data_dir}: {exc}") from None


def main():
    from .acquisition import CARDIAC_PHASE_IDS, PROFILE_IDS
    from .config import DEFAULT_HOST, DEFAULT_PORT, load_environment

    # Case files hold patient data; everything this process writes stays private to the user.
    os.umask(0o077)
    load_environment()
    parser = argparse.ArgumentParser(description="3DScan local CT anatomy pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default=os.environ.get("SCAN3D_HOST", DEFAULT_HOST))
    serve.add_argument("--port", type=int, default=int(os.environ.get("SCAN3D_PORT", DEFAULT_PORT)))
    sub.add_parser("doctor")
    backup = sub.add_parser("backup", help="Snapshot the database and case files into DESTINATION/scan3d-<UTC time>")
    backup.add_argument("destination", type=Path)
    users = sub.add_parser("users", help="Manage sign-in accounts")
    actions = users.add_subparsers(dest="action", required=True)
    add_user = actions.add_parser("add", help="Create an account, which also takes any case with no owner")
    add_user.add_argument("name")
    # Required for now, so that a plain add can mean a member later without changing what it does today.
    add_user.add_argument("--admin", action="store_true", required=True, help="Every account is an administrator for now")
    add_user.add_argument("--display-name")
    passwd = actions.add_parser("passwd", help="Set a new password, which ends the account's sessions")
    passwd.add_argument("name")
    actions.add_parser("list", help="List accounts, without password hashes")
    sub.add_parser("demo")
    optimize = sub.add_parser("optimize", help="Prepare incremental viewer assets for an existing case without resegmenting")
    optimize.add_argument("case")
    include_all = sub.add_parser("include-all", help="Include all saved model labels in a new revision without rerunning inference")
    include_all.add_argument("case")
    imp = sub.add_parser("import")
    imp.add_argument("path")
    imp.add_argument("--label")
    process = sub.add_parser("process", help="Prepare a selected CT series for viewing without segmentation")
    process.add_argument("case")
    process.add_argument("--series", required=True)
    process.add_argument(
        "--profile", choices=PROFILE_IDS, default="chest_abdomen", help="Scan region/study label; does not crop the CT or select inference tasks"
    )
    process.add_argument("--phase", required=True)
    process.add_argument("--cardiac-phase", choices=CARDIAC_PHASE_IDS, help="Optional cardiac reconstruction timing label")
    process.add_argument("--rr-percent", type=float, help="Optional cardiac reconstruction position within the R–R interval (0–100)")
    segment = sub.add_parser("segment", help="Run a selected TotalSegmentator task on a prepared CT")
    segment.add_argument("case")
    segment.add_argument("--revision", help="Input revision (defaults to the active revision)")
    segment.add_argument("--task", help="Model task (default: total for CT, total_mr for MRI)")
    segment.add_argument("--robust-crop", action="store_true")
    segment.add_argument("--body-seg", action="store_true")
    segment.add_argument("--remove-small-blobs", action="store_true")
    segment.add_argument("--inference-path", help="An inference path id from doctor (default: the default path)")
    refine = sub.add_parser("refine")
    refine.add_argument("case")
    refine.add_argument("--revision", required=True)
    refine.add_argument("--structure", required=True)
    refine.add_argument("--recipe", type=Path, required=True)
    status = sub.add_parser("job")
    status.add_argument("id")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.command == "serve":
        import uvicorn
        from .api import create_app

        # The sign-in mode depends on the address this listens on, however that was chosen.
        os.environ["SCAN3D_HOST"] = args.host
        uvicorn.run(create_app(), host=args.host, port=args.port, workers=1, proxy_headers=False, access_log=False)
        return
    if args.command == "doctor":
        from .doctor import diagnose
        from .inference_paths import rows

        print(json.dumps({**diagnose(), "inference_paths": rows()}, indent=2))
        return
    if args.command == "backup":
        # Only reads the live database and files, so it is safe while the service runs.
        from .backup import backup as write_backup
        from .config import Settings

        print(json.dumps(write_backup(Settings.from_env().data_dir, args.destination), indent=2))
        return
    if args.command == "users":
        manage_users(args)
        return
    from .service import Service
    from .store import public_case, public_job

    service = Service()
    if args.command == "demo":
        from .demo import create_demo

        print(json.dumps(public_case(create_demo(service), service.settings), indent=2))
    elif args.command == "import":
        print(json.dumps(public_case(service.import_local(args.path, args.label), service.settings), indent=2))
    elif args.command == "job":
        print(json.dumps(public_job(service.store.get("jobs", args.id)), indent=2))
    elif args.command == "optimize":
        print(json.dumps(service.optimize_assets(args.case), indent=2))
    else:
        if args.command == "process":
            job = service.process(args.case, args.series, args.profile, args.phase, cardiac_phase=args.cardiac_phase, rr_percent=args.rr_percent)
        elif args.command == "segment":
            revision_id = args.revision or service.store.get("cases", args.case).get("active_revision_id")
            if not revision_id:
                raise ValueError("Prepare a CT with process before requesting segmentation")
            job = service.segment_case(args.case, revision_id, args.task, args.robust_crop, args.body_seg, args.remove_small_blobs, args.inference_path)
        elif args.command == "include-all":
            job = service.include_all_outputs(args.case)
        else:
            from .api import Refine

            payload = Refine(revision_id=args.revision, structure_id=args.structure, operation=json.loads(args.recipe.read_text())).model_dump()
            service.store.revision(args.case, args.revision)
            job = service.store.enqueue(args.case, "refine", payload)
        print(f"Job {job['id']}", flush=True)
        from .store import job_lane

        # Only this job's lane: stopping after it must not cancel other work.
        service.start(lanes=(job_lane(job),))
        try:
            result = service.wait(job["id"])
            print(json.dumps(public_job(result), indent=2))
            if result["status"] != "completed":
                raise SystemExit(1)
        finally:
            service.stop()


if __name__ == "__main__":
    main()
