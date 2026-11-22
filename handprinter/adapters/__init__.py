"""Model adapters. Each runs in its own Python environment as a subprocess."""

from handprinter.adapters.base import ModelAdapter
from handprinter.adapters.diffusionpen import DiffusionPenAdapter
from handprinter.adapters.eruku import ErukuAdapter

_CACHE: dict[tuple, dict[str, ModelAdapter]] = {}


def all_adapters(env: dict[str, str]) -> dict[str, ModelAdapter]:
    """Adapter instances, cached per configuration so probe results are reused between requests."""
    key = tuple(sorted(env.items()))
    if key not in _CACHE:
        adapters: list[ModelAdapter] = [DiffusionPenAdapter(env), ErukuAdapter(env)]
        _CACHE[key] = {a.name: a for a in adapters}
    return _CACHE[key]
