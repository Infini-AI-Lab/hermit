"""Adapter loading: manifest, in-process exports, evidence bindings."""
from .loader import Adapter, load_manifest, inside
from .resources import EvidenceStore

__all__ = ["Adapter", "load_manifest", "inside", "EvidenceStore"]
