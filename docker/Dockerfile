# The offline evaluation image (docker/offline.sh build): Ubuntu 24.04 with python3 (3.12); the framework's
# environment in /opt/venv (pyproject.toml's pinned dependencies with its test and skillopt extras, and what the
# examples' launchers and prepare scripts import: openai, huggingface_hub, uv); bubblewrap for hermit's sandboxes;
# the Codex CLI for the prompt-only and mwm learners; sqlite3; and the egress proxy of the OfficeQA Codex harness,
# which offline.sh runs as the containers' only route out. The hermit checkout is NOT in the image: offline.sh mounts
# it at its host path and points PYTHONPATH at it, so a container runs the mounted code.
#
#   docker build -f docker/Dockerfile --build-arg UID=$(id -u) --build-arg GID=$(id -g) -t hermit-offline:latest .
#
# UID and GID make the user `hermit` the host user, so files written to the mounts belong to the host user and
# bubblewrap runs unprivileged. CODEX_VERSION names the Codex release fetched from npm (empty: no Codex).
# docker/Dockerfile.dockerignore keeps the build context to the three files copied below.
ARG BASE=ubuntu:24.04
FROM ${BASE}
ARG UID=1000
ARG GID=1000
ARG CODEX_VERSION=0.164.0
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv python3-pip git bubblewrap curl \
        ca-certificates sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# /opt/venv: the dependencies as pyproject.toml pins them (hermit_dist.py reads them), plus the launchers' needs.
# hermit itself is not installed; hermit_dist.py registers the distribution's metadata, which hermit/sandbox.py
# looks up to bind the framework's dependencies into a sandbox. /opt/venv/pip-freeze.txt records the versions.
COPY pyproject.toml /tmp/pyproject.toml
COPY docker/hermit_dist.py /opt/hermit_dist.py
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && python3 /opt/hermit_dist.py deps /tmp/pyproject.toml test skillopt \
        | xargs -d '\n' /opt/venv/bin/pip install --no-cache-dir openai huggingface_hub uv \
    && /opt/venv/bin/pip freeze > /opt/venv/pip-freeze.txt \
    && /opt/venv/bin/python /opt/hermit_dist.py register /tmp/pyproject.toml \
    && rm /tmp/pyproject.toml \
    && printf '#!/opt/venv/bin/python\nimport sys\nfrom hermit.cli import main\nsys.exit(main())\n' > /opt/venv/bin/hermit \
    && chmod 755 /opt/venv/bin/hermit

# The Codex CLI (the learner of the prompt-only and mwm examples) from its npm package, under /usr/local, which
# hermit's sandbox shows read-only, with the package's ripgrep on PATH as the npm launcher would have it.
RUN if [ -n "$CODEX_VERSION" ]; then \
        mkdir -p /usr/local/lib/codex \
        && curl -fsSL "https://registry.npmjs.org/@openai/codex/-/codex-${CODEX_VERSION}-linux-x64.tgz" \
            | tar -xz --strip-components 1 -C /usr/local/lib/codex \
        && ln -s /usr/local/lib/codex/vendor/x86_64-unknown-linux-musl/bin/codex /usr/local/bin/codex \
        && codex --version; \
    fi

# The containers' user, with the host user's ids (offline.sh passes --user too): the base image's `ubuntu` goes
# when its uid collides, and a group that already has the gid is reused.
RUN if getent passwd "$UID" >/dev/null; then userdel -r "$(getent passwd "$UID" | cut -d: -f1)"; fi \
    && (getent group "$GID" >/dev/null || groupadd -g "$GID" hermit) \
    && useradd -m -u "$UID" -g "$GID" -s /bin/bash hermit

COPY examples/officeqa/utils/agent/docker/egress_proxy.py /opt/egress_proxy.py
ENV PATH=/opt/venv/bin:/usr/local/lib/codex/vendor/x86_64-unknown-linux-musl/codex-path:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
USER hermit
CMD ["bash"]
