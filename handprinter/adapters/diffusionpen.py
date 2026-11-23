"""DiffusionPen adapter (Nikolaidou et al., ECCV 2024).

Runs `runners/diffusionpen_runner.py` in its own Python environment. The runner was written against the
cloned upstream code (commit `TESTED_COMMIT`) and verified there with the real weights: sampling custom
words from 5 of your own style crops, and fine-tuning on your own words (docs/model-experiments.md).
"""

from __future__ import annotations

import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from handprinter.adapters.base import (
    RUNNERS_DIR, AdapterStatus, AdapterUnavailable, Capabilities, InferRequest, ModelAdapter, python_ok, run_runner,
)

UPSTREAM = {
    "paper": "https://arxiv.org/abs/2409.06065",
    "code": "https://github.com/koninik/DiffusionPen",
    "weights": "https://huggingface.co/konnik/DiffusionPen",
    "licence": "MIT",
}
CHARSET = "!\"#&'()*+,-./0123456789:;?" + string.ascii_uppercase + string.ascii_lowercase + " "
TESTED_COMMIT = "059e2f2bcb7372231038719e955597917c1a8783"
PROBE_TTL_S = 120
RUNNER = RUNNERS_DIR / "diffusionpen_runner.py"


@dataclass
class TrainRequest:
    items: list[dict[str, str]]          # {"image": path, "text": transcription}, training split only
    out_dir: Path
    steps: int = 2000
    batch_size: int = 8
    lr: float = 5e-5
    seed: int = 7
    options: dict[str, Any] = field(default_factory=dict)


