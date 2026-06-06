"""Console entry point: run the API under uvicorn using env-driven settings."""

from __future__ import annotations

import uvicorn

from quipclipper_web.config import Settings


def main() -> None:
    settings = Settings.from_env()
    # Import string (not the app object) so uvicorn owns the lifecycle; the
    # module-level `app` is built from the same environment.
    uvicorn.run(
        "quipclipper_web.app:app",
        host=settings.listen_address,
        port=settings.listen_port,
    )


if __name__ == "__main__":
    main()
