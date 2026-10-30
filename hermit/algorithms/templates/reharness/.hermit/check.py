"""The template's check (check 6 of check_spec): index.json and every reflection file parse and agree with each
other (the retrieval mode, the embedding model and dimensions, file names and ids). Loaded from the template root,
run on the candidate adapter."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent      # the adapter root, beside .hermit/: reharness_core lives there
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reharness_core import bank  # noqa: E402


def check(adapter_path: str) -> list[dict]:
    return bank.validate(Path(adapter_path))
