# syntax=docker/dockerfile:1
#
# LapVIEW's web app, for a Linux host with or without an NVIDIA GPU.
#
#   docker build -t lapview .
#   docker run -d --name lapview --restart unless-stopped --gpus all \
#       -p 8770:8770 -v /mnt/bulk/lapview:/data \
#       -e LAPVIEW_NVENC_PROFILE=pascal lapview
#
# Everything it keeps is under /data: library/, output/, cache/, inbox/ (and
# maps/, if you add one). The host folder must be writable by uid 1000.
# GET /api/health says which encoder was chosen and whether /data is writable.
#
# Base: Debian 13 (trixie). Its ffmpeg (7.1) is built with h264_nvenc,
# hevc_nvenc and CUDA decode; NVENC needs only the driver's own libraries,
# which the NVIDIA container toolkit mounts in when the container is started
# with --gpus. Without a GPU the app falls back to libx265/libx264 by itself.

FROM python:3.12-slim-trixie AS build
# pycairo has no Linux wheels, so it is compiled here, once, and only the
# finished wheels reach the runtime image -- no compiler in what is deployed.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential pkg-config libcairo2-dev \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r /tmp/requirements.txt


FROM python:3.12-slim-trixie

# NVIDIA capabilities -- compute: CUDA; video: NVENC/NVDEC; utility:
# nvidia-smi, which is how the NVENC profile is chosen when
# LAPVIEW_NVENC_PROFILE is left at auto.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility \
    LAPVIEW_DATA_DIR=/data \
    LAPVIEW_HOST=0.0.0.0 \
    LAPVIEW_PORT=8770 \
    LAPVIEW_HEADLESS=1

# ffmpeg for decode/encode, cairo for the gauges, and DejaVu Sans for their
# numerals (Segoe UI does not exist here; see racechrono/gauges/base.py).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ffmpeg libcairo2 fontconfig fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index /wheels/*.whl && rm -rf /wheels

# Host files under /mnt/bulk/lapview belong to uid/gid 1000.
RUN groupadd --gid 1000 lapview \
 && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin \
        lapview \
 && mkdir -p /data \
 && chown 1000:1000 /data

# The code stays root-owned and read-only to the app; it writes only to /data.
WORKDIR /app
COPY . /app

USER 1000:1000
EXPOSE 8770

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('LAPVIEW_PORT', '8770'), timeout=8)" || exit 1

# Host and port come from LAPVIEW_HOST / LAPVIEW_PORT above, so either can be
# changed with -e without rewriting the command.
CMD ["python", "-m", "webui.server", "--no-open", "--quiet"]
