"""Native role discovery from original MWM SPEC.md bundles."""
import os
ENABLED = True

def list_specs(wrepo: str) -> list:
    """(name, first line of SPEC.md) for every spec."""
    root = os.path.join(wrepo, "agents")
    out = []
    if not os.path.isdir(root):
        return out
    for d in sorted(os.listdir(root)):
        if d.startswith(("_", ".")):
            continue
        p = os.path.join(root, d, "SPEC.md")
        if not os.path.isfile(p) or not os.path.realpath(p).startswith(os.path.realpath(wrepo) + os.sep):
            continue
        first = ""
        try:
            for line in open(p, errors="replace"):
                line = line.strip().lstrip("#").strip()
                if line:
                    first = line
                    break
        except OSError:
            pass
        out.append((d, first))
    return out
