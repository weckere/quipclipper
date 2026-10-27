import pytest

from quipclipper import updates as upd


@pytest.fixture(autouse=True)
def _no_github(monkeypatch):
    """No test asks GitHub: an app whose daily check for updates (M24) fires during a test
    gets "unreachable". test_update_api.py replaces these with its own fakes."""
    def offline(*args, **kwargs):
        raise upd.CheckError("tests don't reach GitHub")

    monkeypatch.setattr(upd, "fetch_latest", offline)
    monkeypatch.setattr(upd, "fetch_tag_commit", offline)
