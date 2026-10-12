"""MwmEvolver: the MWM learner as an in-process Evolver.

Each step is one MWM review session. The learner model (default GLM 5.3 Flash on Fireworks; any OpenAI-compatible model
that accepts images) runs through this Evolver's own ChatClient: no system message, the filled `prompt.md` as the only
user message, the eight file tools (`tools.json`), no tool_choice, the sampling settings of params (default
temperature 0, top_k 40, reasoning_effort 4096), no retries. The Trainer cannot stop an in-process call, so the caps are
enforced here (900 s, 64 requests, 120 requested tools, 65,536 completion tokens, 32,768 per response) and `ctx.cancel`
is checked before every request and tool.

Areas the tools see:
  adapter      read/write  the workspace adapter; writes only to approved Markdown paths (check_mutation)
  evidence     read-only   rebuilt each step from this run's learning material: every earlier run of the case
  history      read-only   the adapter as this session found it
  method       read-only   method/ of this directory (the pattern cards without their demo programs)
  environment  read-only   environment/README.md
  scratch      read/write  text files only; discarded with the step
`validate` runs the Trainer's check_spec. Evidence never contains a score, a judge record or another case. Runs are
labelled by what they used: B for a harness run (no adapter, or the untouched W0), Cn for an adapted run with the
adapter of session n (normally B, C1..C(k-1) at step k; a session that failed or changed nothing makes the next run use
the earlier adapter again, labelled B-2 or Cn-2).

Written to ctx.workspace.output: report.md, prompt.md, transcript.md, changes.diff, evidence-manifest.json and
learner/{result.json, final.md, calls/NN.json, tool-results/NNN.json}. reasoning_content stays in memory.
With params.rounds = R, a step after R is a measurement round: no model call, no edits, no usage. A step whose own
run left no record (the pipeline raised) makes no learner call.

get_policy reports backend "in-process". The prompt's notice slot also carries the Trainer's repair notice when
check_spec restored files after the last session. A run whose final capture failed is listed with a note in the
evidence README. A failed request's recorded error holds the first 500 characters of the HTTP body (ChatClient's limit).
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time

from jsonschema import Draft202012Validator
import yaml

from harnessadapter.evolver.base import Evolver, StepContext, StepResult, Usage
from harnessadapter.llm import ChatClient

HERE = Path(__file__).resolve().parent
DEFAULTS = {"rounds": None, "base_url": "https://api.fireworks.ai/inference/v1",
            "model": "accounts/fireworks/models/glm-5p3-flash", "api_key_env": "FIREWORKS_API_KEY",
            "price": {"input": 0.15, "cached_input": 0.03, "output": 0.50},
            "max_seconds": 900, "max_requests": 64, "max_tools": 120, "max_completion": 65536,
            "max_response": 32768, "reply_limit_bytes": 30000,
            "temperature": 0, "top_k": 40, "presence_penalty": 0, "frequency_penalty": 0, "reasoning_effort": 4096,
            "cases_file": "../../../tasks/visual_showcase/cases.json"}
SAMPLING = ("temperature", "top_k", "presence_penalty", "frequency_penalty", "reasoning_effort")   # null: not sent
REQUEST_TIMEOUT = 420           # seconds per request, never beyond the session deadline
MAX_IMAGE_CHARS = 8_000_000     # base64 characters of the latest image batch
MAX_IMAGE_BYTES = 4 * 1024 * 1024
IMAGE_REPLY_LIMIT = 6 * 1024 * 1024
MAX_BRIEF = 6000
ALLOWED_TOOLS = ("list_files", "read_file", "read_image", "write_file", "delete_path", "move_path", "validate",
                 "get_policy")
PRIVATE_FIELDS = ("reasoning_content", "api_key", "Authorization")
SCORE_FIELDS = frozenset(("judge", "score", "scores", "mean_score", "rubric_score", "mechanical_score", "mechanical",
                          "feedback", "verdict", "judge_usage", "judge_cost_usd", "judge_model"))
VALIDATE_SCOPE = ("Template specification check (check_spec): fixed files, learnable paths and formats, manifest, "
                  "deployment and the template's own check. No performance claim.")
PNG = b"\x89PNG\r\n\x1a\n"


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


# ---------- write rules (old learning.py: safe_file, allowed_candidate_path, check_mutation) ----------

def safe_file(root, relative, *, existing=True):
    root = Path(root).absolute()
    if root.resolve() != root:
        raise ValueError("Root must not contain symlinks")
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("Use a nonempty relative POSIX file path")
    parts = relative.split("/")
    if any(not p or p in (".", "..") or p.startswith(".") for p in parts):
        raise ValueError("Hidden, absolute or traversal paths are not admitted")
    path = root.joinpath(*parts)
    if path.resolve() != path or not path.is_relative_to(root):
        raise ValueError("Symlinks or escaping paths are not admitted")
    if path.exists() and not path.is_file():
        raise ValueError("Only regular files are admitted")
    if existing and not path.is_file():
        raise ValueError(f"Missing regular file: {relative}")
    return path


def allowed_candidate_path(name):
    parts = name.split("/") if isinstance(name, str) else []
    if not parts or any(not p or p in (".", "..") or p.startswith(".") for p in parts):
        return False
    if not name.endswith(".md") or "\\" in name:
        return False
    return (len(parts) >= 2 and parts[0] in ("memory", "examples") or
            name in ("consumption/brief.md", "meta/journal.md", "meta/journal-archive.md") or
            len(parts) >= 3 and parts[:2] == ["meta", "cases"])


def check_mutation(roots, name, arguments):
    """`roots` maps the writable areas (adapter, scratch) to their directories."""
    if name not in ("write_file", "delete_path", "move_path"):
        return
    area = arguments.get("area")
    if area not in ("adapter", "scratch"):
        raise ValueError("Only prose candidate files and isolated text scratch are writable")
    names = ([arguments.get("source"), arguments.get("destination")]
             if name == "move_path" else [arguments.get("path")])
    for i, relative in enumerate(names):
        if area == "adapter" and not allowed_candidate_path(relative):
            raise ValueError("Candidate writes are limited to approved Markdown knowledge paths")
        if area == "scratch" and (not isinstance(relative, str) or
                                  Path(relative).suffix not in (".md", ".txt", ".json", ".csv", ".log")):
            raise ValueError("Scratch accepts text .md/.txt/.json/.csv/.log files only")
        existing = name == "delete_path" or name == "move_path" and i == 0
        safe_file(roots[area], relative, existing=existing)
    if name == "write_file":
        content = arguments.get("content")
        if not isinstance(content, str) or len(content.encode()) > 1_000_000 or "\x00" in content:
            raise ValueError("Write content must be UTF-8 text at most 1 MB")
        if area == "adapter" and arguments.get("path") == "consumption/brief.md":
            if len(content) > MAX_BRIEF:
                raise ValueError(f"Brief has {len(content)} characters; maximum is {MAX_BRIEF}. "
                                 "Shorten it before writing.")
            if len(content.encode()) > MAX_BRIEF:     # the template caps bytes; a longer brief would be repaired away
                raise ValueError(f"Brief has {len(content.encode())} UTF-8 bytes; maximum is {MAX_BRIEF}. "
                                 "Shorten it before writing.")


# ---------- the file capabilities (old evolution/workspace.py + policy/operation.py) ----------

class ToolFailure(Exception):
    """An error inside a tool; the model sees it as the old MCP error text."""


class Areas:
    def __init__(self, roots: dict, check_spec=None, reply_limit: int | None = 30000):
        self.areas = {name: Path(p).resolve() for name, p in roots.items()}
        self.check_spec = check_spec
        self.reply_limit = reply_limit

    def path(self, area, relative, write=False):
        if area not in self.areas or (write and area not in ("adapter", "scratch")):
            raise ValueError("Read: adapter/evidence/scratch/method/environment. Write: adapter/scratch only.")
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts \
                or ".git" in Path(relative).parts:
            raise ValueError("Use a relative path without parent traversal or .git")
        base = self.areas[area]
        result = (base / relative).resolve()
        if not result.is_relative_to(base):
            raise ValueError("Path escapes the granted area")
        if write and result == base:
            raise ValueError("An area root cannot be replaced or removed")
        return result

    def list_files(self, area):
        base = self.path(area, "")
        return {"files": sorted(str(p.relative_to(base)) for p in base.rglob("*") if p.is_file()
                                and not p.is_symlink() and p.resolve().is_relative_to(base))}

    def read_file(self, area, path, offset=0):
        file = self.path(area, path)
        if offset < 0:
            raise ValueError("offset must be nonnegative")
        content = file.read_text()
        end = min(offset + 30000, len(content))
        return {"area": area, "path": path, "text": content[offset:end],
                "next_offset": end if end < len(content) else None}

    def write_file(self, area, path, content):
        file = self.path(area, path, write=True)
        if len(content.encode()) > 1000000:
            raise ValueError("One write is limited to 1 MB")
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        return {"written": path, "area": area, "bytes": len(content.encode())}

    def delete_path(self, area, path):
        file = self.path(area, path, write=True)
        if file.is_dir():
            if any(file.iterdir()):
                raise ValueError("Delete files explicitly before removing a nonempty directory")
            file.rmdir()
        else:
            file.unlink()
        return {"deleted": path, "area": area}

    def move_path(self, area, source, destination):
        src, dst = self.path(area, source, write=True), self.path(area, destination, write=True)
        if dst.exists():
            raise ValueError("Destination already exists")
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        return {"moved": source, "to": destination, "area": area}

    def validate(self):
        result = self.check_spec()
        findings = [f"{i.get('path') or '(adapter)'}: {i.get('problem')} (check {i.get('check')})"
                    for i in result.get("issues", [])]
        return {"passed": bool(result.get("passed")), "findings": findings, "scope": VALIDATE_SCOPE}

    def read_image(self, area, path):
        file = self.path(area, path)
        if file.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            raise ValueError("read_image accepts PNG or JPEG files only")
        with file.open("rb") as stream:
            data = stream.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("Image exceeds the 4 MiB limit")
        if data.startswith(PNG):
            mime = "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        else:
            raise ValueError("File does not have a PNG or JPEG signature")
        return {"area": area, "path": path, "mime_type": mime, "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data), "data": base64.b64encode(data).decode("ascii")}

    def operation(self, name, arguments):
        """One file operation as the old sandboxed helper ran it, including its cap on the reply size."""
        try:
            result = getattr(self, name)(**arguments)
        except Exception as exc:                   # noqa: BLE001 — reported to the model as the old error text
            raise ToolFailure(f"{type(exc).__name__}: {exc}") from None
        limit = IMAGE_REPLY_LIMIT if name == "read_image" else self.reply_limit
        if limit and len(json.dumps({"result": result})) + 1 > limit:
            raise ToolFailure("Sandboxed file result exceeded its output limit")
        return result


# ---------- evidence (old assemble_evidence, for one case and all earlier runs of this Trainer run) ----------

def _no_private_fields(value):
    if isinstance(value, dict):
        if any(k in value for k in PRIVATE_FIELDS):
            raise ValueError("Private transport fields must not enter learning evidence")
        for child in value.values():
            _no_private_fields(child)
    elif isinstance(value, list):
        for child in value:
            _no_private_fields(child)


def scrub(value, where=""):
    """The value without score or judge fields, and the dotted paths that were removed."""
    removed = []
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in SCORE_FIELDS:
                removed.append(f"{where}{k}")
                continue
            out[k], sub = scrub(v, f"{where}{k}.")
            removed += sub
        return out, removed
    if isinstance(value, list):
        out = []
        for i, v in enumerate(value):
            item, sub = scrub(v, f"{where}{i}.")
            out.append(item)
            removed += sub
        return out, removed
    return value, removed


def adapter_sessions(material: Path, upto: int) -> dict[int, int]:
    """For each step 1..upto, the review session whose adapter the step's rollouts used (0 = the root W0): one less
    than the first step whose rollouts used the same adapter commit (step_k/summary.json `adapter_commit`). A session
    that failed or changed nothing makes no commit, so the next step reruns the earlier adapter. Without a recorded
    commit, step k counts as using session k-1's adapter."""
    first, out = {}, {}
    for step in range(1, upto + 1):
        try:
            commit = json.loads((material / f"step_{step:04d}" / "summary.json").read_text()).get("adapter_commit")
        except (OSError, ValueError):
            commit = None
        if commit:
            first.setdefault(commit, step)
            out[step] = first[commit] - 1
        else:
            out[step] = step - 1
    return out


