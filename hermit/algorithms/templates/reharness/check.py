"""The template's check (check 6 of check_spec): index.json and every reflection file parse and agree with each
other (the retrieval mode, the embedding model and dimensions, file names and ids). Loaded from the template root,
run on the candidate adapter."""
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from reharness_core import bank  # noqa: E402


def check(adapter_path: str) -> list[dict]:
    return bank.validate(Path(adapter_path))
