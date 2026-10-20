"""quipclipper-web — a browser front-end for the quipclipper clipping engine."""



def _version() -> str:
    """The installed version; it follows the root pyproject.toml (tests/test_version.py)."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("quipclipper-web")
    except PackageNotFoundError:  # an uninstalled checkout (PYTHONPATH=web/backend)
        from pathlib import Path
        import re

        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        try:
            found = re.search(r'^version = "([^"]+)"', pyproject.read_text(), re.M)
        except OSError:
            found = None
        return found.group(1) if found else "0+unknown"


__version__ = _version()