def run_label(arm: str | None, session: int) -> str:
    """B for a harness episode (no adapter, or the untouched root), Cn for an adapted episode that used the adapter of
    review session n. Without a recorded arm, the adapter decides."""
    if arm == "harness" or arm is None and session == 0:
        return "B"
    return f"C{session}"


def case_of(record: dict) -> str:
    """The case a material record belongs to: the task's `case` (alias rows share it), else its id."""
    row = record.get("input") or {}
    return row.get("case") or row.get("id") or record.get("id")


def case_spec(row: dict, cases_file: Path | None) -> tuple[dict, str | None]:
    """task.json as the old evidence wrote it: the common contract and the full case from cases.json."""
    case_id = row.get("case") or row.get("id")
    if cases_file is not None and cases_file.is_file():
        cases = json.loads(cases_file.read_text())
        for case in cases.get("cases", []):
            if case.get("id") == case_id:
                return {"common_contract": cases.get("common_contract"), "case": case}, sha(cases_file)
    case = {"id": case_id, **{k: row[k] for k in ("title", "prompt", "rubric") if k in row}}
    return {"common_contract": row.get("common_contract"), "case": case}, None


def _inside(root: Path, relative: str) -> Path | None:
    path = (root / relative).resolve()
    return path if path.is_relative_to(root.resolve()) and path.is_file() and not (root / relative).is_symlink() \
        else None


