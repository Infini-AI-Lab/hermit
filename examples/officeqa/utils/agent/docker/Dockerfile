# The Codex harness's container: Ubuntu 24.04, python3 (for the session's scripts) and Codex 0.162.0 for Linux arm64
# (its npm package: the codex binary, ripgrep and the bubblewrap its sandbox uses), unpacked into ./codex first.
# This version must match codex_session.py's default image tag and OFFICEQA_CODEX_IMAGE in the run configs
# (officeqa-codex:0.162.0):
#
#   npm pack @openai/codex@0.162.0-linux-arm64 && mkdir -p codex && tar -xzf openai-codex-0.162.0-linux-arm64.tgz \
#     --strip-components 1 -C codex
#   docker build -t officeqa-codex:0.162.0 .
#
# On an x64 host: @openai/codex@0.162.0-linux-x64 and --build-arg TRIPLE=x86_64-unknown-linux-musl.
# BASE may name an image that already has python3 and CA certificates (the build then downloads nothing).
ARG BASE=ubuntu:24.04
FROM ${BASE}
ARG TRIPLE=aarch64-unknown-linux-musl
RUN (command -v python3 && test -s /etc/ssl/certs/ca-certificates.crt) >/dev/null \
    || (apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 \
        ca-certificates && rm -rf /var/lib/apt/lists/*)
COPY codex /opt/codex
# codex and ripgrep on every PATH, a login shell's included (Codex runs commands with `bash -lc`, and
# /etc/profile resets PATH)
RUN ln -s /opt/codex/vendor/${TRIPLE}/bin/codex /usr/local/bin/codex \
    && ln -s /opt/codex/vendor/${TRIPLE}/codex-path/rg /usr/local/bin/rg
# what the npm launcher sets: ripgrep on PATH, the package's root
ENV PATH=/opt/codex/vendor/${TRIPLE}/codex-path:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    CODEX_MANAGED_PACKAGE_ROOT=/opt/codex \
    CODEX_MANAGED_BY_NPM=1 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    PYTHONDONTWRITEBYTECODE=1
COPY egress_proxy.py /opt/egress_proxy.py
ENTRYPOINT []
CMD ["codex", "--version"]
