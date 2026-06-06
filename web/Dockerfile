# Backend image for quipclipper-web. Build with the repo root as context:
#   docker build -f web/Dockerfile -t quipclipper-web .
# (docker-compose.yml sets this context automatically.)
FROM python:3.12-slim

# ffmpeg/ffprobe + mkvmerge are the cutting backends.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg mkvtoolnix \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the clipping engine (repo root package) first…
COPY pyproject.toml README.md ./engine/
COPY src ./engine/src
RUN pip install --no-cache-dir ./engine

# …then the web backend that depends on it.
COPY web/backend ./backend
RUN pip install --no-cache-dir ./backend

ENV QC_BIND=0.0.0.0 \
    QC_PORT=8000 \
    QC_CLIPS_DIR=/clips \
    QC_STATE_DIR=/state

EXPOSE 8000
CMD ["quipclipper-web"]