def _screens(summary_path: Path, traj: Path) -> list[tuple[Path, dict]]:
    """The model-facing screenshots a capture summary lists (diagnostic images are not among them)."""
    try:
        summary = json.loads(summary_path.read_text())
    except (OSError, ValueError):
        return []
    found = []
    for shot in summary.get("screenshots") or []:
        if not isinstance(shot, dict) or not isinstance(shot.get("file"), str):
            continue
        path = _inside(summary_path.parent, shot["file"])
        if path is None or not path.resolve().is_relative_to(traj.resolve()):
            continue
        data = path.read_bytes()
        if len(data) > MAX_IMAGE_BYTES or not data.startswith(PNG):
            continue
        if shot.get("sha256") and hashlib.sha256(data).hexdigest() != shot["sha256"]:
            continue
        found.append((path, shot))
    return found


def assemble_evidence(material: Path, upto: int, case_id: str, cases_file: Path | None, out: Path) -> dict:
    """Copy the listed files of every run of `case_id` in learning-material steps 1..upto into `out`."""
    material = Path(material)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    secret = os.environ.get(DEFAULTS["api_key_env"]) or ""
    episodes, images, copied, scrubbed = [], {}, [], []
    cases_sha = None
    sessions = adapter_sessions(material, upto)
    for step in range(1, upto + 1):
        d = material / f"step_{step:04d}"
        records = [r for r in (json.loads(f.read_text()) for f in sorted((d / "tasks").glob("*.json")))
                   if case_of(r) == case_id]
        for record in records:
            traj = (d / record["trajectory"]) if record.get("trajectory") else None
            if traj is not None and not traj.is_dir():
                traj = None
            recorded = _read_json(traj / "result.json") if traj is not None else {}
            label = run_label(recorded.get("arm") or (record.get("output") or {}).get("arm"), sessions[step])
            taken = [e["id"] for e in episodes]
            if label in taken:                  # a further episode of the same condition and adapter
                label += f"-{sum(1 for t in taken if t == label or t.startswith(label + '-')) + 1}"
            spec, cases_sha = case_spec(record.get("input") or {}, cases_file)
            save(out / label / "task.json", spec)
            episode = {"id": label, "learning_step": step, "task_id": record.get("id"), "arm": None,
                       "actor_status": None, "submission_sha256": None, "images": [], "check_images": [],
                       "spec": f"{label}/task.json", "notes": []}
            if traj is None:
                episode["notes"].append(f"no run record was kept (the run ended with {record.get('status')})")
                episodes.append(episode)
                continue
            files = ["submission/index.html", "result.json", "capture/summary.json"]
            files += [p.relative_to(traj).as_posix() for directory in ("calls", "tools")
                      for p in sorted((traj / directory).glob("*.json"))]
            shots = [(p, "images") for p, _ in _screens(traj / "capture" / "summary.json", traj)]
            for summary in sorted(traj.glob("checks/**/summary.json")):
                files.append(summary.relative_to(traj).as_posix())
                shots += [(p, "check_images") for p, _ in _screens(summary, traj)]
            for path, kind in shots:
                relative = path.relative_to(traj.resolve()).as_posix()
                files.append(relative)
                key = f"{label}/{relative}"
                images[key] = {"case": case_id, "run": label, "sha256": sha(path), "bytes": path.stat().st_size}
                episode[kind].append(key)
            for relative in dict.fromkeys(files):
                source = _inside(traj, relative)
                if source is None:
                    continue
                target = out / label / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.suffix == ".json":
                    try:
                        data = json.loads(source.read_text())
                    except ValueError:
                        continue
                    _no_private_fields(data)
                    clean, removed = scrub(data)
                    if removed:
                        save(target, clean)
                        scrubbed.append({"path": f"{label}/{relative}", "removed": removed})
                    else:
                        shutil.copy2(source, target)
                else:
                    shutil.copy2(source, target)
                if secret and source.suffix in (".json", ".html") and secret in target.read_text(errors="replace"):
                    raise ValueError("A credential value must not enter learning evidence")
                copied.append({"path": f"{label}/{relative}",
                               "source": source.relative_to(material.resolve()).as_posix()
                               if source.is_relative_to(material.resolve()) else str(source),
                               "sha256": sha(target)})
            result = json.loads((out / label / "result.json").read_text()) if (out / label / "result.json").is_file() \
                else {}
            submission = out / label / "submission/index.html"
            episode.update(arm=result.get("arm"), actor_status=result.get("status"),
                           submission_sha256=sha(submission) if submission.is_file() else None)
            episode["notes"] += record_notes(episode, result, _read_json(traj / "capture" / "summary.json"),
                                             any(f.startswith("calls/") for f in files))
            episodes.append(episode)
    labels = [e["id"] for e in episodes]
    manifest = {"created_utc": utc(), "case": case_id, "runs": labels, "episodes": episodes,
                "original_images": images, "copied_files": copied, "cases_file_sha256": cases_sha,
                "excluded": ["A/direct outputs", "other cases", "scores, judge records and rubric verdicts",
                             "subjective external reviews"]}
    save(out / "manifest.json", manifest)
    (out / "README.md").write_text(evidence_readme(case_id, labels, {e["id"]: e["notes"] for e in episodes}))
    return {"manifest": manifest, "scrubbed": scrubbed}


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def record_notes(episode: dict, result: dict, summary: dict, has_calls: bool) -> list[str]:
    """What the old evidence gate refused a run for (learning.py assemble_evidence), stated as facts instead: a later
    round must not lose all earlier evidence because one run's capture failed."""
    notes = []
    shots = len(episode["images"])
    if not summary:
        notes.append("no final capture summary was kept; 0 of 5 final screenshots are indexed")
    elif summary.get("capture_ok") is not True or shots != 5:
        notes.append(f"the final capture is {'ok' if summary.get('capture_ok') is True else 'not ok'} (status "
                     f"{summary.get('status')}); {shots} of 5 final screenshots are indexed")
    digest = episode["submission_sha256"]
    if digest is None or result.get("submission_sha256") != digest \
            or (summary.get("input") or {}).get("index_sha256") != digest:
        notes.append("the submission, result.json and the final capture do not name the same source hash")
    if not has_calls:
        notes.append("no model-call record was kept")
    return notes


