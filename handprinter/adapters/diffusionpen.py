"""DiffusionPen adapter (Nikolaidou et al., ECCV 2024).

Upstream sampling is a mode of `train.py` tied to its IAM loaders; there is no documented
entry point for "these words with these five style images". Until a driver is written
against a locally cloned repository, this adapter reports that blocker honestly.
"""

from __future__ import annotations

import string
from pathlib import Path

from handprinter.adapters.base import AdapterStatus, Capabilities, ModelAdapter, python_ok

UPSTREAM = {
    "paper": "https://arxiv.org/abs/2409.06065",
    "code": "https://github.com/koninik/DiffusionPen",
    "weights": "https://huggingface.co/konnik/DiffusionPen",
    "licence": "MIT",
}
CHARSET = "!\"#&'()*+,-./0123456789:;?" + string.ascii_uppercase + string.ascii_lowercase + " "


class DiffusionPenAdapter(ModelAdapter):
    name = "diffusionpen"
    title = "DiffusionPen"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            levels=["word"], charset=CHARSET,
            charset_note="Hard-coded in upstream train.py (IAM character set).",
            max_chars=None, needs_style_references=5, can_train_in_app=False,
            train_note="upstream has no fine-tune command; README says to adapt the data loader for new data",
        )

    def status(self) -> AdapterStatus:
        repo = Path(self.env.get("HANDPRINTER_DIFFUSIONPEN_REPO", "") or ".missing")
        ckpt = Path(self.env.get("HANDPRINTER_DIFFUSIONPEN_CHECKPOINT", "") or ".missing")
        checks = {
            "environment": python_ok(self.env.get("HANDPRINTER_DIFFUSIONPEN_PYTHON")),
            "repository": (repo / "train.py").is_file(),
            "checkpoint": ckpt.is_file(),
            "custom_style_entry_point": False,
        }
        blockers = []
        if not checks["environment"]:
            blockers.append("no DiffusionPen Python environment configured (HANDPRINTER_DIFFUSIONPEN_PYTHON)")
        if not checks["repository"]:
            blockers.append("upstream repository not cloned (HANDPRINTER_DIFFUSIONPEN_REPO)")
        if not checks["checkpoint"]:
            blockers.append("checkpoint ema_ckpt.pt not found (HANDPRINTER_DIFFUSIONPEN_CHECKPOINT)")
        blockers.append("upstream offers no documented way to sample custom words with your own style images")
        next_step = (
            "clone the repo and download weights (docs/model-experiments.md), then write and verify a small sampling driver "
            "against the cloned code; the app will not guess its internal APIs"
        )
        return AdapterStatus(self.name, self.title, "blocked", checks, blockers, next_step, UPSTREAM)
