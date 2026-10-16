"""OfficeQA task tools for the built-in react agent: full-text search over the bulletins and reading one file.

The prepared OfficeQA directory is named in data_root.txt (written by prepare.py). The agent loop itself is
`harnessadapter.agents.react`; see methods/*/smoke.yaml.
"""
from pathlib import Path

from harnessadapter.agents import tool

HERE = Path(__file__).resolve().parent
PREPARED = Path((HERE / "data_root.txt").read_text().strip())
_corpus = None


def corpus():
    global _corpus
    if _corpus is None:
        from corpus import OfficeQACorpus
        _corpus = OfficeQACorpus(PREPARED / "agent" / "corpus", PREPARED / "agent" / "corpus.sqlite")
    return _corpus


@tool("Full-text search (BM25) over the bulletins; returns filenames, line ranges and excerpts.",
      {"type": "object", "properties": {"query": {"type": "string"},
                                        "filename_glob": {"type": "string", "description": "e.g. treasury_bulletin_1995_*"},
                                        "limit": {"type": "integer"}}, "required": ["query"]})
def search(query: str, limit: int = 8, filename_glob: str = "*.txt") -> dict:
    return corpus().search(query, int(limit), filename_glob)


@tool("Read lines of one bulletin file.",
      {"type": "object", "properties": {"filename": {"type": "string"}, "start_line": {"type": "integer"},
                                        "num_lines": {"type": "integer"}}, "required": ["filename"]})
def read(filename: str, start_line: int = 1, num_lines: int = 80) -> dict:
    return corpus().read(filename, int(start_line), int(num_lines))