def evidence_readme(case_id: str, labels: list[str], notes: dict[str, list[str]] | None = None) -> str:
    runs = f"{len(labels)} run{'s' if len(labels) != 1 else ''}: {', '.join(labels) or 'none'}"
    suffix = (" A label with a suffix (-2, -3, ...) is a further episode of the same condition with the same "
              "adapter." if any("-" in label for label in labels) else "")
    listed = [f"- {label}: {'; '.join(items)}." for label, items in (notes or {}).items() if items]
    return (
        f"# This campaign's GLM episodes of the {case_id} task ({runs})\n\n"
        "manifest.json indexes original submissions, captured PNGs, browser observations, "
        "and public GLM call/tool records. Run B is the harness episode without an adapter; run Cn is an "
        f"adapted episode that used the adapter of review session n.{suffix} Each <run>/task.json contains its "
        "original brief. Read at least one original PNG from EACH run using read_image; text descriptions "
        "alone do not meet this requirement. Read the source and relevant checks too. "
        "Five final screenshots are indexed for each episode; the screenshots the actor saw in its own "
        "inspections are indexed under <run>/checks/.\n\n"
        + ("Record notes (facts about the copied records, not judgements; a run without indexed screenshots has "
           "none to read):\n" + "\n".join(listed) + "\n\n" if listed else "") +
        "No human, Codex or model visual critique and no score is supplied. Browser state fields are application "
        "self-reports, not proof of physical accuracy or visual quality. Frame cadence includes "
        "capture overhead. A screenshot demonstrates its captured moment, not all motion. "
        "If diagnosing a visual issue yourself, label it as your interpretation and name its "
        "actual frame. No baseline-versus-adapted winner or improvement is supplied.\n\n"
        "This is task-family learning from familiar examples of one task, not unseen transfer, "
        "weight training, or a benchmark score. Extract compact durable principles and "
        "evidence-linked examples, not complete output HTML or a turnkey replacement solution.\n")


# ---------- MWM session preparation (old evolvers/mwm: prepare_session, build_prompt, maintenance) ----------

KEEP_SESSIONS = 5
DIGEST_HEAD = "## Older sessions (digests)"
_SESSION = re.compile(r"^## Session (?:block\s*)?0*(\d+)\b", re.I)
_OPENISH = re.compile(r"\b(open|unresolved|pending|awaiting|candidate)\b", re.I)
CASE_CAP, MAX_CASES = 3000, 12
_CASE_NAME = re.compile(r"^(\d{2})-[A-Za-z0-9._-]+\.md$")
_CASE_MARK = "\n[wrapper-truncated at %d chars — rewrite this case tighter; git holds the full text]" % CASE_CAP


def _digest(n: int, body: str) -> str:
    for line in body.splitlines():
        s = line.strip()
        if s.lower().startswith("digest:"):
            return f"- Session {n}: {s[7:].strip()}"
    first = next((l.lstrip("-# ").strip() for l in body.splitlines() if l.strip()), "")
    return f"- Session {n}: {first[:160]}"


def roll_journal(adapter: Path, keep: int = KEEP_SESSIONS) -> tuple[list, str]:
    """Collapse all but the newest `keep` session entries of meta/journal.md to digest lines (old journal.py)."""
    p = adapter / "meta" / "journal.md"
    try:
        text = p.read_text()
    except OSError:
        return [], ""
    parts = re.split(r"(?m)^(## .+)\n?", text)
    pre = parts[0]
    segs = [(parts[i], parts[i + 1] if i + 1 < len(parts) else "") for i in range(1, len(parts), 2)]
    sess, others, old_digests = [], [], ""
    for h, b in segs:
        m = _SESSION.match(h)
        if m:
            sess.append((int(m.group(1)), h, b))
        elif h.strip() == DIGEST_HEAD:
            old_digests += b
        else:
            others.append((h, b))
    numbers = sorted({n for n, _h, _b in sess})
    if len(numbers) <= keep:
        return [], ""
    cutoff = numbers[-keep]
    old = [(n, h, b) for n, h, b in sess if n < cutoff]
    recent = [(n, h, b) for n, h, b in sess if n >= cutoff]
    if not old:
        return [], ""
    with open(adapter / "meta" / "journal-archive.md", "a") as f:
        for n, h, b in sorted(old):
            f.write(h + "\n" + b.rstrip("\n") + "\n\n")
    out = pre
    for h, b in others:
        out += h + "\n" + b
    digests = (old_digests.rstrip("\n") + "\n" if old_digests.strip() else "")
    digests += "".join(_digest(n, b) + "\n" for n, _h, b in sorted(old))
    out += DIGEST_HEAD + "\n" + digests + "\n"
    for n, h, b in sorted(recent):
        out += h + "\n" + b
    p.write_text(out if out.endswith("\n") else out + "\n")
    flagged = sorted({n for n, _h, b in old if any(_OPENISH.search(l) for l in b.splitlines()
                                                   if "RESOLVED" not in l and "resolved" not in l)})
    notice = ""
    if flagged:
        notice = (f"Roll notice: archived session entr{'y' if len(flagged) == 1 else 'ies'} "
                  f"{', '.join(str(n) for n in flagged)} still mention{'s' if len(flagged) == 1 else ''} "
                  "open/unresolved work — if any of it is a live question, it must have its own file under "
                  "meta/cases/ (archived text is no longer required reading).")
    return ["meta/journal.md", "meta/journal-archive.md"], notice


def cap_cases(adapter: Path) -> list[str]:
    """Truncate oversized docket cases in place before the session (old docket.enforce_case_caps)."""
    cdir = adapter / "meta" / "cases"
    if not cdir.is_dir():
        return []
    cut = []
    for p in sorted(cdir.iterdir()):
        if p.name == "README.md" or not _CASE_NAME.match(p.name) or not p.is_file():
            continue
        text = p.read_text(errors="replace")
        if len(text) > CASE_CAP:
            p.write_text(text[:CASE_CAP - len(_CASE_MARK)] + _CASE_MARK)
            cut.append(f"meta/cases/{p.name}")
    return cut


