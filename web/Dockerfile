# Backend image for quipclipper-web. Build with the repo root as context:
#   docker build -f web/Dockerfile -t quipclipper-web .
# (docker-compose.yml sets this context automatically.)
FROM python:3.12-slim

# ffmpeg/ffprobe + mkvmerge are the cutting backends. The Intel iHD VAAPI driver
# (intel-media-va-driver, Debian non-free) + libva let ffmpeg use Intel Quick
# Sync (the host iGPU at /dev/dri) to hardware-encode H.264 and decode
# H.264/HEVC for the browser-preview re-encode (B23). Verified on the host's UHD
# 630: H.264 encode works with iHD (the older i965 driver asserts on encode).
# Falls back to software when /dev/dri isn't passed into the container.
RUN sed -i 's/Components: main/Components: main contrib non-free non-free-firmware/' \
       /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg mkvtoolnix intel-media-va-driver-non-free libva2 libva-drm2 vainfo \
    && rm -rf /var/lib/apt/lists/*
ENV LIBVA_DRIVER_NAME=iHD

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
