"""Command line: run, serve, worker, doctor, recognition helpers and backup."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import shutil
import sqlite3
import sys
from pathlib import Path

from handprinter.config import Settings, get_settings


def _serve(settings: Settings) -> None:
    import uvicorn

    from handprinter.web.app import create_app

    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")


def _worker(settings: Settings) -> None:
    from handprinter import worker

    worker.main(settings)


def cmd_run(settings: Settings) -> int:
    """Start the worker as a child process, then the web server; stop both together."""
    if settings.lan_mode and len(settings.access_token) < 16:
        print("LAN mode needs HANDPRINTER_ACCESS_TOKEN (16+ characters).", file=sys.stderr)
        return 2
    proc = multiprocessing.Process(target=_worker, args=(settings,), name="handprinter-worker", daemon=False)
    proc.start()
    print(f"Handprinter: http://{settings.host}:{settings.port}  (data: {settings.data_dir}; worker pid {proc.pid})")
    try:
        _serve(settings)
    finally:
        proc.terminate()
        proc.join(timeout=15)
    return 0


def cmd_recognition(settings: Settings, args: argparse.Namespace) -> int:
    from handprinter.db import connect, init_db
    from handprinter.recognition import importer, package
    from handprinter.storage import Store

    store = Store(settings)
    conn = connect(settings.db_path)
    init_db(conn)
    if args.action == "pending":
        rows = package.pending_packages(conn)
        out = [{**r, "path": str(package.package_dir(store, r["id"]))} for r in rows]
        print(json.dumps(out, indent=2) if args.json else "\n".join(
            f"{r['id']}  pending {r['pending']}  failed {r['failed']}  model {r['model_configured']}  {r['path']}" for r in out) or "No pending recognition packages.")
        return 0
    if not args.package_id:
        print("package id required", file=sys.stderr)
        return 2
    if args.action == "validate":
        checks = importer.validate_package(store, args.package_id)
        ok = [c for c in checks if c.ok]
        bad = [c for c in checks if not c.ok]
        if args.json:
            print(json.dumps({"ok": [c.item_id for c in ok], "failed": {c.item_id: c.errors for c in bad}}, indent=2))
        else:
            print(f"{len(ok)} valid, {len(bad)} invalid or missing")
            for c in bad:
                print(f"  {c.item_id}: {'; '.join(c.errors)}")
        return 0 if not bad else 1
    if args.action == "import":
        print(json.dumps(importer.import_package(conn, store, args.package_id), indent=2))
        return 0
    return 2


def cmd_backup(settings: Settings, dest: Path) -> int:
    """Consistent copy: SQLite online backup plus every file in the data directory."""
    dest = dest.expanduser().resolve()
    if dest == settings.data_dir or settings.data_dir in dest.parents:
        print("backup destination must be outside the data directory", file=sys.stderr)
        return 2
    dest.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(settings.db_path)
    out = sqlite3.connect(dest / settings.db_path.name)
    with out:
        src.backup(out)
    src.close()
    out.close()
    for item in settings.data_dir.iterdir():
        if item.name.startswith(settings.db_path.name) or item.name in ("staging", "logs"):
            continue
        if item.is_dir():
            shutil.copytree(item, dest / item.name, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest / item.name)
    print(f"Backup written to {dest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="handprinter", description="Local handwriting dataset and generation studio")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="start web server and worker (the normal launch command)")
    sub.add_parser("serve", help="start only the web server")
    sub.add_parser("worker", help="start only the background worker")
    doc = sub.add_parser("doctor", help="report OS, Python, GPU, decoders, Claude CLI and adapters")
    doc.add_argument("--json", action="store_true")
    rec = sub.add_parser("recognition", help="recognition package helpers for Claude Code")
    rec.add_argument("action", choices=["pending", "validate", "import"])
    rec.add_argument("package_id", nargs="?")
    rec.add_argument("--json", action="store_true")
    bak = sub.add_parser("backup", help="copy the database and data files to a folder")
    bak.add_argument("dest", type=Path)
    args = parser.parse_args(argv)
    settings = get_settings()

    if args.command == "run":
        return cmd_run(settings)
    if args.command == "serve":
        _serve(settings)
        return 0
    if args.command == "worker":
        _worker(settings)
        return 0
    if args.command == "doctor":
        from handprinter import doctor

        data = doctor.report(settings)
        print(json.dumps(data, indent=2) if args.json else doctor.format_report(data))
        return 0
    if args.command == "recognition":
        return cmd_recognition(settings, args)
    if args.command == "backup":
        return cmd_backup(settings, args.dest)
    return 2