def docket_status(adapter: Path) -> str:
    """The docket census of every session prompt (old docket.render_status)."""
    cdir = adapter / "meta" / "cases"
    if not cdir.is_dir():
        lines = ["(no cases directory)"]
    else:
        lines, matched = [], []
        for name in sorted(os.listdir(cdir)):
            if name == "README.md":
                continue
            if not _CASE_NAME.match(name):
                lines.append(f"IGNORED meta/cases/{name} (name must be NN-<slug>.md)")
                continue
            matched.append(name)
        for name in matched[:MAX_CASES]:
            try:
                text = (cdir / name).read_text(errors="replace")
            except OSError:
                continue
            n = len(text.strip())
            flag = ""
            if text.rstrip().endswith(_CASE_MARK.strip()):
                flag = " TRUNCATED by the wrapper before this session — rewrite it tighter (git holds the full text)"
            elif n >= 0.8 * CASE_CAP:
                flag = " NEAR CAP"
            lines.append(f"meta/cases/{name}: {n}/{CASE_CAP} chars{flag}")
        for name in matched[MAX_CASES:]:
            lines.append(f"IGNORED meta/cases/{name} (over the {MAX_CASES}-case docket — close or merge one first)")
    return ("Open cases (meta/cases/ — your tracking surface; delete a case when it settles):\n"
            + "\n".join(lines or ["(no open cases)"]))


_PATCHED = re.compile(r"^(?:\+\+\+ b/|rename to )(.+)$", re.M)


def edit_ranks(history: Path) -> dict[str, int]:
    """For each file, the position of its newest commit in the Trainer's history view of this step's adapter (0 =
    the newest; log.jsonl lists commits newest first, diffs/<commit>.patch holds their changes). The old index ordered
    entries by `git log -1` of each file; an exported adapter has no history of its own, and every file of it has the
    export's modification time. Empty without a history view."""
    ranks: dict[str, int] = {}
    try:
        lines = (Path(history) / "log.jsonl").read_text().splitlines()
    except OSError:
        return ranks
    for rank, line in enumerate(l for l in lines if l.strip()):
        commit = json.loads(line).get("commit") or ""
        try:
            patch = (Path(history) / "diffs" / f"{commit[:12]}.patch").read_text(errors="replace")
        except OSError:
            continue
        for name in _PATCHED.findall(patch):
            ranks.setdefault(name, rank)
    return ranks


def memory_index(adapter: Path, tick: int, ranks: dict[str, int] | None = None) -> str:
    """The [Memory index] of the old build_prompt; newest edit first: by each file's last commit (`ranks`, see
    edit_ranks), else by modification time; name order where equal."""
    files = sorted(p for p in (adapter / "memory").glob("*.md") if p.name != "README.md" and not p.name.startswith("_"))
    if ranks:
        files = sorted(files, key=lambda p: ranks.get(f"memory/{p.name}", len(ranks) + 1))
    else:
        files = sorted(files, key=lambda p: p.stat().st_mtime_ns, reverse=True)
    entries = []
    for p in files[:100]:
        fields = dict(re.findall(r"^(brief|last_confirmed_t):\s*(.*)$", p.read_text(), re.M))
        stamp = fields.get("last_confirmed_t", "?")
        age = ""
        if stamp.isdigit() and tick and tick - int(stamp) > 0.4 * tick:
            age = f" (unverified for {tick - int(stamp)} ticks)"
        entries.append(f"[{p.stem}] t={stamp}{age} · {fields.get('brief', '(no brief)')[:300]}")
    return "\n".join(entries) or "(empty)"


def start_text(adapter: Path, sessions: dict[int, int]) -> str:
    """What this session starts from: W0 while the adapter has no `origin` (the Trainer adds one with the first
    committed change), else the sessions whose changes it holds (from adapter_sessions)."""
    try:
        learned = "origin" in (yaml.safe_load((adapter / "adapter.yaml").read_text()) or {})
    except (OSError, yaml.YAMLError):
        learned = False
    written = sorted({s for s in sessions.values() if s > 0})
    if not learned or not written:
        return "generic MWM W0"
    if written == list(range(written[0], written[-1] + 1)):
        span = f"session {written[0]}" if len(written) == 1 else f"sessions {written[0]}-{written[-1]}"
    else:
        span = "sessions " + ", ".join(map(str, written))
    return f"the adapter written in {span} from generic MWM W0"


def fill(template: str, values: dict) -> str:
    """Replace {name} placeholders in one pass; other braces stay as they are."""
    return re.sub(r"\{(\w+)\}", lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), template)


def repair_notice(material_step: Path) -> str:
    path = material_step / "check_feedback.json"
    if not path.is_file():
        return ""
    items = json.loads(path.read_text())
    listed = "; ".join(f"{i.get('path')} ({i.get('reason')}; restored from {i.get('restored_from')})" for i in items)
    return ("Repair notice: after your last session the framework restored these files because the adapter failed "
            f"its template specification: {listed}.")


# ---------- images (old learning.py: _tool_reply checks, retain_latest_images) ----------

def retain_latest_images(messages, pictures, records, cap=MAX_IMAGE_CHARS):
    """Remove older image payloads, retain textual context and admit a bounded group."""
    for message in messages:
        if message.get("role") == "user" and isinstance(message.get("content"), list):
            message["content"] = [part for part in message["content"] if part.get("type") != "image_url"]
    admitted, kept, total = [], [], 0
    for picture, record in zip(pictures, records, strict=True):
        record = dict(record)
        size = record["base64_characters"]
        if total + size > cap:
            record["omitted"] = "Latest image group exceeds the declared payload cap; read separately."
        else:
            total += size
            record["included"] = True
            admitted += [{"type": "text", "text": f"Original {record.get('run', 'B')} screenshot: " + record["path"]},
                         picture]
            kept.append(record)
    if admitted:
        messages.append({"role": "user", "content": admitted})
    return kept


def redact(text: str, key_env: str | None) -> str:
    key = os.environ.get(key_env) if key_env else None
    return text.replace(key, "[REDACTED]") if key else text


def continuing_message(message):
    """The assistant message as the next request carries it: reasoning stays in memory only."""
    return {k: v for k, v in message.items() if k in ("role", "content", "reasoning_content", "tool_calls")}


