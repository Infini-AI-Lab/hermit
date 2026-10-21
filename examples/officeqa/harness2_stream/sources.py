"""An OfficeQA question's source material for Harness²'s editor sessions (upstream copies it into context/source/):
the bulletin text files the question's agent may open, found by SkillOpt's own lookup over data.json's docs."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def task_files(task: dict, dest: Path) -> Path:
    from skillopt.envs.officeqa.tool_runtime import resolve_candidate_files, resolve_docs_roots
    docs = json.loads((HERE.parent / "utils" / "data.json").read_text())["docs"]
    files = resolve_candidate_files(task["source_files"], resolve_docs_roots([docs]))
    if not files:
        raise FileNotFoundError(f"no source files for {task['id']} under {docs}")
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        link = dest / Path(f).name
        if not link.exists():
            link.symlink_to(Path(f).resolve())
    return dest
