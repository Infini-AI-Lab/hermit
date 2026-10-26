"""Check the assembled Pages artifact, including links between the homepage and docs."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


SITE_URL = "https://infini-ai-lab.github.io/hermit/"
ROOT = Path(__file__).resolve().parent / "_build" / "site"


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.add(attrs["id"])
        for key in ("href", "src"):
            if attrs.get(key):
                self.refs.append(attrs[key])
        if attrs.get("srcset"):
            self.refs.extend(item.strip().split()[0] for item in attrs["srcset"].split(","))


def main():
    pages = {}
    errors = set()
    for path in ROOT.rglob("*.html"):
        parser = Page()
        parser.feed(path.read_text())
        pages[path.resolve()] = parser

    for required in ("index.html", "docs/index.html", "assets/logo.svg"):
        if not (ROOT / required).is_file():
            errors.add(f"Missing site entry: {required}")

    for path, page in pages.items():
        for ref in page.refs:
            if ref.startswith(SITE_URL):
                ref, parent = ref[len(SITE_URL):], ROOT
            elif urlsplit(ref).scheme or ref.startswith("//"):
                continue
            else:
                parent = path.parent
            parsed = urlsplit(ref)
            target = (parent / unquote(parsed.path)).resolve() if parsed.path else path
            if target.is_dir():
                target /= "index.html"
            label = f"{path.relative_to(ROOT)}: {ref}"
            if not target.exists():
                errors.add(f"Missing file: {label}")
            elif parsed.fragment and target.suffix == ".html":
                target_page = pages.get(target)
                if target_page is None or unquote(parsed.fragment) not in target_page.ids:
                    errors.add(f"Missing anchor: {label}")

    if (ROOT / "docs/_static/drafts").exists():
        errors.add("Local design drafts must not be published")
    for path in ROOT.rglob("*"):
        if path.is_symlink():
            errors.add(f"Pages artifacts cannot contain symlinks: {path.relative_to(ROOT)}")
        if path.name == ".doctrees":
            errors.add(f"Build cache must not be published: {path.relative_to(ROOT)}")

    if errors:
        raise SystemExit("\n".join(sorted(errors)))
    print(f"Checked {len(pages)} HTML pages: local links, anchors, assets, and publication exclusions passed.")


if __name__ == "__main__":
    main()
