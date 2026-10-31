"""OfficeQA for the framework's Codex agent (`agent.name: codex`, hermit/agent/acting/workspace.py): what is OfficeQA's own.

    agent:
      name: codex
      params:
        prompt: ../utils/prompt.md
        prepare: ../utils/task.py:prepare      # task.md and the corpus
        answer: ../utils/task.py:answer        # the last <answer>...</answer>
        collect: ../utils/task.py:collect      # task.md into the log
        reserved: [task.md, docs]              # deploy: files: names an adapter may not use

The workspace holds the question as `task.md` (the question with its oracle parsed pages and its source files as
hints: the text SkillOpt's own Codex harness writes) and the whole corpus under `docs/` (read-only). The corpus is
bound at its own path too, so that its links (prepare.py may link `transformed/` and `jsons/`) resolve. The corpus
is `agent.params.docs`, else ../data.json's (utils/prepare.py writes it).
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
ANSWER = re.compile(r"<answer>(.*?)</answer>", re.IGNORECASE | re.DOTALL)
CORPUS_NOTE = ("The full OfficeQA document corpus is available under `docs/`. The candidate files below are source "
               "hints or likely starting points; search the full corpus if needed.")


def corpus(params: dict, base: Path) -> Path:
    docs = params.get("docs") or json.loads((HERE / "data.json").read_text())["docs"]
    path = Path(docs).expanduser()
    return (path if path.is_absolute() else Path(base) / path).resolve()


def task_text(task: dict, docs: Path) -> tuple[str, list[str]]:
    """`task.md` as SkillOpt's Codex harness writes it (its `_build_user`: the question, its oracle parsed pages,
    the corpus note, the candidate files as workspace paths, the source hints), and the corpus roots."""
    from skillopt.envs.officeqa.rollout import _build_user, _workspace_doc_path
    from skillopt.envs.officeqa.tool_runtime import (build_oracle_parsed_pages_context, resolve_candidate_files,
                                                     resolve_docs_roots)
    roots = resolve_docs_roots([str(docs)])
    oracle = build_oracle_parsed_pages_context(task["source_files"], task["source_docs"], roots)
    if not oracle:
        raise RuntimeError(f"no parsed pages for {task['id']} under {docs}: see utils/prepare.py")
    files = resolve_candidate_files(task["source_files"], roots)
    item = {"id": task["id"], "question": task["question"], "task_type": task.get("category", "officeqa"),
            "source_files": task["source_files"], "source_docs": task["source_docs"]}
    text = _build_user(item, [_workspace_doc_path(f, roots) for f in files[:20]], corpus_note=CORPUS_NOTE,
                       oracle_context=oracle)
    return text, roots


def corpus_mounts(docs: Path) -> list[Path]:
    """The corpus and whatever its `transformed/` and `jsons/` link to outside it, each bound at its own path."""
    docs = Path(docs).resolve()
    extra = {p.resolve().parent if p.resolve().is_dir() else p.resolve() for p in (docs / "transformed", docs / "jsons")
             if p.is_symlink()}
    return [docs, *sorted(m for m in extra if not m.is_relative_to(docs) and not docs.is_relative_to(m))]


def prepare(task: dict, workspace: Path, params: dict, base: Path) -> list[dict]:
    """task.md, and the corpus: at its own paths and at docs/root_1 (a real directory in a container, which `find`
    and `rg --files` descend into); further roots, if any, linked at docs/root_<n>."""
    docs = corpus(params, base)
    for sub in ("transformed", "jsons"):
        if not (docs / sub).is_dir():
            raise RuntimeError(f"the corpus {docs} lacks {sub}/: see utils/prepare.py")
    text, roots = task_text(task, docs)
    (workspace / "task.md").write_text(text, encoding="utf-8")
    for i, root in enumerate(roots[1:], start=2):
        (workspace / "docs").mkdir(exist_ok=True)
        (workspace / "docs" / f"root_{i}").symlink_to(Path(root).resolve())
    return [*({"source": m, "at": str(m)} for m in corpus_mounts(docs)), {"source": roots[0], "at": "docs/root_1"}]


def answer(last_message: str, task: dict) -> dict:
    """The text inside the last <answer>...</answer> of the agent's final message."""
    found = ANSWER.findall(last_message)
    return {"answer": found[-1].strip() if found else ""}


def collect(task: dict, workspace: Path, log: Path) -> dict:
    """task.md beside the session's stream, so that the log holds the whole run."""
    if (workspace / "task.md").is_file():
        shutil.copy2(workspace / "task.md", log / "task.md")
    return {}
