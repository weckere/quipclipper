import pytest


@pytest.fixture(autouse=True)
def _isolate_cli_settings(tmp_path, monkeypatch):
    """Keep the CLI away from the developer's real settings file and server."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))  # update.check's answer
    for var in ("QC_URL", "QC_API_TOKEN", "QC_API_TOKEN_FILE", "QC_BUILD_COMMIT", "QC_INSTALL"):
        monkeypatch.delenv(var, raising=False)