def public_message(message):
    return {k: v for k, v in message.items() if k in ("role", "content", "tool_calls")}


# ---------- one learner session (old learning.py: _conversation) ----------

class Session:
    def __init__(self, *, client, areas: Areas, image_manifest: dict, prompt: str, deadline: float, caps: dict,
                 cancel, out: Path, policy: dict, settings: dict, key_env: str | None = None):
        self.client, self.areas, self.images, self.prompt = client, areas, image_manifest, prompt
        self.deadline, self.caps, self.cancel, self.out, self.policy = deadline, caps, cancel, out, policy
        self.settings = settings        # sampling settings sent with every request
        self.key_env = key_env          # its value is redacted from recorded request errors
        self.function_tools = json.loads((HERE / "tools.json").read_text())
        self.schemas = {t["function"]["name"]: t["function"]["parameters"] for t in self.function_tools}
        assert set(self.schemas) == set(ALLOWED_TOOLS)
        self.usages: list[Usage] = []
        self.failed_requests = 0
        self.calls: list[dict] = []
        self.tool_records: list[dict] = []

    def _stopped(self) -> bool:
        return self.cancel is not None and self.cancel.is_set()

    def run(self) -> dict:
        messages = [{"role": "user", "content": self.prompt}]
        state = {"status": "not_started", "model_calls": 0, "answered_requests": 0, "requested_tools": 0,
                 "executed_tools": 0,
                 "successful_tools": 0, "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0},
                 "image_reads": [], "images_delivered": [], "final": "",
                 "image_delivery_semantics": "First successful API request after each new read_image batch; "
                                             "retained images may also be resent in later requests."}
        pending = []
        caps = self.caps
        for number in range(1, caps["max_requests"] + 1):
            if self._stopped():
                state["status"] = "cancelled"
                break
            remaining = self.deadline - time.monotonic()
            tokens = caps["max_completion"] - state["usage"]["completion_tokens"]
            if remaining <= 0 or tokens <= 0 or state["requested_tools"] >= caps["max_tools"]:
                state["status"] = "budget_exhausted"
                break
            state["model_calls"] = number
            request_max = min(caps["max_response"], tokens)
            record = {"request": number, "request_max_tokens": request_max, "image_inputs": pending}
            self.client.timeout = min(REQUEST_TIMEOUT, remaining)
            started = time.monotonic()
            try:
                message, usage = self.client.chat(messages, tools=self.function_tools, **self.settings,
                                                  max_tokens=request_max)
            except Exception as exc:            # noqa: BLE001 — the old transport recorded the error and stopped
                self.failed_requests += 1
                record.update(seconds=round(time.monotonic() - started, 3),
                              error=redact(f"{type(exc).__name__}: {exc}", self.key_env)[:3000])
                self._save_call(number, record)
                state["status"], state["error"] = "api_error", record["error"]
                break
            self.usages.append(usage)
            record.update(seconds=round(time.monotonic() - started, 3), usage=usage.__dict__)
            state["images_delivered"] += [{**r, "request": number} for r in pending]
            pending = []
            state["usage"]["prompt_tokens"] += usage.input_tokens
            state["usage"]["completion_tokens"] += usage.output_tokens
            state["usage"]["cached_tokens"] += usage.cached_input_tokens
            if set(message) <= {"finish_reason"}:      # the provider returned no choice
                self._save_call(number, record)
                state["status"] = "invalid_response"
                break
            state["answered_requests"] += 1
            record.update(message=public_message(message), finish_reason=message.get("finish_reason"))
            self._save_call(number, record)
            messages.append(continuing_message(message))
            calls = message.get("tool_calls") or []
            if not calls:
                state["status"] = "completed" if message.get("finish_reason") == "stop" else "response_truncated"
                state["final"] = message.get("content") or ""
                break
            pictures, image_records = [], []
            for call in calls:
                state["requested_tools"] += 1
                index = state["requested_tools"]
                function = call.get("function") or {}
                name, args = function.get("name"), None
                tool_record = {"index": index, "request": number, "name": name, "tool_call_id": call.get("id"),
                               "started_utc": utc()}
                new_records = []
                try:
                    if self._stopped():
                        raise ValueError("The Trainer stopped this learner session (budget or timeout)")
                    if index > caps["max_tools"] or time.monotonic() >= self.deadline:
                        raise ValueError("Learner tool or time budget exhausted")
                    if name not in self.schemas:
                        raise ValueError("Tool is not in the prose-only capability allowlist")
                    args = json.loads(function.get("arguments") or "{}")
                    Draft202012Validator(self.schemas[name]).validate(args)
                    if set(args) - set(self.schemas[name].get("properties", {})):
                        raise ValueError("Unknown tool argument")
                    check_mutation(self.areas.areas, name, args)
                    if name == "read_image" and (args.get("area") != "evidence" or args.get("path") not in self.images):
                        raise ValueError("Read an indexed original run screenshot from evidence")
                    state["executed_tools"] += 1
                    try:
                        text, new_pictures, new_records = self._execute(name, args)
                        ok = True
                    except ToolFailure as failure:
                        text, new_pictures, new_records, ok = f"Error executing tool {name}: {failure}", [], [], False
                    if ok:
                        state["successful_tools"] += 1
                        pictures += new_pictures
                        image_records += new_records
                        state["image_reads"] += [{**r, "tool_index": index} for r in new_records]
                    tool_record.update(ok=ok, response=text, images=new_records)
                except Exception as exc:          # noqa: BLE001 — refused before execution: the old JSON error
                    text = json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                    tool_record.update(ok=False, response=text)
                tool_record["arguments"] = ({k: v for k, v in args.items() if k != "content"}
                                            if isinstance(args, dict) else args)
                if isinstance(args, dict) and isinstance(args.get("content"), str):
                    tool_record["written_content_sha256"] = hashlib.sha256(args["content"].encode()).hexdigest()
                self.tool_records.append(tool_record)
                save(self.out / "tool-results" / f"{index:03}.json", tool_record)
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": text})
            if pictures:
                pending = retain_latest_images(messages, pictures, image_records)
        else:
            state["status"] = "budget_exhausted"
        return state

    def _execute(self, name, args):
        if name == "get_policy":
            return json.dumps(self.policy, ensure_ascii=False, indent=2), [], []
        if name != "read_image":
            return json.dumps(self.areas.operation(name, args), ensure_ascii=False, indent=2), [], []
        result = self.areas.operation("read_image", args)
        metadata = {k: v for k, v in result.items() if k != "data"}
        expected = self.images.get(args.get("path")) if args.get("area") == "evidence" else None
        data = base64.b64decode(result["data"], validate=True)
        if not expected or hashlib.sha256(data).hexdigest() != expected["sha256"] or len(data) != expected["bytes"]:
            raise ValueError("Image must match an indexed, unmodified run capture")
        if result["mime_type"] != "image/png":
            raise ValueError("Expected original PNG content")
        picture = {"type": "image_url", "image_url": {"url": f"data:{result['mime_type']};base64,{result['data']}"}}
        record = {"path": args["path"], **expected, "base64_characters": len(result["data"])}
        return json.dumps(metadata), [picture], [record]

    def _save_call(self, number, record):
        self.calls.append(record)
        save(self.out / "calls" / f"{number:02}.json", record)

    def usage(self, model: str) -> list[Usage]:
        out = []
        if self.usages:
            costs = [u.cost_usd for u in self.usages]
            out.append(Usage(model, sum(u.input_tokens for u in self.usages),
                             sum(u.cached_input_tokens for u in self.usages),
                             sum(u.output_tokens for u in self.usages), len(self.usages),
                             None if any(c is None for c in costs) else sum(costs)))
        if self.failed_requests:            # a failed request may still have been billed: its cost is unknown
            out.append(Usage(model, calls=self.failed_requests, cost_usd=None))
        return out


