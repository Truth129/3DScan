# 3DScan in a container. docs/DOCKER.md shows how to build and run it.
# It uses no BuildKit-only syntax, so the legacy builder can build it too.
ARG UBUNTU=ubuntu:26.04

# The viewer, with the Node major version CI uses.
FROM node:26.13.0-slim AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npx vite build && npx vite build --config vite.offline.config.ts

FROM ghcr.io/astral-sh/uv:0.9.18 AS uv

# Compilers stay in the build stages. Some packages, such as pyjpegls, have no wheel for Python 3.14.
FROM ${UBUNTU} AS toolchain
RUN apt-get update \
  && apt-get install --yes --no-install-recommends ca-certificates curl g++ make cmake pkg-config libopenjp2-7-dev zlib1g-dev python3 \
  && rm -rf /var/lib/apt/lists/*

# The pinned dcm2niix with JPEG 2000 and JPEG-LS decoding, built by the checkout's own script.
FROM toolchain AS converter
WORKDIR /src
COPY scripts/build_dcm2niix.sh scripts/
RUN scripts/build_dcm2niix.sh

# Python 3.14 and the dependencies, at the paths the runtime image uses.
FROM toolchain AS python
# cpu: CPU-only PyTorch and the AI packages. rocm: the hash-locked AMD stack for gfx1201 GPUs.
# none: viewer and editing only, with no AI.
ARG TORCH=cpu
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python UV_PYTHON_PREFERENCE=only-managed UV_NO_CACHE=1 UV_COMPILE_BYTECODE=1
WORKDIR /app
RUN uv python install 3.14 && uv venv --python 3.14 .venv
# Only what the install reads, so a docs change does not reinstall everything.
COPY pyproject.toml ./
COPY requirements/constraints.txt requirements/rocm10-py314.lock requirements/
RUN case "$TORCH" in \
    cpu) uv pip install --python .venv/bin/python --torch-backend cpu --constraint requirements/constraints.txt --requirements pyproject.toml --extra ai ;; \
    rocm) uv pip sync --python .venv/bin/python --require-hashes --index-strategy unsafe-best-match requirements/rocm10-py314.lock ;; \
    none) uv pip install --python .venv/bin/python --constraint requirements/constraints.txt --requirements pyproject.toml ;; \
    *) echo "TORCH must be cpu, rocm or none" >&2; exit 1 ;; \
  esac

FROM ${UBUNTU}
ARG TORCH=cpu
# AMD's PyTorch links the system libgomp, and MIOpen compiles kernels at run time against the C
# library headers.
RUN apt-get update \
  && apt-get install --yes --no-install-recommends ca-certificates libopenjp2-7 tini \
  && if [ "$TORCH" = rocm ]; then apt-get install --yes --no-install-recommends libc6-dev libgomp1; fi \
  && rm -rf /var/lib/apt/lists/* \
  && userdel --remove ubuntu \
  && groupadd --gid 10001 scan3d \
  && useradd --uid 10001 --gid scan3d --create-home --shell /usr/sbin/nologin scan3d
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=python /opt/python/ /opt/python/
COPY --from=python /app/.venv/ /app/.venv/
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY scan3d/ scan3d/
COPY scripts/ scripts/
# Editable, because the app finds web/dist, .tools and .runtime beside its package.
RUN UV_NO_CACHE=1 uv pip install --python .venv/bin/python --no-deps --editable . \
  && .venv/bin/python -m compileall -q scan3d scripts \
  && rm -rf /tmp/*
COPY --from=converter /src/.tools/ .tools/
COPY --from=web /src/web/dist/ web/dist/
COPY deployment/docker-entrypoint.sh /usr/local/bin/scan3d-entrypoint
RUN chmod 0755 /usr/local/bin/scan3d-entrypoint \
  && mkdir /data .runtime .cache \
  && chown scan3d:scan3d /data .runtime .cache \
  && chmod 0700 /data .runtime .cache

# Listening on every address inside the container means sign-in is always on.
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    SCAN3D_HOST=0.0.0.0 \
    SCAN3D_PORT=9010 \
    SCAN3D_DATA_DIR=/data \
    SCAN3D_IMPORT_ROOTS=/import
# AMD's compiled kernels stay in the runtime volume, so an updated image does not compile them again.
ENV MIOPEN_USER_DB_PATH=/app/.runtime/cache/miopen-db \
    MIOPEN_CUSTOM_CACHE_DIR=/app/.runtime/cache/miopen-kernels \
    AMD_COMGR_CACHE_DIR=/app/.runtime/cache/comgr
USER scan3d
VOLUME ["/data", "/app/.runtime"]
EXPOSE 9010
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s \
  CMD python -c "import json, os, urllib.request as r; h = json.load(r.build_opener(r.ProxyHandler({})).open('http://127.0.0.1:%s/api/health' % os.environ.get('SCAN3D_PORT', '9010'), timeout=8)); raise SystemExit(not h['worker'])"
ENTRYPOINT ["tini", "--", "scan3d-entrypoint"]
CMD ["scan3d", "serve"]
