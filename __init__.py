"""hermes-bot-rooms — Agent-Haelfte des Plugins.

Keine Tools, keine Hooks: die Arbeit steckt im Dashboard-Tab und in den
Backend-Routen (``dashboard/plugin_api.py``, geladen ueber
``dashboard/manifest.json`` -> ``"api"``).

Der Plugin-Loader verlangt dieses Modul im Ordner — ohne ``__init__.py`` bricht
er mit "No __init__.py" ab.
"""

from __future__ import annotations

from typing import Any


def register(ctx: Any) -> None:
    """Nichts anzumelden: die Routen liegen im Dashboard-Wirt."""
    return None
