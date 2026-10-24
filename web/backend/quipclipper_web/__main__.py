"""Console entry point: run the API under uvicorn using env-driven settings.

``quipclipper-web`` serves the app; ``quipclipper-web advertise`` announces it on the
local network instead (opt-in, ``QC_ADVERTISE=1``; see :mod:`quipclipper_web.advertise`).
"""

from __future__ import annotations

import sys

USAGE = """usage: quipclipper-web [advertise [--port PORT]]

  (no command)  serve the web app (settings from QC_* environment variables)
  advertise     announce this server on the local network (mDNS) until stopped;
                needs QC_ADVERTISE=1 and the port browsers use (QC_ADVERTISE_PORT)
"""


def main(argv: list[str] | None = None) -> int | None:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "advertise":
        from quipclipper_web import advertise

        return advertise.main(args[1:])
    if args:
        out = sys.stdout if args[0] in ("-h", "--help", "help") else sys.stderr
        print(USAGE, end="", file=out)
        return 0 if out is sys.stdout else 2

    import uvicorn

    from quipclipper_web.config import Settings

    settings = Settings.from_env()
    # Import string (not the app object) so uvicorn owns the lifecycle; the
    # module-level `app` is built from the same environment.
    uvicorn.run(
        "quipclipper_web.app:app",
        host=settings.listen_address,
        port=settings.listen_port,
        # The connection's own peer, not uvicorn's reading of X-Forwarded-For: who the
        # caller is comes from the bundled nginx only when the peer is a trusted proxy
        # (QC_FORWARDED_ALLOW_IPS, quipclipper_web.caller).
        proxy_headers=False,
    )
    return None


if __name__ == "__main__":
    sys.exit(main())
