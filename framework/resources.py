"""Explicit evidence bindings; the evidence is outside the evolvable core."""
from pathlib import Path


class EvidenceStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        if not self.root.is_dir():
            raise ValueError("Evidence binding must be a directory")

    def list_records(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")
                      if p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(self.root))

    def read(self, address: str, offset: int = 0, limit: int = 30000):
        if not isinstance(address, str) or Path(address).is_absolute():
            raise ValueError("Evidence addresses must be relative")
        path = (self.root / address).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError(f"Unknown evidence address: {address}")
        if not isinstance(offset, int) or offset < 0 or not 1 <= limit <= 30000:
            raise ValueError("Invalid evidence window")
        text = path.read_text(encoding="utf-8")
        end = min(offset + limit, len(text))
        return {"source": address, "text": text[offset:end], "total_chars": len(text),
                "next_offset": end if end < len(text) else None}
