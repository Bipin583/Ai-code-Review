"""Root-level settings entry point.

Thin re-export of :mod:`reviewbot.utils.config` so ``from configs.settings import
settings`` works from scripts run at the repo root.
"""

from reviewbot.utils.config import PROJECT_ROOT, Settings, get_settings, settings

__all__ = ["Settings", "settings", "get_settings", "PROJECT_ROOT"]
