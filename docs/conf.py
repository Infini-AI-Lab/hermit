"""Sphinx configuration for the MetaHarnessAdapter documentation.

Build: bash docs/build.sh  ->  docs/_build/html
Live preview: bash docs/serve.sh
"""
from datetime import datetime
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]

project = "MetaHarnessAdapter"
author = "MetaHarnessAdapter contributors"
copyright = f"2026-{datetime.now().year}, {author}"
release = version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]

extensions = [
    "myst_parser",
    "sphinx_copybutton",
    "sphinxcontrib.mermaid",
]

source_suffix = {".md": "markdown"}
root_doc = "index"
exclude_patterns = ["_build", "_includes", "_examples", "README.md", "Thumbs.db", ".DS_Store"]

myst_enable_extensions = ["colon_fence", "deflist", "html_image", "substitution"]
myst_heading_anchors = 3
# ```mermaid fences render as diagrams.
myst_fence_as_directive = ["mermaid"]

html_theme = "shibuya"
html_title = f"{project} {version}"
html_static_path = ["_static", "../assets"]
html_css_files = ["css/custom.css", "css/overview-diagrams.css"]
html_js_files = ["js/component-explorer.js", "js/overview-diagrams.js"]
html_favicon = "../assets/logo.svg"
html_theme_options = {
    "accent_color": "orange",
    "light_logo": "_static/wordmark-light.svg",
    "dark_logo": "_static/wordmark-dark.svg",
    "github_url": "https://github.com/haizhongzheng/MetaHarnessAdapter",
    "nav_links": [
        {"title": "Quick Start", "url": "get-started/quickstart"},
        {"title": "Methods", "url": "methods/index"},
        {"title": "Recipes", "url": "recipes/index"},
        {"title": "Reference", "url": "reference/config-fields"},
    ],
}
html_context = {
    "source_type": "github",
    "source_user": "haizhongzheng",
    "source_repo": "MetaHarnessAdapter",
    "source_version": "main",
    "source_docs_path": "/docs/",
}
html_copy_source = False
