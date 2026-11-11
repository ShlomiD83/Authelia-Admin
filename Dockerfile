# ---------------------------------------------------------------------------
# Stage 1: build the Authelia CLI from the LATEST release tag with the newest Go
# toolchain and patched dependencies. Only `validate-config` and `storage ...`
# are needed, so the web frontend is skipped (the repo ships a placeholder).
# Built on Wolfi (glibc) so it matches the runtime image's libc.
# ---------------------------------------------------------------------------
FROM cgr.dev/chainguard/go:latest-dev AS authelia-build
USER root
RUN apk add --no-cache git build-base
# Changing this value (CI passes the ISO week) forces a fresh clone and fresh dependency versions
# instead of a cached layer, so scheduled builds really pick up new Authelia releases and patched libraries.
ARG BUILD_STAMP=manual
RUN set -eux; \
    echo "Build stamp: ${BUILD_STAMP}"; \
    TAG="$(git ls-remote --tags --refs --sort=-v:refname https://github.com/authelia/authelia 'v4.*' \
           | awk -F/ '{print $3}' | grep -v -- '-' | head -n1)"; \
    echo "Building Authelia ${TAG}"; \
    git clone --depth 1 --branch "${TAG}" https://github.com/authelia/authelia /src
WORKDIR /src
RUN set -eux; \
    go mod edit -dropreplace=golang.org/x/net; \
    go get golang.org/x/net@latest github.com/klauspost/compress@latest golang.org/x/crypto@master; \
    go mod tidy; \
    CGO_ENABLED=1 go build -trimpath -ldflags "-s -w" -o /out/authelia ./cmd/authelia

# ---------------------------------------------------------------------------
# Stage 2: install the Python dependencies into a plain directory (no venv, no pip
# in the final image).
# ---------------------------------------------------------------------------
FROM cgr.dev/chainguard/python:latest-dev AS pybuild
USER root
RUN apk add --no-cache build-base libffi-dev
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --target /deps -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 3: runtime. Distroless Wolfi Python: no shell, no package manager, no pip,
# no musl, no system SQLite.
# ---------------------------------------------------------------------------
FROM cgr.dev/chainguard/python:latest

ENV PYTHONPATH=/deps \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin \
    AUTHELIA_CONFIG_DIR=/config

COPY --from=pybuild /deps /deps
COPY --from=authelia-build /out/authelia /usr/local/bin/authelia
COPY --chmod=755 app/ /srv/app/

# Smoke tests (exec form, no shell needed): fail the build if either piece is broken.
RUN ["/usr/bin/python", "-c", "import flask, yaml, argon2, docker, gunicorn, webauthn, segno, zoneinfo; zoneinfo.ZoneInfo('Asia/Jerusalem'); print('python deps OK')"]
RUN ["/usr/local/bin/authelia", "--help"]

WORKDIR /srv/app
EXPOSE 8080

# No shell or curl in this image, so probe /healthz with Python (exec form).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["/usr/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"]

# Runs as the image's default non-root user; compose sets `user:` to match the owner
# of your Authelia folder.
ENTRYPOINT ["/usr/bin/python", "-m", "gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "--threads", "4", "--worker-tmp-dir", "/dev/shm", "--no-control-socket", "app:app"]
