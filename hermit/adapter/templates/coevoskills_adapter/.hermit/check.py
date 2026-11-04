"""Template check for `coevoskills-adapter` adapters: `skills/` holds only skill directories, each a package that
passes upstream's Skill schema (the authors' validator, vendored unmodified as skill_schema.py: a frontmatter with
`name` and `description`, the name equal to its directory, lowercase letters, digits and single hyphens, at most 64
characters), and nothing in the adapter is a link."""
from pathlib import Path
import importlib.util
import sys


def check(adapter_path: str) -> list[dict]:
    root = Path(adapter_path)
    spec = importlib.util.spec_from_file_location("_coevoskills_skill_schema",
                                                  Path(__file__).with_name("skill_schema.py"))
    schema = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = schema
    spec.loader.exec_module(schema)
    issues = [{"path": str(p.relative_to(root)), "problem": "links are not allowed in the adapter"}
              for p in root.rglob("*") if p.is_symlink()]
    bank = root / "skills"
    if not bank.is_dir() or bank.is_symlink():
        return issues
    for entry in sorted(bank.iterdir()):
        rel = str(entry.relative_to(root))
        if not entry.is_dir() or not (entry / "SKILL.md").is_file():
            issues.append({"path": rel, "problem": "skills/ holds only skill directories, each with a SKILL.md"})
            continue
        issues += [{"path": rel + "/SKILL.md", "problem": i.message}
                   for i in schema.validate_skill_path(entry / "SKILL.md")]
    return issues
