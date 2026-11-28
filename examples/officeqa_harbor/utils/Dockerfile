# Harbor's corpus image with what Harbor would install in each trial: its install at 16 trials at once outlasts the setup
# timeout and exhausts the memory of a small machine. prepare.py builds it and pins the base image's digest and Codex's
# version (the apt packages are Ubuntu's at build time). The check's own ~/.codex is removed: a Codex temp dir left in
# the image makes `codex --version` warn on overlayfs (it cannot move the dir), and Harbor, which reads the version
# from the command's merged output, then installs Codex again in each trial, over the network, past the setup timeout.
ARG BASE
FROM ${BASE}
ARG CODEX_VERSION
RUN apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    ca-certificates curl nodejs npm ripgrep \
 && npm install -g --silent "@openai/codex@${CODEX_VERSION}" \
 && codex --version \
 && rm -rf /var/lib/apt/lists/* /root/.npm /root/.codex
