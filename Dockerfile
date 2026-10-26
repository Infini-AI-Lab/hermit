# Run the hermit framework (check / train / eval / deploy / convert / serve) from a self-contained image, for
# environments that only accept a prebuilt container (e.g. Kubernetes) with no shell-in install access.
#
# The build needs network — it fetches the git-pinned gepa and skillopt evolvers — but the built image installs
# nothing at run time. Two things the image cannot provide; your runtime must still arrange them:
#   * Model egress — train/eval call your model endpoint over the network. A fully air-gapped node runs only the
#     offline/deterministic tutorial (docs/get-started/quickstart.md).
#   * A real sandbox — bubblewrap is deliberately NOT installed (see below). The image runs the acting pipeline and
#     the learner unconfined via sandbox.enforce=best_effort; that is what works on a restricted container.
#
#   docker build -t hermit:0.2.0 .
#   docker run --rm hermit:0.2.0 --help
#   docker run --rm -v "$PWD/runs:/work/runs" -w /work -e FIREWORKS_API_KEY hermit:0.2.0 \
#       check --config /opt/hermit/docs/_examples/first_training/run.yaml
#
# On the bubblewrap decision: hermit's sandbox needs user namespaces, which clusters commonly deny — and worse,
# bubblewrap *installed but unable to make a namespace* (the nested-in-container case) makes best_effort try it and
# crash, instead of falling back to unconfined (which it does only when bwrap is absent: hermit/sandbox.py). So this
# image omits bubblewrap: best_effort then runs unconfined everywhere, and sandbox.enforce=required gives a clean
# "install bubblewrap or use best_effort" error. For real isolation on a node that allows (nested) user namespaces,
# derive an image that adds `bubblewrap` and run it with a permissive securityContext + sandbox.enforce=required.
FROM python:3.11-slim

# git is a RUNTIME dependency: the Trainer versions each adapter as git commits (hermit/tree.py).
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/hermit
COPY . .
# Bake the framework plus the gepa and skillopt evolvers (git-pinned in requirements-*.txt) into the image. For a
# minimal image, drop the two -r files: `pip install --no-cache-dir -e .`. The built-in evolvers (continual_harness,
# claude, codex, mwm) and all templates ship with the package and need no extra; harness2 (requirements-harness2.txt)
# is left out of the default image.
RUN pip install --no-cache-dir -e . -r requirements-gepa.txt -r requirements-skillopt.txt

# Non-root (many clusters require runAsNonRoot). The CLI and the adapter git tree work fine as non-root; point run
# output at a writable working directory you mount (e.g. -w /work with a volume).
RUN useradd --create-home --uid 1000 hermit && chown -R hermit:hermit /opt/hermit
USER hermit
ENV HOME=/home/hermit

ENTRYPOINT ["hermit"]
CMD ["--help"]