# ---------- the Evolver ----------

def _files(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): sha(p) for p in sorted(root.rglob("*")) if p.is_file()}


def _diff(before: Path, after: Path, changed: list[str]) -> str:
    lines = []
    for name in changed:
        old = (before / name).read_text(errors="replace").splitlines(keepends=True) if (before / name).is_file() else []
        new = (after / name).read_text(errors="replace").splitlines(keepends=True) if (after / name).is_file() else []
        lines += difflib.unified_diff(old, new, fromfile="before/" + name, tofile="after/" + name)
    return "".join(lines)


def _clip(text, n):
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    return text if len(text) <= n else text[:n] + " …"


def transcript(calls: list[dict], tools: list[dict]) -> str:
    """A readable summary of the session: visible messages, tool calls and the start of each tool result."""
    by_request: dict[int, list[dict]] = {}
    for t in tools:
        by_request.setdefault(t["request"], []).append(t)
    out = ["# Learner transcript (visible content only; reasoning is not kept)\n"]
    for c in calls:
        u = c.get("usage") or {}
        out.append(f"## Request {c['request']} — finish {c.get('finish_reason')}, "
                   f"completion {u.get('output_tokens', '?')} tokens, {c.get('seconds', '?')} s")
        if c.get("error"):
            out.append(f"Request failed: {c['error']}")
        content = ((c.get("message") or {}).get("content") or "").strip()
        if content:
            out.append(content)
        for t in by_request.get(c["request"], []):
            args = json.dumps(t.get("arguments"), ensure_ascii=False)
            written = " (content sha256 " + t["written_content_sha256"][:12] + ")" if t.get("written_content_sha256") \
                else ""
            out.append(f"- `{t['name']}` {args}{written} → {'ok' if t.get('ok') else 'error'}: "
                       f"{_clip(t.get('response', ''), 400)}")
        out.append("")
    return "\n".join(out) + "\n"


