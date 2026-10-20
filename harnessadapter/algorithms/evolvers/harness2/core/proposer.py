"""Improver sessions: one `call(mission, root, label, harvest=...)` for every stage, on one of two channels.

- `llm` (this module): one metered call of the run's `llm` tool per attempt. The evidence under `root/context/` is
  inlined into the prompt under a size budget (trajectories are clipped to fit, with explicit markers; everything
  else is kept whole), and an editing stage replies with whole-file blocks that are applied to a copy of
  `root/context/harness/`. This is the channel GEPA, SkillOpt and the prompt-only Evolver use.
- `agent`: a sandboxed Codex or Claude Code session with `root` as its working directory, editing the harness in
  place (upstream's design). Not implemented yet; the Evolver refuses it with a clear message.

A Reply carries the text, the harvested harness directory (editing stages) and the cost. A custom proposer (for
tests, or another channel) is any object with the same `call` signature, named in `params.proposer`.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from . import surface as S

SYSTEM = ("You improve the harness of an autonomous agent: the instructions and skills it works with. You are given a "
          "mission and the evidence it refers to, inlined as files. Follow the mission exactly and reply in the format "
          "it asks for.")


@dataclass
class Reply:
    label: str
    text: str
    workspace: Path | None = None      # the harvested harness directory of an editing stage
    usd: float = 0.0
    attempts: int = 1
    error: str = ""


class ProposerError(RuntimeError):
    def __init__(self, message: str, usd: float = 0.0):
        super().__init__(message)
        self.usd = usd


class Proposer(Protocol):
    def call(self, mission: str, root: Path, label: str, *, harvest: str | None = None,
             expect: str | None = None) -> Reply: ...


# ---------------------------------------------------------------- inlining the evidence -------------------------

_TRAJECTORY = re.compile(r"^(rollouts/|pool/[^/]+/rollouts/)")


def _priority(rel: str) -> int:
    """Lower first. Trajectories (3) share what is left; whole candidate and library trees come last."""
    if rel.startswith(("library/",)):
        if rel == "library/INDEX.md" or rel.endswith("/EXPERIENCE.md"):
            return 2
        return 5
    if _TRAJECTORY.match(rel):
        return 3
    if re.match(r"^pool/[^/]+/harness/", rel):
        return 4
    if rel.startswith(("deliverables/", "candidates/")) or re.match(r"^pool/[^/]+/(produced/|harness\.diff)", rel):
        return 1
    return 0


def _clip_middle(text: str, limit: int, what: str) -> str:
    if len(text) <= limit:
        return text
    marker = f"\n\n[... {len(text) - limit:,} chars of this {what} omitted here to fit the inlined evidence; this marker is ours ...]\n\n"
    head = max(0, int(limit * 0.6))
    tail = max(0, limit - head)
    return text[:head] + marker + (text[-tail:] if tail else "")


def inline(root: Path, budget: int) -> tuple[str, dict]:
    """The files under root/context/ as one text, within `budget` characters; and a manifest of what was cut.

    Order: the task, harness, decisions and deliverables whole; the library's INDEX.md and the newest EXPERIENCE.md
    files within a fifth of the budget; the trajectories sharing what is left (short ones whole, long ones clipped in
    the middle with an explicit marker); whole candidate and library trees only if room remains."""
    ctx = Path(root) / "context"
    files = []
    for p in sorted(ctx.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            rel = p.relative_to(ctx).as_posix()
            files.append((rel, p.read_text(encoding="utf-8", errors="replace")))
    by = {lvl: [(r, t) for r, t in files if _priority(r) == lvl] for lvl in range(6)}
    manifest: dict = {"files": len(files), "clipped": [], "dropped": []}
    if by[2]:
        keep, used = [], 0
        index = [x for x in by[2] if x[0] == "library/INDEX.md"]
        newest_first = sorted((x for x in by[2] if x[0] != "library/INDEX.md"), key=lambda x: x[0], reverse=True)
        for r, t in index + newest_first:
            if r == "library/INDEX.md" or used + len(t) <= budget * 0.2:
                keep.append((r, t))
                used += len(t)
            else:
                manifest["dropped"].append(r)
        by[2] = keep
    fixed = sum(len(t) + len(r) + 30 for lvl in (0, 1, 2) for r, t in by[lvl])
    room = max(budget - fixed, 0)
    share: dict[str, int] = {}
    pending = sorted(by[3], key=lambda x: len(x[1]))
    left = room
    for i, (r, t) in enumerate(pending):
        fair = left // (len(pending) - i)
        share[r] = min(len(t), fair)
        left -= share[r]
    room = left
    out_parts = []
    for lvl in (0, 1, 2, 3, 4, 5):
        for r, t in by[lvl]:
            if lvl == 3:
                limit = share.get(r, len(t))
                if limit < len(t):
                    manifest["clipped"].append({"file": r, "chars": len(t), "kept": limit})
                t = _clip_middle(t, limit, "trajectory")
            elif lvl in (4, 5):
                if len(t) + len(r) + 30 > room:
                    manifest["dropped"].append(r)
                    continue
                room -= len(t) + len(r) + 30
            out_parts.append(f"===== context/{r} =====\n{t.rstrip()}\n")
    if manifest["dropped"]:
        out_parts.append("===== (not inlined) =====\nThese files exist but did not fit and are not shown: "
                         + ", ".join(manifest["dropped"][:40]) + (" ..." if len(manifest["dropped"]) > 40 else "")
                         + "\n")
    return "\n".join(out_parts), manifest


def clipping_note(manifest: dict) -> str:
    """What the mission must know when the inliner cut trajectories."""
    if not manifest.get("clipped"):
        return ""
    lines = [f"- context/{c['file']}: {c['kept']:,} of {c['chars']:,} chars shown" for c in manifest["clipped"][:30]]
    return ("# NOTE ON THE INLINED EVIDENCE\n\nTo fit this message, these trajectories are cut in the middle, at an "
            "explicit `[... omitted here ...]` marker. The cut is ours: events missing at the marker happened but are "
            "not shown, so never diagnose them as agent behaviour.\n\n" + "\n".join(lines) + "\n")


# ---------------------------------------------------------------- harvesting file blocks ------------------------

_FILE = re.compile(r"^[ \t]*<<<FILE:?[ \t]+`?([^\n`>]+?)`?[ \t]*(?:>>>)?[ \t]*\n(.*?)"
                   r"^[ \t]*(?:FILE>>>|>>>FILE|<<<END>>>|<<<END FILE>>>)[ \t]*$", re.S | re.M)
_OPEN = re.compile(r"^[ \t]*<<<FILE\b", re.M)
_DELETE = re.compile(r"^[ \t]*<<<DELETE:?[ \t]+`?([^\n`>]+?)`?[ \t]*>>>", re.M)
_FENCE = re.compile(r"\A(`{3,}|~{3,})[^\n]*\n(.*)\n\1[ \t]*\Z", re.S)


def _rel(path: str) -> str:
    """A reply's block path, relative to the harness root. The evidence shows the tree as `context/harness/`, whose
    always-on files sit in `harness/`, so models often write `harness/skills/...`: skills and agents are at the
    root, and that prefix is dropped."""
    rel = path.strip().strip("`'\"")
    for prefix in ("context/harness/", "harness_root/", "./"):
        if rel.startswith(prefix):
            rel = rel[len(prefix):]
    rel = S.normalize_rel(rel)
    for misplaced in ("harness/skills/", "harness/agents/"):
        if rel.startswith(misplaced):
            rel = rel[len("harness/"):]
    return rel


def parse_blocks(text: str) -> tuple[dict[str, str], list[str]]:
    """(path -> new content, paths to delete) from a reply's file blocks. Raises ValueError for a path that escapes
    the harness."""
    text = (text or "").replace("\r\n", "\n")
    files: dict[str, str] = {}
    for m in _FILE.finditer(text):
        body = m.group(2).rstrip("\n")
        fenced = _FENCE.match(body.strip("\n"))
        if fenced:
            body = fenced.group(2)
        files[_rel(m.group(1))] = (body.rstrip() + "\n") if body.strip() else ""
    deletes = [_rel(m.group(1)) for m in _DELETE.finditer(text)]
    return files, deletes


def apply_blocks(harness: Path, files: dict[str, str], deletes: list[str]) -> list[str]:
    """Apply blocks to a harness directory in place; return the problems to report back (none when all applied).
    ADAPTER.md and the template's own files are not editable; a DELETE of a skill directory removes the skill."""
    problems = []
    for rel in deletes:
        p = harness / rel
        if rel in S.FRAMEWORK_FILES:
            problems.append(f"`{rel}` cannot be deleted: it is not a harness component")
        elif p.is_file():
            p.unlink()
        elif p.is_dir() and rel.startswith("skills/") and rel.count("/") == 1:
            shutil.rmtree(p)
        else:
            problems.append(f"<<<DELETE {rel}>>> names no file of the harness")
    for rel, body in files.items():
        if rel in S.FRAMEWORK_FILES:
            problems.append(f"`{rel}` is not editable: ADAPTER.md is rendered from `harness/*.md` and the skills, so "
                            f"write those files instead")
            continue
        p = harness / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return problems