class DiffusionPenAdapter(ModelAdapter):
    name = "diffusionpen"
    title = "DiffusionPen"

    def __init__(self, env: dict[str, str]):
        super().__init__(env)
        self._probe: tuple[float, dict[str, Any]] | None = None

    @property
    def python(self) -> str:
        return self.env.get("HANDPRINTER_DIFFUSIONPEN_PYTHON", "")

    @property
    def repo(self) -> str:
        return self.env.get("HANDPRINTER_DIFFUSIONPEN_REPO", "")

    @property
    def weights(self) -> Path:
        return Path(self.env.get("HANDPRINTER_DIFFUSIONPEN_WEIGHTS", "") or ".missing")

    @property
    def checkpoint(self) -> Path:
        override = self.env.get("HANDPRINTER_DIFFUSIONPEN_CHECKPOINT", "")
        return Path(override) if override else self.weights / "diffusionpen_iam_model_path" / "models" / "ema_ckpt.pt"

    def paths(self, checkpoint: str | None = None) -> dict[str, str]:
        return {"repo": self.repo, "checkpoint": checkpoint or str(self.checkpoint),
                "style_model": str(self.weights / "style_models" / "iam_style_diffusionpen.pth"),
                "sd_path": str(self.weights / "stable-diffusion-v1-5"), "canine": str(self.weights / "canine-c")}

    def capabilities(self) -> Capabilities:
        return Capabilities(
            levels=["word"], charset=CHARSET,
            charset_note="Fixed IAM character set (upstream train.py): letters, digits and ! \" # & ' ( ) * + , - . / : ; ? — no maths symbols.",
            max_chars=None, needs_style_references=5, can_train_in_app=True,
            train_note="fine-tunes the UNet on your approved training words (text encoder frozen); about 600 MB of trainable state, fits a 6 GB GPU",
        )

    def probe(self, refresh: bool = False) -> dict[str, Any]:
        if not python_ok(self.python):
            return {"ok": False, "error": "HANDPRINTER_DIFFUSIONPEN_PYTHON is not set to a Python executable"}
        if self._probe and not refresh and time.time() - self._probe[0] < PROBE_TTL_S:
            return self._probe[1]
        try:
            result = run_runner(self.python, RUNNER, {"action": "probe", **self.paths()}, lambda e: None, lambda: False, timeout_s=90)
        except (RuntimeError, AdapterUnavailable, OSError) as exc:
            result = {"ok": False, "error": str(exc)[:500]}
        self._probe = (time.time(), result)
        return result

    def status(self) -> AdapterStatus:
        checks = {"environment": python_ok(self.python), "torch": False, "repository": False, "weights": False, "cuda": False}
        blockers: list[str] = []
        next_step = ""
        if not checks["environment"]:
            blockers.append("no DiffusionPen Python environment configured (HANDPRINTER_DIFFUSIONPEN_PYTHON)")
            next_step = "run SetupModels.bat on the PC (creates the environment and gets the code), then restart Handprinter"
        else:
            probe = self.probe()
            libs = all(probe.get(k) for k in ("torch", "diffusers", "transformers", "timm", "einops"))
            checks.update(torch=libs, repository=bool(probe.get("repository")), cuda=bool(probe.get("cuda")),
                          weights=all(probe.get(k) for k in ("checkpoint", "style_model", "vae")))
            if probe.get("error"):
                blockers.append(f"environment probe failed: {probe['error']}")
                next_step = "fix the adapter environment (see the error), then refresh"
            elif not libs:
                missing = [k for k in ("torch", "diffusers", "transformers", "timm", "einops") if not probe.get(k)]
                blockers.append(f"missing in the DiffusionPen environment: {', '.join(missing)}")
                next_step = "re-run SetupModels.bat"
            elif not checks["repository"]:
                blockers.append("upstream repository not found (HANDPRINTER_DIFFUSIONPEN_REPO must hold unet.py)")
                next_step = f"git clone https://github.com/koninik/DiffusionPen and check out {TESTED_COMMIT[:7]}"
            elif not checks["weights"]:
                blockers.append(f"weights are not downloaded to {self.weights} (about 1.5 GB)")
                next_step = "start the download from Experiments (explicit action)"
            commit = probe.get("commit")
            if commit and commit != TESTED_COMMIT and not blockers:
                next_step = f"note: the repository is at {commit[:7]}, the driver was verified at {TESTED_COMMIT[:7]}"
            if not blockers and not checks["cuda"]:
                next_step = "no CUDA: runs on the CPU (a few seconds per word; fine-tuning is slow)"
        return AdapterStatus(self.name, self.title, "ready" if not blockers else "blocked", checks, blockers, next_step, UPSTREAM)

    def download(self, on_event: Callable[[dict[str, Any]], None], cancelled: Callable[[], bool]) -> dict[str, Any]:
        if not python_ok(self.python):
            raise AdapterUnavailable("configure HANDPRINTER_DIFFUSIONPEN_PYTHON first")
        self.weights.mkdir(parents=True, exist_ok=True)
        result = run_runner(self.python, RUNNER, {"action": "download", "weights": str(self.weights)}, on_event, cancelled,
                            timeout_s=4 * 3600)
        self._probe = None
        return result

    def preflight(self, out_dir: Path, on_event: Callable[[dict[str, Any]], None], cancelled: Callable[[], bool]) -> dict[str, Any]:
        self.require_ready()
        return run_runner(self.python, RUNNER, {"action": "preflight", **self.paths()}, on_event, cancelled, timeout_s=1800)

    def infer(self, request: InferRequest, on_event: Callable[[dict[str, Any]], None], cancelled: Callable[[], bool]) -> dict[str, Any]:
        self.require_ready()
        if not request.style_images:
            raise AdapterUnavailable("DiffusionPen needs 5 style references: create a style profile from an approved snapshot first")
        payload = {"action": "infer", **self.paths(request.options.get("checkpoint_path")), "texts": request.texts,
                   "seeds": request.seeds, "style_images": [str(p) for p in request.style_images], "out_dir": str(request.out_dir)}
        return run_runner(self.python, RUNNER, payload, on_event, cancelled, timeout_s=3600)

    def train(self, request: TrainRequest, on_event: Callable[[dict[str, Any]], None],  # type: ignore[override]
              cancelled: Callable[[], bool]) -> dict[str, Any]:
        self.require_ready()
        payload = {"action": "train", **self.paths(), "items": request.items, "out_dir": str(request.out_dir),
                   "steps": request.steps, "batch_size": request.batch_size, "lr": request.lr, "seed": request.seed,
                   **request.options}
        return run_runner(self.python, RUNNER, payload, on_event, cancelled, timeout_s=12 * 3600)