class MwmEvolver(Evolver):
    client = None       # a ChatClient-like object (chat(messages, **kwargs) -> (message, Usage)); built on first use

    def _client(self, p: dict):
        if self.client is None:
            self.client = ChatClient(p["base_url"], p["model"], api_key_env=p.get("api_key_env"),
                                     price=p.get("price"), timeout=REQUEST_TIMEOUT, max_retries=0)
        return self.client

    def step(self, ctx: StepContext) -> StepResult:
        p = {**DEFAULTS, **(ctx.params or {})}
        n = ctx.progress.step if ctx.progress else 1
        ws, out = ctx.workspace, ctx.workspace.output
        out.mkdir(parents=True, exist_ok=True)
        if p.get("rounds") is not None and n > int(p["rounds"]):
            (out / "report.md").write_text(f"Step {n}: measurement round (learning sessions: {p['rounds']}). "
                                           "No learner call, no edits.\n")
            return StepResult("done", "measurement round")
        directory = Path(getattr(self, "directory", HERE))
        deadline = time.monotonic() + float(p["max_seconds"])
        cases_file = (directory / p["cases_file"]).resolve() if p.get("cases_file") else None
        material = Path(ws.learning_material).resolve()
        records = sorted((material / f"step_{n:04d}" / "tasks").glob("*.json"))
        if not records:
            return StepResult("failed", f"no learning material for step {n}")
        case_id = case_of(json.loads(records[0].read_text()))
        lost = [r for r in (json.loads(f.read_text()) for f in records)
                if case_of(r) == case_id and not r.get("trajectory")]
        if lost:        # round k learns from the run of round k-1; without it there is nothing new to learn from
            why = "; ".join(f"{r.get('id')}: {r.get('status')} ({str(r.get('error'))[:300]})" for r in lost)
            (out / "report.md").write_text(f"Step {n}: no learner session: this step's run of {case_id} left no "
                                           f"record ({why}). The adapter is unchanged; the next step runs it again.\n")
            return StepResult("failed", f"session {n} ({case_id}): not run, this step's run left no record")

        # Areas: evidence, environment and history live in the step's scratch (wiped by the Trainer every step).
        scratch_root = Path(ws.scratch).resolve()
        roots = {"adapter": Path(ws.adapter).resolve(), "evidence": scratch_root / "evidence",
                 "scratch": scratch_root / "learner", "method": directory / "method",
                 "environment": scratch_root / "environment", "history": scratch_root / "history"}
        for name in ("evidence", "scratch", "environment", "history"):
            if roots[name].exists():
                shutil.rmtree(roots[name])
        try:
            built = assemble_evidence(material, n, case_id, cases_file, roots["evidence"])
        except ValueError as exc:
            (out / "report.md").write_text(f"Step {n}: evidence refused, no learner call: {exc}\n")
            return StepResult("failed", f"evidence refused: {exc}")
        manifest = built["manifest"]
        save(out / "evidence-manifest.json", manifest)
        if built["scrubbed"]:
            save(out / "evidence-scrubbed.json", built["scrubbed"])
        shutil.copytree(roots["adapter"], roots["history"], symlinks=True)     # the pre-maintenance snapshot
        roots["scratch"].mkdir(parents=True)
        roots["environment"].mkdir(parents=True)
        (roots["environment"] / "README.md").write_text(
            fill((directory / "environment.md").read_text(), {"working_directory": roots["adapter"]}))

        # Session preparation as the old MWM method did it, then the prompt.
        rolled, roll = roll_journal(roots["adapter"])
        capped = cap_cases(roots["adapter"])
        notice = "\n".join(x for x in (roll, repair_notice(material / f"step_{n:04d}")) if x)
        brief = roots["adapter"] / "consumption" / "brief.md"
        labels = manifest["runs"]
        start = start_text(roots["adapter"], adapter_sessions(material, n))
        values = {"session": n, "case": case_id, "runs": ", ".join(labels), "start": start, "max_brief": MAX_BRIEF,
                  **{k: p[k] for k in ("max_seconds", "max_requests", "max_tools", "max_completion", "max_response")},
                  "reasoning_note": "" if p.get("reasoning_effort") is None else
                  f"; reasoning_effort={p['reasoning_effort']} is requested for each response",
                  "memory_index": memory_index(roots["adapter"], n, edit_ranks(ws.history)),
                  "docket": docket_status(roots["adapter"]),
                  "brief_chars": len(brief.read_text()) if brief.exists() else 0, "notice": notice,
                  "working_directory": roots["adapter"]}
        prompt = fill((directory / "prompt.md").read_text(), values)
        (out / "prompt.md").write_text(prompt)

        policy = {"requested": {"backend": "in-process", "read_access": {"paths": []}, "network": False},
                  "adapter": str(roots["adapter"]), "evidence": str(roots["evidence"]),
                  "scratch": str(roots["scratch"]), "validation": None, "working_directory": str(roots["adapter"]),
                  "write_rule": "Only candidate adapter and isolated scratch are writable; evidence is always "
                                "readable and read-only."}
        reply_limit = p.get("reply_limit_bytes")
        areas = Areas(roots, check_spec=lambda: ctx.tools["check_spec"](),
                      reply_limit=int(reply_limit) if reply_limit else None)
        client = self._client(p)
        session = Session(client=client, areas=areas, image_manifest=manifest["original_images"], prompt=prompt,
                          deadline=deadline, caps={k: int(p[k]) for k in ("max_requests", "max_tools",
                                                                          "max_completion", "max_response")},
                          cancel=ctx.cancel, out=out / "learner", policy=policy,
                          settings={k: p[k] for k in SAMPLING if p.get(k) is not None}, key_env=p.get("api_key_env"))
        state = session.run()
        save(out / "learner" / "result.json", {k: v for k, v in state.items() if k != "final"})
        (out / "learner" / "final.md").write_text(state["final"])
        (out / "transcript.md").write_text(transcript(session.calls, session.tool_records))
        usage = session.usage(getattr(client, "model", p["model"]))
        return self._finish(ctx, n, case_id, state, usage, roots, labels, rolled + capped)

    def _finish(self, ctx, n, case_id, state, usage, roots, labels, maintained) -> StepResult:
        out = ctx.workspace.output
        before, after = _files(roots["history"]), _files(roots["adapter"])
        changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
        (out / "changes.diff").write_text(_diff(roots["history"], roots["adapter"], changed))
        brief = roots["adapter"] / "consumption" / "brief.md"
        brief_chars = len(brief.read_text()) if brief.exists() else 0
        html = [c for c in changed if c.endswith(".md") and (roots["adapter"] / c).is_file()
                and re.search(r"<!doctype\s+html|<html(?:\s|>)", (roots["adapter"] / c).read_text(), re.I)]
        # The old static review gate, reported only: the Trainer's check_spec decides what is kept.
        gate = []
        if state["status"] != "completed":
            gate.append("Learner did not finish normally")
        if not state["successful_tools"]:
            gate.append("No successful tools")
        if not changed:
            gate.append("No knowledge changes")
        if [c for c in changed if not allowed_candidate_path(c)]:
            gate.append("Changes outside approved Markdown knowledge paths")
        if html:
            gate.append("Complete-HTML marker found in knowledge")
        if brief_chars > MAX_BRIEF:
            gate.append(f"Brief exceeds {MAX_BRIEF} characters")
        if not state["images_delivered"]:
            gate.append("No original image was delivered to a successful API request")
        delivered = sorted({r["run"] for r in state["images_delivered"] if r.get("run")})
        u = state["usage"]
        cost = sum(x.cost_usd for x in usage if x.cost_usd is not None)
        unknown = sum(x.calls for x in usage if x.cost_usd is None)
        summary = (f"session {n} ({case_id}): {state['status']}; {len(changed)} file(s) changed; "
                   f"{state['model_calls']} requests, {state['requested_tools']} tools")
        report = [f"# MWM learner — session {n} ({case_id})", "",
                  f"- Status: {state['status']}" + (f" ({state['error']})" if state.get("error") else ""),
                  f"- Evidence runs: {', '.join(labels) or 'none'}; images delivered from runs: "
                  f"{', '.join(delivered) or 'none'}",
                  f"- Requests: {state['model_calls']}; tools requested {state['requested_tools']}, executed "
                  f"{state['executed_tools']}, succeeded {state['successful_tools']}",
                  f"- Tokens: prompt {u['prompt_tokens']} (cached {u['cached_tokens']}), completion "
                  f"{u['completion_tokens']}; cost ${cost:.6f}" + (f" + {unknown} request(s) of unknown cost"
                                                                   if unknown else ""),
                  f"- Changed files: {', '.join(changed) or 'none'}",
                  f"- Session preparation changed: {', '.join(maintained) or 'nothing'}",
                  f"- consumption/brief.md: {brief_chars} characters",
                  f"- Old static review gate: {'passes' if not gate else 'fails: ' + '; '.join(gate)} "
                  "(reported only; the Trainer's check_spec decides what is kept)", "",
                  "## The learner's final report", "", state["final"].strip() or "(none)", ""]
        (out / "report.md").write_text("\n".join(report))
        extra = {"learner_status": state["status"], "changed": changed, "evidence_runs": labels,
                 "images_delivered_runs": delivered, "old_gate_failures": gate, "maintenance": maintained}
        if state["status"] == "cancelled":
            return StepResult("failed", f"session {n}: stopped by the Trainer", usage=usage, extra=extra)
        if not state["answered_requests"]:          # the model never answered: nothing was learned
            return StepResult("failed", summary, usage=usage, extra=extra)
        return StepResult("done", summary, rationale=state["final"][:2000], usage=usage, extra=extra)
