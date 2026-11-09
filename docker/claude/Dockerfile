# The Claude Code agent's container (hermit/agent/base/container.py): Ubuntu 24.04, python3 (for the session's
# scripts), ripgrep, bubblewrap and socat (Claude Code's own sandbox, which runs the session's commands, needs both), and
# the machine's own Claude Code, its native build (one file), copied here first:
#
#   cp ~/.local/share/claude/versions/2.1.318 docker/claude/claude
#   docker build --build-context codex=docker/codex -t officeqa-claude:2.1.318 docker/claude
#
# A run names the tag it built (environment.image, or a CLI model endpoint's container.image); a session given none
# takes HERMIT_CLAUDE_IMAGE, else hermit-claude:2.1.318 (container.py's default).
#
# The egress proxy's script is the Codex images' (docker/codex/egress_proxy.py, the `codex` build context), so an
# egress container can run this image too. The CLI does not update itself here (DISABLE_AUTOUPDATER): the image is
# the version it is named for. It is root's and execute-only (0711) for the session's user: the kernel then makes the
# running CLI undumpable, so nothing else that user runs in the container (a hook, a deployment's MCP server) can read
# its memory (the token) or its descriptors (the session's stream) through /proc.
ARG BASE=ubuntu:24.04
FROM ${BASE}
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 \
        ca-certificates ripgrep bubblewrap socat && rm -rf /var/lib/apt/lists/*
COPY --chmod=711 claude /usr/local/bin/claude
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    DISABLE_AUTOUPDATER=1 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    PYTHONDONTWRITEBYTECODE=1
COPY --from=codex egress_proxy.py /opt/egress_proxy.py
ENTRYPOINT []
CMD ["claude", "--version"]
