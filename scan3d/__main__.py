import argparse
import json
import logging
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="3DScan local CT anatomy pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=9010)
    sub.add_parser("doctor")
    sub.add_parser("demo")
    optimize = sub.add_parser("optimize", help="Prepare incremental viewer assets for an existing case without resegmenting")
    optimize.add_argument("case")
    include_all = sub.add_parser("include-all", help="Include all saved model labels in a new revision without rerunning inference")
    include_all.add_argument("case")
    imp = sub.add_parser("import")
    imp.add_argument("path")
    imp.add_argument("--label")
    process = sub.add_parser("process")
    process.add_argument("case")
    process.add_argument("--series", required=True)
    process.add_argument("--profile", choices=["chest", "abdomen", "chest_abdomen"], default="chest_abdomen")
    process.add_argument("--phase", required=True)
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
        uvicorn.run(create_app(), host=args.host, port=args.port, workers=1, proxy_headers=False, access_log=False)
        return
    if args.command == "doctor":
        from .doctor import diagnose
        print(json.dumps(diagnose(), indent=2))
        return
    from .service import Service
    from .store import public_case, public_job
    service = Service()
    if args.command == "demo":
        from .demo import create_demo
        print(json.dumps(public_case(create_demo(service)), indent=2))
    elif args.command == "import":
        print(json.dumps(public_case(service.import_local(args.path, args.label)), indent=2))
    elif args.command == "job":
        print(json.dumps(public_job(service.store.get("jobs", args.id)), indent=2))
    elif args.command == "optimize":
        print(json.dumps(service.optimize_assets(args.case), indent=2))
    else:
        if args.command == "process":
            job = service.process(args.case, args.series, args.profile, args.phase)
        elif args.command == "include-all":
            job = service.include_all_outputs(args.case)
        else:
            from .api import Refine
            payload = Refine(revision_id=args.revision, structure_id=args.structure,
                             operation=json.loads(args.recipe.read_text())).model_dump()
            service.store.revision(args.case, args.revision)
            job = service.store.enqueue(args.case, "refine", payload)
        print(f"Job {job['id']}", flush=True)
        service.start()
        try:
            result = service.wait(job["id"])
            print(json.dumps(public_job(result), indent=2))
            if result["status"] != "completed":
                raise SystemExit(1)
        finally:
            service.stop()


if __name__ == "__main__":
    main()
