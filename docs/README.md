# MetaHarnessAdapter docs

The documentation site is built with [Sphinx](https://www.sphinx-doc.org/), the [Shibuya](https://shibuya.lepture.com/) theme and [MyST](https://myst-parser.readthedocs.io/) Markdown. Every page is a Markdown file in this directory.

```sh
pip install -r docs/requirements.txt
bash docs/build.sh          # -> docs/_build/html
bash docs/serve.sh 8000     # live preview at http://127.0.0.1:8000
```

`build.sh` treats warnings as errors, so a broken link or a page missing from the navigation fails the build.

## Adding a page

1. Write the Markdown file, for example `docs/guide/my-topic.md`. Its first `#` heading is the page title.
2. List it in a `toctree` in `docs/index.md` under the right section. Link to other pages by their `.md` path.

Use Get Started for installation and the shortest walkthrough, Framework Components for relationships and contracts,
Recipes for task-specific setup, Developer Guide for extensions, and Reference for configuration and commands.

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
