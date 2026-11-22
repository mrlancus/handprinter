"""Eruku adapter (Zaccagnino et al., WACV 2026). Inference via the documented model-card call only."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from handprinter.adapters.base import (
    RUNNERS_DIR, AdapterStatus, AdapterUnavailable, Capabilities, InferRequest, ModelAdapter, python_ok, run_runner,
)

UPSTREAM = {
    "paper": "https://arxiv.org/abs/2510.23240",
    "code": "https://github.com/Blowing-Up-Groundhogs/Eruku",
    "weights": "https://huggingface.co/blowing-up-groundhogs/eruku",
    "licence": "code MIT; model card says Apache-2.0",
}
PROBE_TTL_S = 120


class ErukuAdapter(ModelAdapter):
    name = "eruku"
    title = "Eruku"

    def __init__(self, env: dict[str, str]):
        super().__init__(env)
        self._probe: tuple[float, dict[str, Any]] | None = None

    @property
    def python(self) -> str:
        return self.env.get("HANDPRINTER_ERUKU_PYTHON", "")

    @property
    def model_id(self) -> str:
        return self.env.get("HANDPRINTER_ERUKU_MODEL", "blowing-up-groundhogs/eruku")

    def capabilities(self) -> Capabilities:
        return Capabilities(
            levels=["word", "line"], charset=None,
            charset_note="Byte-level ByT5 tokenizer; trained mainly on English. No character set is documented.",
            max_chars=120, needs_style_references=1, can_train_in_app=False,
            train_note="upstream has no documented fine-tune-on-your-own-writer command (train_eruku_continous.py targets multi-GPU webdataset training)",
        )

    def probe(self, refresh: bool = False) -> dict[str, Any]:
        if not python_ok(self.python):
            return {"ok": False, "error": "HANDPRINTER_ERUKU_PYTHON is not set to a Python executable"}
        if self._probe and not refresh and time.time() - self._probe[0] < PROBE_TTL_S:
            return self._probe[1]
        try:
            result = run_runner(self.python, RUNNERS_DIR / "eruku_runner.py", {"action": "probe", "model_id": self.model_id},
                                lambda e: None, lambda: False, timeout_s=90)
        except (RuntimeError, AdapterUnavailable, OSError) as exc:
            result = {"ok": False, "error": str(exc)[:500]}
        self._probe = (time.time(), result)
        return result

    def status(self) -> AdapterStatus:
        checks = {"environment": python_ok(self.python), "torch": False, "transformers": False, "cuda": False, "weights_cached": False}
        blockers: list[str] = []
        next_step = ""
        if not checks["environment"]:
            blockers.append("no Eruku Python environment configured")
            next_step = "create a separate venv with PyTorch (CUDA build) and the packages in docs/model-experiments.md, then set HANDPRINTER_ERUKU_PYTHON"
        else:
            probe = self.probe()
            checks.update({k: bool(probe.get(k)) for k in ("torch", "transformers", "cuda", "weights_cached")})
            if probe.get("error"):
                blockers.append(f"environment probe failed: {probe['error']}")
                next_step = "fix the adapter environment (see the error), then refresh"
            elif not checks["torch"] or not checks["transformers"]:
                blockers.append("torch/transformers missing in the Eruku environment")
                next_step = "pip install torch torchvision transformers==4.53.0 diffusers==0.34.0 einops pillow huggingface_hub in that environment"
            elif not checks["weights_cached"]:
                blockers.append(f"weights for {self.model_id} are not downloaded (about 3 GB plus T5-large and the VAE)")
                next_step = "start the download from Experiments (explicit action)"
            elif not checks["cuda"]:
                blockers.append("CUDA is not available in the Eruku environment (CPU inference would be impractically slow)")
                next_step = "install a CUDA build of PyTorch matching your driver; run doctor"
        return AdapterStatus(self.name, self.title, "ready" if not blockers else "blocked", checks, blockers, next_step, UPSTREAM)

    def download(self, on_event: Callable[[dict[str, Any]], None], cancelled: Callable[[], bool]) -> dict[str, Any]:
        if not python_ok(self.python):
            raise AdapterUnavailable("configure HANDPRINTER_ERUKU_PYTHON first")
        result = run_runner(self.python, RUNNERS_DIR / "eruku_runner.py", {"action": "download", "model_id": self.model_id},
                            on_event, cancelled, timeout_s=6 * 3600)
        self._probe = None
        return result

    def preflight(self, out_dir: Path, on_event: Callable[[dict[str, Any]], None], cancelled: Callable[[], bool]) -> dict[str, Any]:
        self.require_ready()
        return run_runner(self.python, RUNNERS_DIR / "eruku_runner.py",
                          {"action": "preflight", "model_id": self.model_id, "out_dir": str(out_dir)}, on_event, cancelled, timeout_s=1800)

    def infer(self, request: InferRequest, on_event: Callable[[dict[str, Any]], None], cancelled: Callable[[], bool]) -> dict[str, Any]:
        self.require_ready()
        if not request.style_images:
            raise AdapterUnavailable("Eruku needs one style reference: create a style profile from an approved snapshot first")
        payload = {
            "action": "infer", "model_id": self.model_id, "texts": request.texts, "seeds": request.seeds,
            "style_image": str(request.style_images[0]), "style_text": request.options.get("style_text", ""),
            "cfg_scale": float(request.options.get("cfg_scale", 1.25)), "out_dir": str(request.out_dir),
        }
        return run_runner(self.python, RUNNERS_DIR / "eruku_runner.py", payload, on_event, cancelled, timeout_s=3600)
