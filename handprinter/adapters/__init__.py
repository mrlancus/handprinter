"""Model adapters. Each runs in its own Python environment as a subprocess."""

from handprinter.adapters.base import ModelAdapter
from handprinter.adapters.diffusionpen import DiffusionPenAdapter
from handprinter.adapters.eruku import ErukuAdapter


def all_adapters(env: dict[str, str]) -> dict[str, ModelAdapter]:
    adapters: list[ModelAdapter] = [DiffusionPenAdapter(env), ErukuAdapter(env)]
    return {a.name: a for a in adapters}
