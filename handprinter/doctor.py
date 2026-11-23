"""`python -m handprinter doctor`: report the local machine so hardware logic never assumes."""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from handprinter import imaging
from handprinter import tunnel
from handprinter.adapters import all_adapters
from handprinter.config import Settings
from handprinter.recognition.headless import cli_status
from handprinter.recognition.package import REPO_ROOT


def gpu_info() -> dict[str, Any]:
    info: dict[str, Any] = {"nvidia_smi": None, "torch_cuda": None}
    exe = shutil.which("nvidia-smi")
    if exe:
        try:
            out = subprocess.run(
                [exe, "--query-gpu=name,memory.total,memory.free,driver_version", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=15,
            ).stdout.strip()
            info["nvidia_smi"] = [
                dict(zip(("name", "vram_total_mb", "vram_free_mb", "driver"), [p.strip() for p in line.split(",")]))
                for line in out.splitlines() if line.strip()
            ]
        except (OSError, subprocess.TimeoutExpired) as exc:
            info["nvidia_smi"] = f"failed: {exc.__class__.__name__}"
    try:
        import torch  # optional in the app env; normally lives in adapter envs

        if torch.cuda.is_available():
            p = torch.cuda.get_device_properties(0)
            info["torch_cuda"] = {"name": p.name, "vram_total_mb": round(p.total_memory / 2**20), "torch": torch.__version__}
        else:
            info["torch_cuda"] = "torch installed, CUDA unavailable"
    except ImportError:
        info["torch_cuda"] = "torch not installed in the app environment (expected; adapters use their own)"
    return info


def recogniser_agent_model() -> str | None:
    path = REPO_ROOT / ".claude" / "agents" / "handwriting-recogniser.md"
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        if line.strip() == "---":
            break
        if line.startswith("model:"):
            return line.split(":", 1)[1].strip()
    return None


def report(settings: Settings) -> dict[str, Any]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(settings.data_dir)
    agent_model = recogniser_agent_model()
    return {
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "python": sys.version.split()[0],
        "python_ok": sys.version_info >= (3, 11),
        "data_dir": str(settings.data_dir),
        "disk_free_gb": round(usage.free / 1e9, 1),
        "gpu": gpu_info(),
        "decoders": {"heic": imaging.heif_available(), "pdf": imaging.pdf_available()},
        "claude_cli": cli_status(),
        "recognition": {
            "configured_model": settings.recognition_model,
            "subagent_model": agent_model,
            "consistent": agent_model == settings.recognition_model,
            "headless_enabled": settings.headless_recognition,
        },
        "adapters": {name: a.status().as_dict() for name, a in all_adapters(settings.adapter_env).items()},
        "network": {"host": settings.host, "lan_mode": settings.lan_mode, "access_token_set": bool(settings.access_token),
                    "online": settings.online, "public_url": (tunnel.read_state(settings) or {}).get("url") if settings.online else None,
                    "relay": (tunnel.read_state(settings) or {}).get("relay") if settings.online else None,
                    "cloudflared": tunnel.find_cloudflared()},
    }


def format_report(data: dict[str, Any]) -> str:
    def yn(v: bool) -> str:
        return "yes" if v else "no"

    lines = [
        f"OS:             {data['os']}",
        f"Python:         {data['python']} ({'ok' if data['python_ok'] else 'needs 3.11+'})",
        f"Data dir:       {data['data_dir']} ({data['disk_free_gb']} GB free)",
    ]
    smi = data["gpu"]["nvidia_smi"]
    if isinstance(smi, list) and smi:
        for g in smi:
            lines.append(f"GPU:            {g['name']} — {g['vram_total_mb']} MB VRAM ({g['vram_free_mb']} MB free), driver {g['driver']}")
    else:
        lines.append(f"GPU:            nvidia-smi {'not found' if smi is None else smi}")
    lines.append(f"torch.cuda:     {data['gpu']['torch_cuda']}")
    lines.append(f"HEIC decoder:   {yn(data['decoders']['heic'])}   PDF renderer: {yn(data['decoders']['pdf'])}")
    cli = data["claude_cli"]
    lines.append(f"claude CLI:     installed {yn(cli['installed'])}, logged in {yn(cli['logged_in'])} {cli.get('detail') or ''}".rstrip())
    rec = data["recognition"]
    lines.append(f"Recognition:    configured {rec['configured_model']}, subagent file {rec['subagent_model']}"
                 + ("" if rec["consistent"] else "  << MISMATCH: align .env and .claude/agents/handwriting-recogniser.md"))
    for name, st in data["adapters"].items():
        lines.append(f"Adapter {name}: {st['state']}" + (f" — {'; '.join(st['blockers'])}" if st["blockers"] else ""))
        if st["next_step"]:
            lines.append(f"    next step: {st['next_step']}")
    net = data["network"]
    lines.append(f"Network:        {net['host']} ({'online via Cloudflare Tunnel' if net.get('online') else 'LAN mode' if net['lan_mode'] else 'loopback only'})")
    lines.append(f"cloudflared:    {net.get('cloudflared') or 'not installed (only needed for online mode: winget install --id Cloudflare.cloudflared)'}")
    return "\n".join(lines)


def data_dir_inside_repo(settings: Settings) -> bool:
    repo = Path(REPO_ROOT).resolve()
    return settings.data_dir == repo or repo in settings.data_dir.parents
