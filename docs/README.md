# Hermit docs

The documentation site is built with [Sphinx](https://www.sphinx-doc.org/), the [Shibuya](https://shibuya.lepture.com/) theme and [MyST](https://myst-parser.readthedocs.io/) Markdown. Every page is a Markdown file in this directory.

```sh
pip install -r docs/requirements.txt
bash docs/build.sh          # -> docs/_build/html
bash docs/serve.sh 8000     # live preview at http://127.0.0.1:8000
```

`build.sh` treats warnings as errors, so unresolved Sphinx references and pages missing from navigation fail the build. Raw HTML links and Markdown URL anchors also need verification; the strict build is not an external-link checker.

## Published site

The project homepage is at <https://infini-ai-lab.github.io/hermit/> and the documentation is at
<https://infini-ai-lab.github.io/hermit/docs/>. GitHub Pages uses the Actions publishing source.
The `Deploy project site and docs` workflow builds and deploys changes from `main`; it can also be started
manually from the Actions tab.

To build the same artifact locally:

```sh
bash docs/build-pages.sh
python3 docs/check-pages.py
python3 -m http.server 8000 --bind 127.0.0.1 --directory docs/_build/site
```

Open <http://127.0.0.1:8000/> for the homepage or <http://127.0.0.1:8000/docs/> for the docs.
The artifact contains `docs/web/index.html` as its homepage, repository `assets/`, and the strict Sphinx build
under `docs/`. Local design drafts under `_static/drafts/` are excluded.

## Adding a page

1. Write the Markdown file, for example `docs/guide/my-topic.md`. Its first `#` heading is the page title.
2. List it in a `toctree` in `docs/index.md` under the right section. Link to other pages by their `.md` path.

Use Get Started for installation and the complete first learning cycle; User Guide for task-oriented operations;
Learning Methods for selection, setup, and method defaults; Recipes for benchmark-specific protocols;
Developer Guide for extensions and component contracts; Reference for fields, commands, glossary, and limits.
Keep proposed behavior in the separate service design record and label it as unavailable.

The first-training example is a deterministic contract demonstration, not a model-learning benchmark. It keeps
answers out of task rows, learns one rule from scored feedback, and has expected none/root/final means of 0/0/1.
Run its check, train, three eval selectors, and deployment when changing the example or walkthrough.
Use a fresh run name after completion. Copy-based tutorials should write only under their disposable directories.

Method parameter tables use literalinclude from their evolver.yaml files. The full field reference also includes
the framework defaults directly. Verify descriptions against the consumer code: a free-form merge section does
not imply that every client constructor argument is forwarded by the Trainer.

Every recipe must state requirements, data preparation, model setup, launch command, output locations,
evaluation/scoring protocol, runtime/cost evidence or uncertainty, and reproduction limits. Link measured results
to included cards; keep smoke-test evidence distinct from full experiments.

Component pages explain responsibility, inputs and outputs, ownership and lifecycle, interactions, and the reason
for the interface boundaries before presenting implementation details. Each includes a small local interface
diagram and a minimal example. Python type excerpts use `literalinclude` from the implementation.
The offline examples under `docs/_examples/` exercise the pipeline, scoring, Evolver state, and Trainer without
model calls. Run their `check`, `train`, and `eval` commands as documented on the Trainer page after changing them.
The separate `first_model/` example makes real model calls when executed normally; validate it with a simulated
provider during documentation checks unless a real-model run is explicitly intended. Keep opaque task ids so
labels do not leak through naming, and test both shared and separate actor/learner settings.

The overview embeds `docs/_includes/adapter-use-interactive.html` and `adapter-evolve-interactive.html` through
MyST's `raw html` directive. Components are native HTML buttons; only the connectors are inline SVG. Interactions
live in `docs/_static/js/overview-diagrams.js`, with scoped styles in `docs/_static/css/overview-diagrams.css`.
Clicking a node highlights its direct connections and displays the explanation below it. Show all or Escape resets
the selection; Enlarge toggles full-scale rendering. Check both diagrams, keyboard controls, and narrow layouts.
Keep arrows aligned with the host connectors, adapter runtime, and Trainer's actual call flow.

Overview diagrams describe component responsibilities and information flow across tasks, learning methods, and
single- or multi-agent pipelines. Use capability-level labels. Put particular protocols, host commands, file paths,
and example-specific behavior in the lifecycle guide or recipes. Distinguish the general abstraction from current
implementation support; an example interaction must not become the definition of a component.
The lifecycle guide includes `docs/_includes/component-explorer.html`. Its interactions live in
`docs/_static/js/component-explorer.js`; its styles are scoped under `.ha-explorer` in `custom.css`.
Keep the explanation in the page usable without JavaScript. Check both diagram views at desktop and mobile widths
after editing their layout or labels.
