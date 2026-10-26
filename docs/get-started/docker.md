# Run in Docker

For environments that only accept a prebuilt container (e.g. Kubernetes) with no shell-in install access, the
repository ships a `Dockerfile` that bakes the framework and its environment into one image. The build fetches the
git-pinned `gepa` and `skillopt` evolvers; the built image installs nothing at run time.

```bash
docker build -t hermit:0.2.0 .
docker run --rm hermit:0.2.0 --help          # lists check / train / eval / deploy / convert / serve
```

The image is `python:3.11-slim` based, runs as a non-root user, and includes `git` (a runtime dependency: the
Trainer versions each adapter as git commits). `ENTRYPOINT` is `hermit`, so pass subcommands directly. Mount a
writable directory for run output:

```bash
docker run --rm -v "$PWD/runs:/work/runs" -w /work -e FIREWORKS_API_KEY \
  hermit:0.2.0 train --config /opt/hermit/examples/officeqa/gepa/smoke.yaml
```

For a minimal image without the external evolvers, change the install line in the `Dockerfile` to
`pip install --no-cache-dir -e .` (dropping the two `-r requirements-*.txt`). The built-in evolvers
(`continual_harness`, `claude`, `codex`, `mwm`) and all templates ship with the package and need no extra. The
`continual_harness` and `mwm` evolvers in the Codex harness additionally need the Codex CLI, which this image does
not include.

## Two things the image cannot provide

- **Model egress.** `train` and `eval` call your model endpoint over the network. A fully air-gapped node runs only
  the offline/deterministic [quickstart](quickstart.md); real-model workflows need the endpoint reachable at run time
  (pass the credential with `-e`, e.g. `FIREWORKS_API_KEY`).
- **A real sandbox.** The image deliberately omits bubblewrap. hermit's sandbox needs user namespaces, which
  clusters commonly deny; and bubblewrap *present but unable to make a namespace* (the nested-in-container case) makes
  `best_effort` try it and crash, where absent bubblewrap makes `best_effort` fall back to unconfined. So with this
  image, run with `sandbox.enforce=best_effort` (the acting pipeline and learner run unconfined); `sandbox.enforce=required`
  gives a clean "install bubblewrap or use best_effort" error. For real isolation on a node that allows (nested) user
  namespaces, derive an image that `apt-get install -y bubblewrap`, run it with a permissive `securityContext`, and set
  `sandbox.enforce=required`. See [sandbox boundaries](../framework/configuration.md#sandbox-network-and-warnings).
