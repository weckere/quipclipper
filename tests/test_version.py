"""pyproject.toml is the one version source: every other copy must agree with it.

scripts/release.sh bumps the root pyproject.toml and web/backend/pyproject.toml
together; the Nix files read the root one with builtins.fromTOML, and the Python
packages read their installed metadata (importlib.metadata).
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

import quipclipper

ROOT = Path(__file__).resolve().parents[1]
NIX_FILES = ["flake.nix", "nix/quipclipper-web.nix", "nix/frontend.nix"]


def _pyproject_version(path: Path) -> str:
    found = re.search(r'^version = "([^"]+)"', path.read_text(), re.M)
    assert found, f"no version line in {path}"
    return found.group(1)


ROOT_VERSION = _pyproject_version(ROOT / "pyproject.toml")


def test_root_version_is_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", ROOT_VERSION), ROOT_VERSION


def test_web_backend_pyproject_matches_root() -> None:
    assert _pyproject_version(ROOT / "web/backend/pyproject.toml") == ROOT_VERSION


@pytest.mark.parametrize("name", NIX_FILES)
def test_nix_reads_version_from_pyproject(name: str) -> None:
    text = (ROOT / name).read_text()
    assert not re.search(r'\bversion\s*=\s*"', text), f"{name} hard-codes a version"
    assert re.search(
        r"version = \(builtins\.fromTOML \(builtins\.readFile \.\.?/pyproject\.toml\)\)"
        r"\.project\.version;",
        text,
    ), f"{name} does not read the version from the root pyproject.toml"


def test_engine_version_matches_pyproject() -> None:
    try:
        installed = version("quipclipper")
    except PackageNotFoundError:
        installed = None
    assert quipclipper.__version__ == ROOT_VERSION, (
        f"quipclipper.__version__ is {quipclipper.__version__} (installed metadata: "
        f"{installed}), pyproject.toml says {ROOT_VERSION}; reinstall (pip install -e .)"
    )


def test_engine_version_fallback_reads_the_checkout(monkeypatch) -> None:
    import importlib.metadata as md

    def missing(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(md, "version", missing)
    assert quipclipper._version() == ROOT_VERSION
