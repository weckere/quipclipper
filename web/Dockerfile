# Backend image for quipclipper-web. Build with the repo root as context:
#   docker build -f web/Dockerfile -t quipclipper-web .
# (docker-compose.yml sets this context automatically.)
FROM python:3.12-slim

# ffmpeg/ffprobe + mkvmerge are the cutting backends. The i965 VAAPI driver +
# libva let ffmpeg use Intel Quick Sync (the host iGPU at /dev/dri) to
# hardware-encode H.264 (and hardware-decode H.264/HEVC) for the browser-preview
# re-encode — B23. Verified on the host's UHD 630: H264 High encode + HEVC
# decode. Falls back to software when /dev/dri isn't passed into the container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg mkvtoolnix i965-va-driver libva2 libva-drm2 vainfo \
    && rm -rf /var/lib/apt/lists/*
ENV LIBVA_DRIVER_NAME=i965

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
