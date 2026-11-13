from pathlib import Path
from she_runtime import SHEDefense


def build():
    return SHEDefense(Path(__file__).parent)
