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
    print(f"Handprinter: http://{settings.host}:{settings.port}  (data: {settings.data_dir}; worker pid {proc.pid})"
          + ("  [online: password required]" if settings.online else ""))
    try:
        _serve(settings)
    finally:
        proc.terminate()
        proc.join(timeout=15)
    return 0


def cmd_online(settings: Settings) -> int:
    """Run the app and publish it through a Cloudflare Tunnel, behind the sign-in password."""
    import os

    from handprinter import tunnel

    binary = tunnel.find_cloudflared()
    if not binary:
        print("cloudflared is not installed. On Windows run:  winget install --id Cloudflare.cloudflared\n"
              "then open a new terminal and start this again. Details: docs/online.md", file=sys.stderr)
        return 2
    if not settings.password:
        print("Set HANDPRINTER_PASSWORD in .env first.", file=sys.stderr)
        return 2
    token = os.environ.get("HANDPRINTER_TUNNEL_TOKEN") or _dotenv_value("HANDPRINTER_TUNNEL_TOKEN")
    public = os.environ.get("HANDPRINTER_PUBLIC_URL") or _dotenv_value("HANDPRINTER_PUBLIC_URL")

    def show(url: str) -> None:
        bar = "=" * (len(url) + 8)
        print(f"\n{bar}\n    {url}\n{bar}\nOpen this on your iPad or phone and sign in with your password."
              f"\nKeep this window open: closing it takes the site offline.\n", flush=True)

    t = tunnel.Tunnel(settings, binary, token, public, show)
    t.start()
    print("Starting the Cloudflare tunnel (the address appears in a few seconds)...", flush=True)
    try:
        return cmd_run(settings)
    finally:
        t.stop()


def _dotenv_value(key: str) -> str | None:
    from handprinter.config import _load_dotenv

    return _load_dotenv(Path.cwd() / ".env").get(key) or None


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
    if args.action == "show":
        items = package.item_readings(conn, args.package_id)
        if args.json:
            print(json.dumps(items, indent=2, ensure_ascii=False))
        else:
            for it in items:
                read = it["text"] if it["text"] is not None else ("(page layout)" if it["context_type"] == "page_overview" else "(nothing read)")
                print(f"{it['item_id']}  {it['status']:<9}  {it['page'] or '?'}  {read}{'  [needs a look]' if it['flags'] else ''}")
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
    sub.add_parser("online", help="run, and publish it online through a Cloudflare Tunnel behind a password")
    sub.add_parser("serve", help="start only the web server")
    sub.add_parser("worker", help="start only the background worker")
    doc = sub.add_parser("doctor", help="report OS, Python, GPU, decoders, Claude CLI and adapters")
    doc.add_argument("--json", action="store_true")
    rec = sub.add_parser("recognition", help="recognition package helpers for Claude Code")
    rec.add_argument("action", choices=["pending", "validate", "import", "show"])
    rec.add_argument("package_id", nargs="?")
    rec.add_argument("--json", action="store_true")
    bak = sub.add_parser("backup", help="copy the database and data files to a folder")
    bak.add_argument("dest", type=Path)
    args = parser.parse_args(argv)
    if args.command == "online":
        import os

        os.environ["HANDPRINTER_ONLINE"] = "1"
        os.environ["HANDPRINTER_HOST"] = "127.0.0.1"  # only the tunnel reaches the server
        get_settings.cache_clear()
    settings = get_settings()
    if args.command == "online":
        return cmd_online(settings)

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
