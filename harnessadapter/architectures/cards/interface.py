"""Cards export bridge: the acting context is the compiled deck, nothing else."""
from pathlib import Path

from .cards_core import render

ROOT = Path(__file__).resolve().parent


def context():
    """The preamble plus every admitted card, within the deck's byte budget."""
    return render.compile(ROOT)
