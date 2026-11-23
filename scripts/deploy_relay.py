"""Deploy (or update) the workers.dev relay with the Cloudflare API. See docs/online.md.

Needs CLOUDFLARE_API_TOKEN (Workers Scripts: Edit) and CLOUDFLARE_ACCOUNT_ID in the environment.
Stores the address in a Durable Object (no KV permission needed).

    python scripts/deploy_relay.py              # first time: makes a secret, prints the .env lines
    python scripts/deploy_relay.py --update     # later: new worker.js, keeps the existing secret
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.request
import uuid
from pathlib import Path

API = "https://api.cloudflare.com/client/v4"
NAME = "handprinter"
WORKER = Path(__file__).resolve().parents[1] / "relay" / "worker.js"


def call(method: str, path: str, body: bytes | None = None, content_type: str = "application/json") -> dict:
    req = urllib.request.Request(f"{API}{path}", data=body, method=method, headers={
        "authorization": f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}", "content-type": content_type,
        "user-agent": "handprinter-relay-deploy"})
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return json.load(res)
    except urllib.error.HTTPError as exc:
        return json.load(exc)


def multipart(parts: list[tuple[str, str, bytes]]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    out = b""
    for name, ctype, data in parts:
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{name}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n").encode() + data + b"\r\n"
    return out + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true", help="keep the existing secret and Durable Object")
    args = ap.parse_args()
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not account or not os.environ.get("CLOUDFLARE_API_TOKEN"):
        print("Set CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID first.", file=sys.stderr)
        return 2
    sub = call("GET", f"/accounts/{account}/workers/subdomain")
    if not sub.get("success"):
        print("Cannot read the workers.dev subdomain:", sub.get("errors"), file=sys.stderr)
        return 1
    secret = None
    meta: dict = {"main_module": "worker.js", "compatibility_date": "2024-09-01",
                  "bindings": [{"type": "durable_object_namespace", "name": "STATE", "class_name": "RelayState"}]}
    if args.update:
        meta["keep_bindings"] = ["secret_text"]
    else:
        secret = secrets.token_urlsafe(32)
        meta["bindings"].append({"type": "secret_text", "name": "RELAY_SECRET", "text": secret})
        meta["migrations"] = {"new_tag": "v1", "new_sqlite_classes": ["RelayState"]}
    body, ctype = multipart([("metadata", "application/json", json.dumps(meta).encode()),
                             ("worker.js", "application/javascript+module", WORKER.read_bytes())])
    up = call("PUT", f"/accounts/{account}/workers/scripts/{NAME}", body, ctype)
    if not up.get("success"):
        print("Upload failed:", up.get("errors"), file=sys.stderr)
        return 1
    call("POST", f"/accounts/{account}/workers/scripts/{NAME}/subdomain",
         json.dumps({"enabled": True, "previews_enabled": False}).encode())
    url = f"https://{NAME}.{sub['result']['subdomain']}.workers.dev"
    print(f"Relay deployed at {url}")
    if secret:
        print("\nPut these two lines in .env on the PC (keep the secret private):\n")
        print(f"HANDPRINTER_RELAY_URL={url}\nHANDPRINTER_RELAY_SECRET={secret}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
