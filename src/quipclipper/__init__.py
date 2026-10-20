"""quipclipper — find and cut clips from movies/TV by searching subtitle dialogue."""

from quipclipper.models import Cue, Match

__all__ = ["Cue", "Match"]


def _version() -> str:
    """The installed version; pyproject.toml is the one place it is written down."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("quipclipper")
    except PackageNotFoundError:  # an uninstalled checkout (PYTHONPATH=src)
        from pathlib import Path
        import re

        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        try:
            found = re.search(r'^version = "([^"]+)"', pyproject.read_text(), re.M)
        except OSError:
            found = None
        return found.group(1) if found else "0+unknown"


__version__ = _version()
