"""Read-only LearningMaterial bindings outside the evolvable core."""
from pathlib import Path


class LearningMaterial:
    def __init__(self, root: Path):
        self.root = root.resolve()
        if not self.root.is_dir():
            raise ValueError("LearningMaterial binding must be a directory")

    def list_records(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")
                      if p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(self.root))

    def read(self, address: str, offset: int = 0, limit: int = 30000):
        if not isinstance(address, str) or Path(address).is_absolute():
            raise ValueError("LearningMaterial addresses must be relative")
        path = (self.root / address).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError(f"Unknown LearningMaterial address: {address}")
        if not isinstance(offset, int) or offset < 0 or not 1 <= limit <= 30000:
            raise ValueError("Invalid LearningMaterial window")
        text = path.read_text(encoding="utf-8")
        end = min(offset + limit, len(text))
        return {"source": address, "text": text[offset:end], "total_chars": len(text),
                "next_offset": end if end < len(text) else None}


# Compatibility for existing adapters and clients.
EvidenceStore = LearningMaterial


def resolve_learning_material(learning_material=None, evidence=None):
    """Resolve the public name or the legacy alias without silently overriding input."""
    if learning_material is not None and evidence is not None:
        raise ValueError('Specify learning_material or legacy evidence, not both')
    value = learning_material if learning_material is not None else evidence
    return Path(value) if value is not None else None