# ---------------------------------------------------------------- the llm channel -------------------------------

class LlmProposer:
    """Stages as metered calls of the run's `llm` tool."""

    def __init__(self, llm: Callable[..., dict], *, max_tokens: int = 32000, temperature: float | None = None,
                 inline_chars: int = 600_000, log_dir: Path | None = None):
        self.llm = llm
        self.max_tokens = int(max_tokens)
        self.temperature = temperature
        self.inline_chars = int(inline_chars)
        self.log_dir = Path(log_dir) if log_dir else None

    def _ask(self, messages: list[dict], max_tokens: int) -> tuple[str, float, str]:
        kwargs = {"messages": messages, "max_tokens": max_tokens}
        if self.temperature is not None:
            kwargs["temperature"] = float(self.temperature)
        reply = self.llm(**kwargs)
        usage = reply.get("usage") or {}
        return reply.get("content") or "", float(usage.get("cost_usd") or 0.0), reply.get("finish_reason") or ""

    def call(self, mission: str, root: Path, label: str, *, harvest: str | None = None,
             expect: str | None = None) -> Reply:
        """`expect`: a regular expression a complete non-editing reply must match (the selector's choice block,
        the reflector's closing tag); editing replies are checked for complete file blocks."""
        root = Path(root)
        evidence, manifest = inline(root, self.inline_chars)
        note = clipping_note(manifest)
        prompt = (f"{mission.rstrip()}\n\n{note}\n# THE EVIDENCE, INLINED\n\nEach file below is shown as "
                  f"`===== context/<path> =====` followed by its content.\n\n{evidence}")
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
        usd = 0.0
        texts: list[str] = []
        try:
            text, cost, finish = self._ask(messages, self.max_tokens)
            usd += cost
            texts.append(text)
            if not self._usable(text, finish, harvest, expect):
                # A reasoning model may spend its budget thinking (empty content) or be cut off mid-reply: ask
                # again with twice the room.
                messages += [{"role": "assistant", "content": text},
                             {"role": "user", "content": "Your reply was empty or cut off. Reply now with only the "
                              "final answer in the exact format the mission asks for" + (
                                  " (the file blocks, then the <notes> block)" if harvest else "") +
                              ". No analysis."}]
                text, cost, finish = self._ask(messages, self.max_tokens * 2)
                usd += cost
                texts.append(text)
        except Exception as exc:
            from harnessadapter.tools import ToolError
            from harnessadapter.trainer.budget import BudgetExceeded
            if isinstance(exc, (BudgetExceeded, ToolError)):
                raise
            raise ProposerError(f"{type(exc).__name__}: {exc}", usd) from None
        finally:
            self._log(label, mission, manifest, texts)
        if not self._usable(text, finish, harvest, expect):
            return Reply(label, text, None, usd, len(texts),
                         error=f"the reply was empty or cut off (finish_reason {finish or 'unknown'}) after a retry "
                               f"with twice the room")
        workspace = None
        if harvest:
            src = root / harvest
            workspace = root / "proposal"         # outside context/, so a retry's evidence does not include it
            if workspace.exists():
                shutil.rmtree(workspace)
            shutil.copytree(src, workspace) if src.is_dir() else workspace.mkdir(parents=True)
            try:
                files, deletes = parse_blocks(text)
                problems = apply_blocks(workspace, files, deletes)
            except ValueError as exc:      # a path that escapes the harness
                return Reply(label, text, None, usd, len(texts), error=str(exc))
            if problems:
                return Reply(label, text, None, usd, len(texts), error="; ".join(problems))
        return Reply(label, text, workspace, usd, len(texts))

    @staticmethod
    def _usable(text: str, finish: str, harvest: str | None, expect: str | None) -> bool:
        if not text.strip():
            return False
        if harvest:
            normal = text.replace("\r\n", "\n")
            opened = len(_OPEN.findall(normal))
            closed = len(_FILE.findall(normal))
            if opened != closed:
                return False
            if finish == "length" and "</notes>" not in normal:
                return False
            return "<notes>" in normal or "KEEP_BASE" in normal or closed > 0
        if expect:
            return re.search(expect, text, re.S) is not None
        return finish != "length"

    def _log(self, label: str, mission: str, manifest: dict, texts: list[str]) -> None:
        if self.log_dir is None:
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)
        (self.log_dir / f"{stem}.mission.md").write_text(mission, encoding="utf-8")
        (self.log_dir / f"{stem}.inlined.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        for i, t in enumerate(texts):
            (self.log_dir / f"{stem}.reply{i}.md").write_text(t, encoding="utf-8")
