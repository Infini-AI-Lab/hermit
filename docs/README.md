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

Dated notes that are kept for the record go in the hidden "Design notes" toctree at the bottom of `docs/index.md`.
