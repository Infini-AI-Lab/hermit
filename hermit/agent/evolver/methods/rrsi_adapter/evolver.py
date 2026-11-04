"""RRSIAdapterEvolver: RRSI (Xia et al., arXiv:2609.24972; github.com/google-research/rrsi @ be50316) on the Trainer,
with the harness confined to the adapter, as Meta-Harness Adapter confines Meta-Harness.

RRSI evolves a harness H in rounds t = 0..T-1. Algorithm 1 (proposal side): the analyst turns the incumbent's own
evaluation into the failure report F_t; the annealed edit budget b_t, the stall flag sigma_t with its reserved
exploration slot, and the prune set B_t are read off the edit history L_t; m proposer variants are drafted from the
same incumbent, each edit tagged with its component and hypothesis; a critic reads each diff before any evaluation
is spent (with bounded repair); a liveness smoke runs the survivors. Algorithm 2 (selection side): every screened
candidate is evaluated on the whole evolve set (k trials per task), and is admissible only above the noise-adjusted
floor S* - delta, within the cost rule (dS > delta: dC <= beta0 + beta1 dS; inside the band: w_s dS - w_c dC + w_n nu
> 0) and the domain's guards; the best admissible one becomes H_{t+1}, else H_t stays. The run's result is H_T.

What is upstream's code, unchanged (the pinned `rrsi` package): the schedule (rrsi.schedule.edit_budget), the history
and its summaries (rrsi.history: L_t, T_t, U_t, sigma_t, E_t, g_t, B_t, the rendered rows), the component tags
(rrsi.components.normalize), the scores (rrsi.evaluate), delta's calibration (rrsi.calibrate), the selection
(rrsi.selection.select_round), the analyst with its digesters (rrsi.analyst.analyze) and the critic (rrsi.critic.
review), the last two with `generate` rebound to one session of the `codex` Base Agent per call (roles.py). What
is Hermit's:

1. The evaluation is the Trainer's: one candidate per step (data.step_size covers the training split, k rows per
   task), the rollouts of a step evaluating the candidate handed back last time (the starting adapter, h0, on the
   first step), credited only when the working copy that ran is the stored candidate (its digest).
2. The proposer is a session of the `codex` (or `claude`) Base Agent, as Meta-Harness Adapter's is, instead of
   upstream's JSON-action agent: it edits a copy of the incumbent adapter in place with its own tools and writes
   done.json, whose contract (budget, tags, reserved slot) is upstream's done() check; an invalid done.json, and
   each critic objection, goes back to the same session (resumed) with upstream's repair text. What it must not
   see is the call's `Limits.hidden`, which the Base Agent enforces or refuses. A session that fails (exits
   non-zero) without a proposal fails the step, and the variant is drafted again at the next one.
3. Candidates are stored as Meta-Harness Adapter stores them (state/candidates/<id>/adapter/ with a digest) and
   checked with check_spec; the smoke is `run_agent` on the domain's smoke tasks.

A run is 1 + (evaluated candidates) steps, at most 1 + T x m; after round T-1 the incumbent is handed back and the
step returns `stop` (trainer.allow_early_stop ends the run); finalize hands back H_T, and says so when the run
ended before round T (`max_failures` failed steps, the Trainer's epochs or budget).

Training State: state/rrsi_adapter.json (the pump: phase, round, queue, candidates with their digests and
evaluations), state/rrsi/ (upstream's files: frontier.json, calibration.json, history.jsonl, attribution.jsonl,
global_analysis.json, jobs/<id>/eval.json, r<t>/ with the analysis, directives.json, <variant>/{proposal,critic,
diff.patch,smoke,prep,eval}.json and decisions.json), state/candidates/<id>/adapter/.
"""
from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from hermit.agent.base import login as codex_login
from hermit.agent.base.interface import Limits, Mount, make
from hermit.agent.evolver.agent import AgentEvolver
from hermit.agent.evolver.base import FinalResult, StepContext, StepResult, Usage
from hermit.agent.evolver.methods.rrsi_adapter.store import _copy_tree, _install, _real, _store, _write
from hermit.agent.evolver.methods.rrsi_adapter.roles import Roles, Stop, bound

STATE = "rrsi_adapter.json"
RRSI = "rrsi"
CANDIDATES = "candidates"
DONE = "done.json"
VARIANTS = "ABCDEFGH"                    # upstream's VARIANT_LABELS
REQUIRED = ("id", "component", "hypothesis", "targets_mode", "predicted_affected", "retroactive_check")
SKIP = {".git", ".hermit"}
REPAIR = ("REPAIR ROUND: your previous edits for this candidate are already in the working tree. The reviewer "
          "raised the objections below. Fix ONLY what the objections require (remove leaked content, split or "
          "re-declare edits, wire up dead code, or delete the offending part) with minimal additional edits, then "
          "write {done} again with the corrected edits array.\n\n=== REVIEWER OBJECTIONS ===\n")


def _require_rrsi():
    try:
        import rrsi  # noqa: F401
    except ImportError as exc:
        from hermit.agent.evolver.dependencies import missing_dependency
        raise missing_dependency("rrsi_adapter", "the upstream RRSI package", "rrsi") from exc


def tree_digest(tree: Path) -> str:
    """The content of an adapter as a candidate is stored: every file's relative path, executable bit and bytes,
    without the root's .git and .hermit/ or byte code (Meta-Harness Adapter's digest over what _copy_tree keeps)."""
    h = hashlib.sha256()
    tree = Path(tree)
    paths = []
    for path in sorted(tree.rglob("*")):
        rel = path.relative_to(tree)
        if rel.parts[0] in SKIP or "__pycache__" in rel.parts or path.suffix == ".pyc":
            continue
        if path.is_file() or path.is_symlink():
            paths.append((rel, path))
    for rel, path in paths:
        h.update(os.fsencode(rel.as_posix()) + b"\0")
        if path.is_symlink():
            h.update(b"link:" + os.fsencode(str(path.readlink())))
        else:
            h.update(b"x:" if path.stat().st_mode & 0o111 else b"-:")
            h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "user.name=rrsi", "-c", "user.email=rrsi@localhost", "-c",
                           "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
                          cwd=str(cwd), capture_output=True, text=True)


CACHES = {".pytest_cache", ".mypy_cache", ".ruff_cache", ".ipynb_checkpoints", "__pycache__"}


def _owned(path: Path, root: Path) -> bool:
    """`path` is absent, or a real directory under `root` reached through real directories only (never a link a
    proposer left on the way)."""
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        return False
    return _real(path, root)


def _unlink_links(tree: Path) -> None:
    """Every symbolic link under `tree` removed (none followed): what the host writes there stays there."""
    if not tree.is_dir() or tree.is_symlink():
        return
    for top, dirs, files in os.walk(tree, followlinks=False):
        for name in dirs + files:
            entry = Path(top) / name
            if entry.is_symlink():
                entry.unlink()


def _remove(path: Path) -> None:
    """`path` gone, whatever the proposer left there: a link or a file unlinked (never followed), a directory
    removed."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def links_in(tree: Path) -> list[str]:
    """Symbolic links under `tree`: a candidate may hold none (its copy runs outside the proposer's sandbox and
    would bring in whatever a link points at), as Meta-Harness Adapter refuses them."""
    return sorted(str(p.relative_to(tree)) for p in Path(tree).rglob("*") if p.is_symlink())


def not_text_in(tree: Path, base: Path) -> list[str]:
    """Files under `tree` the candidate added or changed against `base` that are not UTF-8 text (a NUL byte, bytes or
    a name UTF-8 cannot decode). The precheck and the critic read the diff as text, so content in another encoding
    (UTF-16, a NUL among the text) would pass them unread; upstream's proposer writes text files only."""
    out = []
    for path in sorted(Path(tree).rglob("*")):
        rel = path.relative_to(tree)
        if rel.parts[0] in SKIP or not path.is_file() or path.is_symlink():
            continue
        name = os.fsencode(str(rel))
        data = path.read_bytes()
        old = Path(base) / rel
        try:
            name.decode("utf-8")
            if old.is_file() and not old.is_symlink() and old.read_bytes() == data:
                continue                                     # as it was: not the candidate's doing
            if b"\0" not in data:
                data.decode("utf-8")
                continue
        except UnicodeDecodeError:
            pass
        out.append(name.decode("utf-8", errors="replace"))
    return out


def stage(cand: Path, staged: Path) -> list[str]:
    """The candidate as it is stored, reviewed and committed: a copy without the root's .git and .hermit/, byte code,
    nested repositories, tool caches, the files git would not commit (the candidate's own .gitignore files and
    the user's global excludes, which the Trainer's commit honours) and .gitattributes files (the Trainer's commit
    and export would apply them: line endings converted, a file left out of the rollouts' copy). `cand` must hold no
    symbolic link. Returns what was left out, relative to the candidate."""
    if staged.is_symlink() or staged.is_file():
        staged.unlink()
    elif staged.exists():
        shutil.rmtree(staged)
    _copy_tree(cand, staged)
    dropped = []
    for p in sorted(staged.rglob("*"), key=lambda q: len(q.parts)):
        if p.name == ".git" and p.is_file():                # a worktree's, or a submodule's, pointer
            dropped.append(str(p.relative_to(staged)))
            p.unlink()
        elif p.is_dir() and (p.name == ".git" or p.name in CACHES) and p.exists():
            dropped.append(str(p.relative_to(staged)) + "/")
            shutil.rmtree(p)
    _git(staged, "init", "-q")
    # NUL-separated and as bytes: a name git would quote (non-ASCII, quotes), or one not in UTF-8, is read as it is
    listed = subprocess.run(["git", "ls-files", "-z", "-o", "-i", "--exclude-standard", "--directory"], cwd=str(staged),
                            capture_output=True).stdout
    ignored = [os.fsdecode(name) for name in listed.split(b"\0") if name.strip()]
    for rel in ignored:
        target = staged / rel.rstrip("/")
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        dropped.append(rel)
    shutil.rmtree(staged / ".git", ignore_errors=True)
    for p in sorted(staged.rglob(".gitattributes")):
        if p.is_file():
            p.unlink()
            dropped.append(str(p.relative_to(staged)))
    return dropped


def diff_of(base: Path, new: Path) -> str:
    """The unified diff from one stored tree to another, new files included and paths relative to the adapter
    (upstream's gitops.diff_with_new_files), computed from the two trees themselves, not from anything inside the
    candidate the proposer could arrange."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copytree(base, Path(tmp) / "a")
        shutil.copytree(new, Path(tmp) / "b")
        # No repository around the copies and no configuration but this call's: inside a git checkout (where the
        # examples' runs lie) git would read the candidate's own .gitattributes, and the user's attributes file
        # applies anywhere (a `* -diff` in either shows every file as binary to the precheck and the critic); a user's
        # settings could change the diff's form. Every file as text, whole (no rename pairs), with no external diff
        # or text conversion; bytes UTF-8 cannot decode are replaced (a candidate's own files are UTF-8 text:
        # not_text_in, so these are only the incumbent's).
        env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(Path(tmp).parent), "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": os.devnull}
        out = subprocess.run(["git", "-c", "core.quotepath=off", "-c", f"core.attributesFile={os.devnull}", "diff",
                              "--no-index", "--no-color", "--text", "--no-renames", "--no-ext-diff", "--no-textconv",
                              "--", "a", "b"],
                             cwd=tmp, env=env, capture_output=True).stdout.decode("utf-8", errors="replace")
    return _strip_copy_prefixes(out)


def _strip_copy_prefixes(diff: str) -> str:
    """git's paths `a/<copy>/<file>` and `b/<copy>/<file>` (the copies' directories, `a` and `b`) as `a/<file>` and
    `b/<file>`, on a file's header lines only (`diff --git`, `---`, `+++` before its first hunk): a changed line is
    left as it is. With no renames both paths of a `diff --git` line name the same file, also when it is new (both
    under `b/`) or deleted (both under `a/`), so the line is split in its middle, whatever spaces the name holds."""
    def strip(path: str, side: str) -> str | None:
        """`<side>/<copy>/<name>` as `<side>/<name>`; a name git quotes ("...") keeps its quotes."""
        quoted = path.startswith('"') and path.endswith('"') and len(path) > 1
        inner = path[1:-1] if quoted else path
        if inner[:2] != f"{side}/" or inner[2:4] not in ("a/", "b/"):
            return None
        return f'"{side}/{inner[4:]}"' if quoted else f"{side}/{inner[4:]}"

    out, header = [], False
    for line in diff.splitlines(keepends=True):
        body = line.rstrip("\n")
        if body.startswith("diff --git "):
            header = True
            rest = body[len("diff --git "):]
            n = (len(rest) - 1) // 2
            one, two = rest[:n], rest[n + 1:]
            a, b = (strip(one, "a"), strip(two, "b")) if len(rest) % 2 == 1 and rest[n] == " " else (None, None)
            if a and b and a.strip('"')[2:] == b.strip('"')[2:]:
                line = f"diff --git {a} {b}" + line[len(body):]
        elif body.startswith("@@"):
            header = False
        elif header and body.startswith(("--- ", "+++ ")):
            side = "a" if body.startswith("---") else "b"
            path, sep, tail = body[4:].partition("\t")          # git's tab after a name with spaces
            fixed = strip(path, side)
            if fixed:
                line = body[:4] + fixed + sep + tail + line[len(body):]
        out.append(line)
    return "".join(out)


# RRSIConfig fields this method does not read, and what sets the same thing here
NOT_HERE = {"proposer_model": "the proposer's model is the Evolver's own (evolver.model)",
            "analyst_model": "the roles' model is evolver.params.roles.model",
            "critic_model": "the roles' model is evolver.params.roles.model",
            "eval_parallel": "the Trainer evaluates one candidate per step (1 is accepted)"}


class RRSIAdapterEvolver(AgentEvolver):
    supports_resume = False             # every session starts afresh from the files of its round

    # ---------- configuration ----------
    def _cfg(self, ctx):
        from rrsi.config import RRSIConfig
        value = ctx.params.get("rrsi") or {}
        known = {f.name for f in dataclasses.fields(RRSIConfig)} - {"notes"}
        path = None
        if isinstance(value, str):
            path = Path(value) if Path(value).is_absolute() else Path(ctx.base or ".") / value
            value = json.loads(path.read_text())
        # upstream's RRSIConfig.load keeps an unknown key as a note; a misspelt hyperparameter would then be dropped
        # silently. Keys starting with "_" are comments.
        unknown = sorted(k for k in value if k not in known and not str(k).startswith("_"))
        if unknown:
            raise ValueError(f"evolver.params.rrsi{f' ({path})' if path else ''}: not RRSI hyperparameters "
                             f"(RRSIConfig fields): {unknown}")
        unused = sorted(k for k in value if k in NOT_HERE and not (k == "eval_parallel" and value[k] == 1))
        if unused:
            raise ValueError(f"evolver.params.rrsi{f' ({path})' if path else ''}: {unused} have no effect here: "
                             + "; ".join(NOT_HERE[k] for k in unused))
        return RRSIConfig.load(path) if path else RRSIConfig(**{k: v for k, v in value.items() if k in known})

    def _domain(self, ctx):
        spec = ctx.params.get("domain")
        if not spec:
            raise ValueError("evolver.params.domain must name the benchmark's domain: <file>.py:<Class>")
        if getattr(self, "_domain_cache", None) and self._domain_cache[0] == (spec, str(ctx.base)):
            return self._domain_cache[1]
        filename, _, cls = str(spec).partition(":")
        path = Path(filename) if Path(filename).is_absolute() else Path(ctx.base or ".") / filename
        name = "rrsi_adapter_domain_" + hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:10]
        module_spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[name] = module
        module_spec.loader.exec_module(module)
        domain = getattr(module, cls or "DOMAIN")
        domain = domain() if isinstance(domain, type) else domain
        self._domain_cache = ((spec, str(ctx.base)), domain)
        return domain

    # ---------- state ----------
    @staticmethod
    def _rdir(ctx) -> Path:
        d = ctx.workspace.state / RRSI
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _load(ctx) -> dict:
        path = ctx.workspace.state / STATE
        if path.is_file():
            return json.loads(path.read_text())
        return {"phase": "baseline", "t": 0, "handed_back": None, "candidates": {}, "order": [], "round": None,
                "ids": None, "failures": 0}

    @staticmethod
    def _save(ctx, st: dict) -> None:
        _write(ctx.workspace.state / STATE, json.dumps(st, ensure_ascii=False, indent=1))

    def _frontier(self, ctx) -> dict:
        return json.loads((self._rdir(ctx) / "frontier.json").read_text())

    def _delta(self, ctx, cfg) -> float:
        if cfg.delta is not None:
            return float(cfg.delta)
        return float(json.loads((self._rdir(ctx) / "calibration.json").read_text())["delta"])

    def _eval(self, ctx, job: str):
        from rrsi.evaluate import EvalResult
        return EvalResult.load(self._rdir(ctx) / "jobs" / job / "eval.json")

    def _history(self, ctx):
        from rrsi.history import History
        return History(self._rdir(ctx) / "history.jsonl")

    # ---------- the Trainer's calls ----------
    def setup(self, ctx) -> None:
        """What every step needs, checked once: a broken hyperparameter file or domain, or an unset web_search,
        stops the run here rather than failing each step."""
        _require_rrsi()
        if not isinstance(ctx.params.get("web_search"), bool):
            raise ValueError("evolver.params.web_search must be set, true or false")
        role = ctx.params.get("roles") or {}
        if "/" in str(role.get("model") or "") and role.get("binding") is None:
            raise ValueError("evolver.params.roles.model: a Codex model name (with roles.login), or give "
                             "roles.binding too; a `<backend>/<model>` reference is resolved for the proposer only")
        if isinstance(role.get("binding"), dict):        # read at every step (_roles), before its errors count
            from hermit.agent.model.backends import Binding
            try:
                Binding.from_spec(role["binding"]).codex_args()
            except ValueError as exc:
                raise ValueError(f"evolver.params.roles.binding: {exc}") from exc
        cfg = self._cfg(ctx)
        if not 1 <= cfg.m <= len(VARIANTS):
            raise ValueError(f"evolver.params.rrsi: m = {cfg.m} variants a round; at most {len(VARIANTS)} "
                             f"({VARIANTS})")
        self._domain(ctx)
        self._more_hidden(ctx)                               # a "module:" entry naming no installed package

    def step(self, ctx: StepContext) -> StepResult:
        _require_rrsi()
        st = self._load(ctx)
        cfg, dom = self._cfg(ctx), self._domain(ctx)
        n = ctx.progress.step if ctx.progress else 0
        self._usage: list[Usage] = []
        self._lines = [f"# RRSI Adapter step {n}", ""]
        roles = self._roles(ctx)
        store = _store(ctx)
        self._record_code(ctx)
        limit = int(ctx.params.get("max_failures", 3))
        if int(st.get("failures") or 0) >= limit:
            if not st.get("stop_said"):
                # The last steps failed max_failures times in a row. A step the Trainer's clock ended cannot stop the
                # run itself (the Trainer replaces that step's result with its own), so the run stops here, at the
                # next one.
                st["stop_said"] = True
                self._save(ctx, st)
                self._lines.append(f"The last {st['failures']} steps failed in a row (max_failures {limit}): the "
                                   "run stops before this step does anything.")
                return self._result(ctx, st, "stop", f"stopped: {st['failures']} failed steps in a row", roles)
            # The Trainer ran on (trainer.allow_early_stop: false): the count starts again and the step goes on.
            self._lines.append("The Trainer ran on after the Evolver asked to stop: the failures are counted again.")
            st["failures"], st["stop_said"] = 0, False
            self._save(ctx, st)
        if not st["order"]:                                  # the starting adapter, H_0
            _copy_tree(ctx.workspace.adapter, store / "h0" / "adapter")
            st["candidates"]["h0"] = {"round": None, "variant": None, "status": "queued",
                                      "digest": tree_digest(store / "h0" / "adapter")}
            st["order"].append("h0")
            st["handed_back"] = "h0"
            self._save(ctx, st)
        try:
            outcome = self._advance(ctx, st, cfg, dom, n, roles)
        except Stop as exc:
            # The Trainer's clock or budget ended the step: counted, so that a role or a proposer that keeps hanging
            # stops the run after max_failures steps. A step the model's plan held back is not the run's doing.
            if ctx.cancel is not None and ctx.cancel.is_set():
                st["failures"] = int(st.get("failures") or 0) + 1
            self._save(ctx, st)
            status = "stop" if st.get("failures", 0) >= int(ctx.params.get("max_failures", 3)) else "failed"
            return self._result(ctx, st, status, f"stopped: {exc}", roles)
        except Exception as exc:                             # a broken step: counted; the next step resumes
            st["failures"] = int(st.get("failures") or 0) + 1
            self._lines.append(f"\nThe step failed: {type(exc).__name__}: {exc}")
            status = "stop" if st["failures"] >= int(ctx.params.get("max_failures", 3)) else "failed"
            # said only when the Trainer hears it: a step its clock ended has its result replaced
            st["stop_said"] = status == "stop" and not (ctx.cancel is not None and ctx.cancel.is_set())
            self._save(ctx, st)
            return self._result(ctx, st, status, f"{type(exc).__name__}: {str(exc)[:200]}", roles)
        st["failures"], st["stop_said"] = 0, False
        self._save(ctx, st)
        return self._result(ctx, st, outcome[0], outcome[1], roles)

    def _record_code(self, ctx) -> None:
        """The code this step runs (the framework's commit and whether its checkout is dirty, a digest of this
        Evolver's directory, the rrsi package's version), appended to state/rrsi/code.jsonl when it changes."""
        import rrsi
        here = Path(__file__).resolve().parent
        repo = here.parents[4]                    # hermit/agent/evolver/methods/rrsi_adapter
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        dirty = bool(_git(repo, "status", "--porcelain", "--", "hermit").stdout.strip())
        h = hashlib.sha256()
        for f in sorted(here.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                h.update(f.relative_to(here).as_posix().encode() + b"\0" + f.read_bytes())
        try:
            from importlib.metadata import distribution
            url = json.loads(distribution("rrsi").read_text("direct_url.json") or "{}")
            rrsi_commit = (url.get("vcs_info") or {}).get("commit_id") or url.get("url")
        except Exception:                                   # noqa: BLE001 - a source checkout on the path
            rrsi_commit = None
        entry = {"framework_commit": head, "dirty": dirty, "evolver_sha256": h.hexdigest(),
                 "rrsi_commit": rrsi_commit, "rrsi_path": str(Path(rrsi.__file__).parent)}
        path = self._rdir(ctx) / "code.jsonl"
        last = json.loads(path.read_text().splitlines()[-1]) if path.is_file() and path.read_text().strip() else {}
        if {k: last.get(k) for k in entry} != entry:
            with open(path, "a") as f:
                f.write(json.dumps({**entry, "step": ctx.progress.step if ctx.progress else None,
                                    "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}) + "\n")

    def _result(self, ctx, st: dict, status: str, summary: str, roles: Roles) -> StepResult:
        _write(ctx.workspace.output / "report.md", "\n".join(self._lines) + "\n")
        usage = list(self._usage) + roles.total()     # the proposer's sessions, then the roles' (one record per model)
        extra = {"phase": st["phase"], "round": st["t"], "next": st.get("handed_back"),
                 "candidates": len(st["order"])}
        if (self._rdir(ctx) / "frontier.json").is_file():
            fr = self._frontier(ctx)
            extra.update({"incumbent": fr["incumbent"].get("cid"), "S": fr["incumbent"]["S"], "S_star": fr["S_star"]})
        return StepResult(status, summary[:300], usage=usage, extra=extra)

    def _models(self, ctx) -> dict:
        """The models this run's proposer and roles use, in RRSIConfig's fields (its defaults are not read here)."""
        backend = ctx.params.get("backend", "codex")
        proposer = self.settings(ctx, backend).get("model")
        roles = ctx.params.get("roles") or {}
        binding = roles.get("binding") if isinstance(roles.get("binding"), dict) else {}
        role = roles.get("model") or binding.get("model") or self.settings(ctx, "codex").get("model")
        return {"proposer_model": proposer, "analyst_model": role, "critic_model": role}

    def _roles(self, ctx) -> Roles:
        """The roles' sessions: the proposer's Base Agent settings for Codex (its model, login and effort unless
        `roles` gives the roles' own; with the roles' own model, the proposer's binding, price and the provider
        arguments its binding put in `args` are not theirs; `roles.args` replaces the proposer's own arguments, not
        those provider arguments), Codex's own sandbox read-only, `role_timeout_s` each. They do not see the run's
        records, the step's output, the proposers' directories or the other calls' (Roles._session; upstream's roles
        see only the text they are given). Each call's prompt, events and reply are kept in the step's output
        (`rrsi_roles/`), its home and working directory in the scratch."""
        params, ws = ctx.params, ctx.workspace
        role = params.get("roles") or {}
        base = self.settings(ctx, "codex")
        provider = []                                        # the provider arguments the proposer's binding put
        if base.get("binding"):                              # ahead of the user's own in `args`
            from hermit.agent.model.backends import Binding
            try:
                provider = Binding.from_spec(base["binding"]).codex_args()
            except ValueError:                               # no provider of its own (a CLI's model)
                provider = []
            provider = provider if list(base.get("args") or [])[:len(provider)] == provider else []
        if role.get("model") is not None:                    # nor its provider arguments
            args = list(base.get("args") or [])[len(provider):]
            base = {k: v for k, v in base.items() if k not in ("binding", "price", "args")} | (
                {"args": args} if args else {})
            provider = []
        settings = {**base,
                    **{k: role[k] for k in ("model", "binding", "login", "effort", "args", "price")
                       if role.get(k) is not None},
                    "codex_sandbox": "read-only", "timeout_s": float(params.get("role_timeout_s", 1800))}
        if isinstance(role.get("binding"), dict):            # the roles' own provider, as a binding configures Codex
            from hermit.agent.model.backends import Binding
            binding = Binding.from_spec(role["binding"])
            settings["args"] = [*binding.codex_args(), *(role.get("args") or [])]
            settings["model"] = role.get("model") or binding.model
            settings["price"] = role.get("price") or binding.price
            if role.get("login") is None:                    # not the proposer's ChatGPT login: an API provider
                settings.pop("login", None)
        elif "login" in role and role["login"] is None:      # `roles.login: null` clears the proposer's
            settings.pop("login", None)
        if not isinstance(role.get("binding"), dict) and role.get("args") is not None and provider:
            settings["args"] = [*provider, *role["args"]]    # their own arguments, on the proposer's provider still
        hide = [ws.learning_material, ws.state, ws.adapter, ws.history, ws.output, ws.scratch / "rrsi",
                *self._more_hidden(ctx)]
        return Roles(ctx, settings, ws.scratch / "rrsi_roles", hide=hide, records=ws.output / "rrsi_roles")

    def _advance(self, ctx, st: dict, cfg, dom, n: int, roles: Roles) -> tuple[str, str]:
        # 1. The step's rollouts evaluated the candidate handed back last time.
        again = self._record(ctx, st, cfg, dom, n)
        if again is not None:
            return again
        # 2. A round whose candidates are all measured is decided (Algorithm 2).
        rnd = st.get("round")
        if st["phase"] == "evaluate" and rnd and not rnd["queue"] and self._settled(st, rnd):
            self._close(ctx, st, cfg, dom)
        # 3. Rounds are opened until one has a candidate to evaluate, or the horizon is reached (Algorithm 1).
        while st["phase"] == "propose":
            if st["t"] >= cfg.T:
                st["phase"] = "done"
                break
            if ctx.params.get("stop_file") and Path(ctx.params["stop_file"]).expanduser().exists():
                st["phase"] = "done"
                self._lines.append(f"Stopped before round {st['t']}: {ctx.params['stop_file']} exists.")
                break
            if self._open(ctx, st, cfg, dom, roles):
                st["phase"] = "evaluate"
            else:
                self._close(ctx, st, cfg, dom)
            self._save(ctx, st)
        # 4. Hand back the next candidate, or the incumbent when the run is over.
        if st["phase"] == "evaluate":
            cid = st["round"]["queue"][0]
            self._hand_back(ctx, st, cid)
            st["round"]["queue"].pop(0)
            self._save(ctx, st)
            self._lines.append(f"Handed back {cid} (round {st['round']['t']}) for evaluation.")
            return "done", f"round {st['round']['t']}: evaluating {cid}"
        fr = self._frontier(ctx)
        self._hand_back(ctx, st, fr["incumbent"]["cid"])
        self._lines.append(f"The run is over after {st['t']} rounds; H_T = {fr['incumbent']['cid']} "
                           f"(S {fr['incumbent']['S']:.4f}, S* {fr['S_star']:.4f}).")
        return "stop", f"done: H_T = {fr['incumbent']['cid']} (S {fr['incumbent']['S']:.4f})"

    def _hand_back(self, ctx, st: dict, cid: str) -> None:
        source = _store(ctx) / cid / "adapter"
        if not _real(source, ctx.workspace.state) or tree_digest(source) != st["candidates"][cid]["digest"]:
            raise RuntimeError(f"the stored files of {cid} changed after it was stored")
        _install(source, ctx.workspace.adapter)
        st["handed_back"] = cid
        self._save(ctx, st)

    @staticmethod
    def _settled(st: dict, rnd: dict) -> bool:
        return all(st["candidates"][v["cid"]]["status"] in ("evaluated", "eval_invalid")
                   for v in rnd["variants"].values() if v.get("cid"))

    # ---------- 1. recording an evaluation ----------
    def _record(self, ctx, st: dict, cfg, dom, n: int):
        cid = st["handed_back"]
        c = st["candidates"].get(cid) or {}
        current = ctx.workspace.learning_material / "current"
        if c.get("status") not in ("queued", "retry") or not (current / "tasks").is_dir():
            return None
        if tree_digest(ctx.workspace.adapter) != c["digest"]:
            # The rollouts did not run the stored candidate (the working copy was reset or repaired): no credit. It
            # is handed back once more; a second mismatch makes its evaluation invalid, so the round still settles.
            if not c.get("rehanded") or cid == "h0":
                c["rehanded"] = int(c.get("rehanded") or 0) + 1
                self._hand_back(ctx, st, cid)
                self._lines.append(f"The rollouts did not run {cid} as stored; it is handed back again.")
                return "done", f"{cid} handed back again (the working copy differed)"
            c["status"] = "eval_invalid"
            if st.get("round") and c.get("variant") in st["round"]["variants"]:
                st["round"]["variants"][c["variant"]].update(
                    gate_failure="eval_invalid", detail="the working copy that ran is not the stored candidate")
            self._save(ctx, st)
            self._lines.append(f"{cid}: the working copy that ran is still not the stored candidate; invalid.")
            return None
        material = current.resolve()
        halted = dom.halted_rows(material)
        if halted:
            # The rollouts were refused for the model's plan limit (Harbor's HALT): no evaluation of the candidate.
            # The step waits for the halt to clear (bounded below the call timeout), then fails without changing
            # anything, so the next step's rollouts evaluate the same candidate again.
            self._wait_for_halt(ctx)
            self._lines.append(f"{len(halted)} rollouts of {cid} were halted (the plan's limit); it is evaluated "
                               f"again at the next step.")
            return "failed", f"{cid}: {len(halted)} rollouts halted by the plan's limit; evaluating again"
        repaired = (current / "check_feedback.json").is_file()
        ev = dom.evaluation(material, cid, cfg.k, ids=st.get("ids"))
        invalid = repaired or ev.missing > cfg.invalid_missing_frac * ev.n_expected
        if invalid and not c.get("retried"):
            c["retried"] = True
            c["status"] = "retry"
            self._save(ctx, st)
            why = "the Trainer repaired it" if repaired else f"{ev.missing}/{ev.n_expected} trials missing"
            self._lines.append(f"{cid}: {why}; evaluated again (once), as upstream re-measures an invalid evaluation.")
            return "done", f"{cid}: {why}; evaluating again"
        jobs = self._rdir(ctx) / "jobs" / cid
        jobs.mkdir(parents=True, exist_ok=True)
        c["evaluations"] = (c.get("evaluations") or []) + [{"step": n, "material": material.name, "S": ev.S,
                                                             "C": ev.C, "missing": ev.missing}]
        if cid == "h0":
            if invalid:
                c["status"] = "eval_invalid"
                self._save(ctx, st)
                self._lines.append(f"The baseline is invalid: {ev.missing}/{ev.n_expected} trials missing.")
                return "stop", "baseline invalid: too many trials missing"
            self._baseline(ctx, st, cfg, ev)
            return None
        rnd = st["round"]
        v = c["variant"]
        if invalid:
            c["status"] = "eval_invalid"
            rnd["variants"][v]["gate_failure"] = "eval_invalid"
            rnd["variants"][v]["detail"] = (f"{ev.missing}/{ev.n_expected} trials missing (infrastructure) after a "
                                            f"retry" if not repaired else "the Trainer repaired the candidate")
            self._lines.append(f"{cid}: evaluation invalid after a retry.")
        else:
            c["status"] = "evaluated"
            ev.save(jobs / "eval.json")
            ev.save(self._rdir(ctx) / f"r{rnd['t']}" / v / "eval.json")
            self._lines.append(f"Evaluated {cid}: S {ev.S:.4f}, C {ev.C}, missing {ev.missing}/{ev.n_expected}.")
        self._save(ctx, st)
        return None

    def _wait_for_halt(self, ctx) -> None:
        """While `halt_file` exists (a benchmark's halt sentinel, e.g. Harbor's `<jobs_dir>/HALT`), wait, up to
        `halt_wait_min`; the Trainer's cancel ends the wait."""
        path = ctx.params.get("halt_file")
        if not path:
            return
        path = Path(path).expanduser()
        deadline = time.time() + 60 * float(ctx.params.get("halt_wait_min", 240))
        while path.exists() and time.time() < deadline:
            if ctx.cancel is not None and ctx.cancel.wait(60):
                raise Stop("stopped by the Trainer while waiting for the halt to clear")
            if ctx.cancel is None:
                time.sleep(60)

    def _baseline(self, ctx, st: dict, cfg, ev) -> None:
        """H_0's evaluation seeds the frontier, delta is calibrated from it (upstream's driver: baseline, then
        calibrate(["base"]) when the config gives no delta), and the history gets its BASELINE row."""
        from rrsi.calibrate import calibrate, write
        R = self._rdir(ctx)
        (R / "jobs" / "h0").mkdir(parents=True, exist_ok=True)
        ev.save(R / "jobs" / "h0" / "eval.json")
        fr = {"domain": "adapter", "incumbent": {"t": 0, "cid": "h0", "job": "h0", "S": ev.S, "C": ev.C,
                                                 "extra": ev.extra},
              "S_star": ev.S, "trajectory": [{"t": 0, "S": ev.S, "C": ev.C, "cid": "h0", "job": "h0"}],
              "config": {**cfg.dump(), **self._models(ctx)}}
        _write(R / "frontier.json", json.dumps(fr, indent=1))
        if cfg.delta is None:
            cal = calibrate([ev], z=cfg.delta_z)
            cal["jobs"] = ["h0"]
            write(R / "calibration.json", cal)
            self._lines.append(f"Calibrated delta = {cal['delta']:.5f} ({cal['method']}, sd_null {cal['sd_null']}).")
        history = self._history(ctx)
        if not any(r.get("outcome") == "BASELINE" for r in history.records()):
            history.append({"t": 0, "variant": "-", "edit_id": None, "component": None, "hypothesis": "H_0 baseline",
                            "outcome": "BASELINE", "S": ev.S, "C": ev.C, "accepted": True, "delta_S": None,
                            "delta_C": None, "bundle": 0})
        st["ids"] = sorted(ev.per_task)                 # marked evaluated only once everything above is written
        st["candidates"]["h0"]["status"] = "evaluated"
        st["phase"], st["t"] = "propose", 0
        self._save(ctx, st)
        self._lines.append(f"Baseline H_0: S {ev.S:.4f}, C {ev.C}, missing {ev.missing}/{ev.n_expected} "
                           f"({len(ev.per_task)} tasks x k={ev.k}).")

    # ---------- 2. Algorithm 2: deciding a round ----------
    def _close(self, ctx, st: dict, cfg, dom) -> None:
        from rrsi.evaluate import EvalResult
        from rrsi.selection import Candidate, select_round
        rnd = st["round"] or {"t": st["t"], "variants": {}, "queue": []}
        t = rnd["t"]
        R = self._rdir(ctx)
        rdir = R / f"r{t}"
        rdir.mkdir(parents=True, exist_ok=True)
        fr = self._frontier(ctx)
        if any(x["t"] == t + 1 for x in fr["trajectory"]):
            # decided (H_{t+1} written) by a step that ended before it saved the state: not weighed again against
            # the winner itself
            st["round"], st["t"], st["phase"] = None, t + 1, "propose"
            self._save(ctx, st)
            return
        inc_ev = self._eval(ctx, fr["incumbent"]["job"])
        delta = self._delta(ctx, cfg)
        history = self._history(ctx)
        cands = []
        for v in sorted(rnd["variants"]):
            info = rnd["variants"][v]
            ep = rdir / v / "eval.json"
            ev = EvalResult.load(ep) if ep.is_file() and not info.get("gate_failure") else None
            cands.append(Candidate(v, info.get("edits") or [], ev=ev, diff_path=info.get("diff_path"),
                                   commit=info.get("cid"), gate_failure=info.get("gate_failure") or
                                   (None if ev is not None else "not_evaluated"), detail=info.get("detail") or ""))
        # the incumbent's components before this round: a retried close (its earlier try wrote some of this round's
        # rows) decides as the first try did
        from rrsi.components import K
        counts = {c: 0 for c in K}
        for r in history.records():
            if r.get("accepted") and r.get("component") in counts and r.get("t") is not None and int(r["t"]) < t:
                counts[r["component"]] += 1
        winner, decisions = select_round(cands, inc_ev, fr["S_star"], delta, cfg, counts, guard_fn=dom.guards)
        _write(rdir / "decisions.json", json.dumps([d.to_json() for d in decisions], indent=1))
        attributed = self._attributed(ctx, t)
        for c, dec in zip(cands, decisions):
            if c.ev is not None and c.variant not in attributed:     # also on a retry whose history row is written
                self._attribute(ctx, dom, cfg, t, c.variant, c.edits, inc_ev, c.ev)
            if history.has(t, c.variant):
                continue                                     # a resumed step: already recorded
            if c.ev is None:
                history.append_candidate(t, c.variant, c.edits, c.gate_failure or "not_evaluated", None, None, False,
                                         None, None, c.diff_path, c.detail)
                self._lines.append(f"r{t}{c.variant}: {c.gate_failure} {c.detail[:200]}")
                continue
            outcome = "ACCEPTED" if c is winner else ("LOST" if dec.admissible else "REJECTED")
            history.append_candidate(t, c.variant, c.edits, outcome, dec.delta_S, dec.delta_C, c is winner, dec.S,
                                     dec.C, c.diff_path, dec.reason)
            self._lines.append(f"r{t}{c.variant}: S {dec.S:.4f} dS {dec.delta_S:+.4f} dC {dec.delta_C:+.3f} "
                               f"nu {dec.novelty} -> {outcome}: {dec.reason}")
        if winner is not None:
            fr["incumbent"] = {"t": t + 1, "cid": winner.commit, "job": winner.commit, "S": winner.ev.S,
                               "C": winner.ev.C, "extra": winner.ev.extra, "variant": winner.variant}
            fr["S_star"] = max(fr["S_star"], winner.ev.S)
            new_S, new_C = winner.ev.S, winner.ev.C
            self._lines.append(f"ACCEPTED r{t}{winner.variant} ({winner.commit}): S {new_S:.4f}, S* {fr['S_star']:.4f}.")
        else:
            new_S, new_C = inc_ev.S, inc_ev.C
            self._lines.append(f"Round {t}: no admissible candidate; H_{t + 1} = H_{t} ({fr['incumbent']['cid']}).")
        fr["trajectory"] = [x for x in fr["trajectory"] if x["t"] <= t] + [
            {"t": t + 1, "S": new_S, "C": new_C, "cid": fr["incumbent"]["cid"], "job": fr["incumbent"]["job"]}]
        _write(R / "frontier.json", json.dumps(fr, indent=1))
        st["round"], st["t"], st["phase"] = None, t + 1, "propose"
        self._save(ctx, st)

    def _attribute(self, ctx, dom, cfg, t: int, variant: str, edits: list, inc, cand) -> None:
        """Upstream's Run.attribute: each edit's predictions against the measured per-task change."""
        thr = dom.regression_threshold(cfg.k)
        rows = []
        for e in edits:
            pred = [str(p) for p in (e.get("predicted_affected") or []) if str(p) in inc.per_task
                    and str(p) in cand.per_task]
            hits = [p for p in pred if cand.per_task[p].mean > inc.per_task[p].mean]
            drops = [x for x in inc.per_task if x in cand.per_task
                     and cand.per_task[x].mean - inc.per_task[x].mean <= -thr]
            rows.append({"t": t, "variant": variant, "edit_id": e.get("id"), "component": e.get("component"),
                         "hypothesis": str(e.get("hypothesis"))[:160], "n_predicted": len(pred),
                         "predicted_hit": hits, "hit_rate": round(len(hits) / len(pred), 2) if pred else None,
                         "unpredicted_regressions": [d for d in drops if d not in pred][:12]})
        with open(self._rdir(ctx) / "attribution.jsonl", "a") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def _attributed(self, ctx, t: int) -> set:
        """The variants of round t whose attribution rows are written."""
        path = self._rdir(ctx) / "attribution.jsonl"
        if not path.is_file():
            return set()
        rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        return {r.get("variant") for r in rows if r.get("t") == t}

    def _scoreboard(self, ctx) -> list:
        path = self._rdir(ctx) / "attribution.jsonl"
        if not path.is_file():
            return []
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()][-20:]

    # ---------- 3. Algorithm 1: opening a round ----------
    def _traces(self, ctx, dom, cfg, inc_ev) -> dict:
        """Upstream's Run.build_traces: the worst trial of the lowest-scoring tasks and the best trial of the
        highest-scoring ones, from the incumbent's own evaluation."""
        material = ctx.workspace.learning_material / inc_ev.extra["material"]
        ranked = sorted(inc_ev.per_task.items(), key=lambda kv: kv[1].mean)
        fails = [t for t, _ in ranked[:cfg.n_fail_traces]]
        wins = [t for t, _ in ranked[-cfg.n_success_traces:] if t not in fails]
        traces = {}
        for tid in fails + wins:
            tr = inc_ev.per_task[tid]
            if not tr.rewards:
                continue
            idx = tr.rewards.index(min(tr.rewards)) if tid in fails else tr.rewards.index(max(tr.rewards))
            rec = dom.trace(material, tid, idx, tr)
            if rec is not None:
                traces[tid] = rec
        return traces

    def _open(self, ctx, st: dict, cfg, dom, roles: Roles) -> bool:
        from rrsi.analyst import analyze, load_digests
        from rrsi.history import exploration, stall_flag
        from rrsi.schedule import edit_budget
        t = st["t"]
        R = self._rdir(ctx)
        rdir = R / f"r{t}"
        rdir.mkdir(parents=True, exist_ok=True)
        fr = self._frontier(ctx)
        inc_ev = self._eval(ctx, fr["incumbent"]["job"])
        delta = self._delta(ctx, cfg)
        history = self._history(ctx)
        traces = self._traces(ctx, dom, cfg, inc_ev)
        if len(traces) < 0.5 * min(len(inc_ev.per_task), cfg.n_fail_traces + cfg.n_success_traces):
            raise RuntimeError(f"only {len(traces)} traces available from {fr['incumbent']['job']}")
        report_path = rdir / "analysis_report.json"
        if report_path.is_file():
            report = json.loads(report_path.read_text())
        else:
            shutil.rmtree(rdir / "analysis", ignore_errors=True)     # a dead attempt's partial digests
            ga = R / "global_analysis.json"
            prior = json.loads(ga.read_text()) if ga.is_file() else {}
            # upstream's analyze() renders the traces first; rendered here too, so the digesters' sessions can run
            # in a copy of them (the state is hidden from every role session)
            from rrsi.analyst import prepare_rendered
            prepare_rendered(dom, traces, rdir / "analysis" / "rendered")
            shared = ctx.workspace.scratch / "rrsi_roles" / f"rendered_r{t}"
            shutil.rmtree(shared, ignore_errors=True)
            shutil.copytree(rdir / "analysis" / "rendered", shared)
            roles.traces = shared
            with bound(roles.generate, roles.sandboxed_subprocess()):
                report = analyze(dom, traces, inc_ev.per_task, rdir, prior_modes=prior.get("failure_modes"),
                                 prior_habits=prior.get("success_habits"), model=None)
            _write(report_path, json.dumps(report, ensure_ascii=False, indent=1))
        if report.get("error"):                              # upstream goes on with an empty report
            self._lines.append(f"The analyst's report is empty: {report['error']}.")
        _write(R / "global_analysis.json", json.dumps({"failure_modes": report.get("failure_modes"),
                                                       "success_habits": report.get("success_habits")},
                                                      ensure_ascii=False, indent=1))
        budget = edit_budget(t, cfg.T, cfg.b_min, cfg.b_max)
        sigma = stall_flag([x["S"] for x in fr["trajectory"]], t, cfg.w, delta)
        tried = history.tried()
        # Components the benchmark's agent cannot carry are never offered as unexplored: a reserved slot on one
        # could not be met (upstream's scaffold carries every component of K).
        from rrsi.components import K
        uncarried = sorted(set(K) - set(dom.carriers or K))
        explore = exploration(t, sigma, tried | set(uncarried), cfg.m_draft)
        prune = history.prune_set(t, cfg.n_prune)
        rows = history.render()
        directives = {"t": t, "b_t": budget, "sigma_t": sigma, "tried": sorted(tried), "uncarried": uncarried,
                      "explore": explore, "prune_set": prune, "delta": delta, "S_star": fr["S_star"]}
        _write(rdir / "directives.json", json.dumps(directives, indent=1))
        self._lines.append(f"Round {t}/{cfg.T}: incumbent {fr['incumbent']['cid']} S {inc_ev.S:.4f}, S* "
                           f"{fr['S_star']:.4f}, delta {delta:.5f}; b_t {budget}, sigma_t {sigma}, untried "
                           f"{explore['untried']}, prune {[p['component'] for p in prune]}; top modes "
                           f"{[m.get('mode') for m in (report.get('failure_modes') or [])[:5]]}.")
        rnd = st.get("round") or {"t": t, "variants": {}, "queue": []}
        st["round"] = rnd
        self._save(ctx, st)
        bundle = {"report": report, "digests": load_digests(rdir), "traces": traces, "inc_ev": inc_ev,
                  "uncarried": uncarried,
                  "budget": budget, "explore": explore, "prune": prune, "rows": rows, "rdir": rdir,
                  "constitution": dom.constitution()}
        for v in range(cfg.m):
            vid = VARIANTS[v]
            if vid in rnd["variants"]:
                continue
            reserved = bool(sigma and explore["untried"] and v >= cfg.m - cfg.m_draft)
            info = self._draft(ctx, st, cfg, dom, t, vid, reserved, bundle, roles)
            rnd["variants"][vid] = info
            if info.get("cid"):
                rnd["queue"].append(info["cid"])
            self._save(ctx, st)
        return bool(rnd["queue"])

    # ---------- drafting one variant ----------
    def _context(self, ctx, dom, t: int, vid: str, reserved: bool, bundle: dict, work: Path, m: int) -> Path:
        """The round's context for one variant, as upstream's propose() assembles it, written as files the
        session reads (a prompt of that size is no command-line argument)."""
        cdir = work / "context"
        cdir.mkdir(parents=True, exist_ok=True)
        explore = bundle["explore"]
        explore_text = explore.get("text", "")
        if reserved:
            explore_text += ("\n\nRESERVED EXPLORATION SLOT: this variant holds one. At least one of your edits MUST "
                             f"be on a never-exercised component from: {explore.get('untried')}.")
        if bundle.get("uncarried"):
            explore_text += (f"\n\nComponents this benchmark's domain does not offer for exploration (outside its "
                             f"carriers): {bundle['uncarried']}.")
        explore_text += f"\n\nHow the diff evidences each component (tags are checked against it): {dom.evidence}"
        brief = (f"You are variant {vid} of round {t}. {m} variants are drafted independently from the same "
                 f"incumbent this round and each is evaluated on the full evolve set; the best admissible one becomes "
                 f"H_{t + 1}.")
        sections = [
            "=== THIS VARIANT'S BRIEF ===", brief,
            "=== EDIT HISTORY L_t (every measured edit: component, hypothesis, Delta S, Delta C, accepted). A rejected "
            "mechanism is negative evidence; do not redraw it unchanged. An accepted one carries the gain it "
            "produced; refine what has known credit, not what merely preceded a rise. ===",
            json.dumps(bundle["rows"], ensure_ascii=False, indent=1),
            "=== ATTRIBUTION SCOREBOARD (how past edits' predictions fared; unpredicted_regressions are tasks an edit "
            "likely broke) ===", json.dumps(self._scoreboard(ctx), ensure_ascii=False, indent=1),
            "=== EXPLORATION DIRECTIVES E_t ===", explore_text,
            "=== COMPONENTS TO PRUNE B_t (exercised, no strictly improving edit in the recent window; remove the "
            "accepted machinery listed, it has stopped earning its place) ===",
            json.dumps(bundle["prune"], ensure_ascii=False, indent=1) if bundle["prune"] else "(none)",
            "=== THIS ROUND'S EDIT BUDGET b_t ===",
            f"You may ship AT MOST {bundle['budget']} independent edit(s) in this candidate (the budget anneals over "
            f"the run: early rounds explore, late rounds make single attributable changes). Ship fewer if the "
            f"evidence supports fewer.",
        ]
        (cdir / "round.md").write_text("\n\n".join(sections) + "\n")
        (cdir / "analysis_report.json").write_text(json.dumps(bundle["report"], ensure_ascii=False, indent=1))
        (cdir / "digests.json").write_text(json.dumps(bundle["digests"], ensure_ascii=False, indent=1))
        traces_dir = cdir / "traces"
        traces_dir.mkdir(exist_ok=True)
        rendered = bundle["rdir"] / "analysis" / "rendered"
        modes: dict = {}                                    # upstream's _list_traces: the digests' labels per task
        for f in bundle["digests"] or []:
            label = f.get("blocker") or f.get("wanted") or f.get("lens") or "?"
            modes.setdefault(str(f.get("task_id")), []).append(str(label)[:80])
        rows = []
        for tid, rec in bundle["traces"].items():
            src = rendered / f"{tid}.txt"
            text = src.read_text() if src.is_file() else dom.render_trace(rec, detail=True)
            (traces_dir / f"{tid}.txt").write_text(text)
            tr = bundle["inc_ev"].per_task.get(tid)
            rows.append((tr.mean if tr else 0.0, dom.task_row(tid, rec, tr) + f" | modes={modes.get(tid, [])}"))
        (cdir / "tasks.md").write_text("This round's traces (lowest score first); each is traces/<task id>.txt\n\n"
                                       + "\n".join(r for _, r in sorted(rows, key=lambda x: x[0])) + "\n")
        skill_md, patterns_md = bundle["constitution"]
        (cdir / "SKILL.md").write_text(skill_md)
        (cdir / "PATTERNS.md").write_text(patterns_md)
        return cdir

    @staticmethod
    def _more_hidden(ctx) -> list[Path]:
        """`hide`: more paths hidden from the proposer and the roles, read as AgentEvolver's `hide` reads them (a path,
        or "module:<name>" for an installed package), without the framework's directories it adds."""
        from hermit.sandbox import hidden_paths
        return hidden_paths(ctx.params.get("hide") or [])[len(hidden_paths(None)):]

    def _hidden_from_proposer(self, ctx, work: Path) -> list[Path]:
        """What a proposer session must not see: the learning material and the state (the evaluations, every
        candidate, upstream's records), the adapter's history and working copy, the step's output (other sessions'
        logs), the roles' calls, and every other variant's or round's working directory. Upstream's proposer reads only
        its own worktree and the round's context."""
        ws = ctx.workspace
        hide = [ws.learning_material, ws.state, ws.history, ws.adapter, ws.output, ws.scratch / "rrsi_roles"]
        root = ws.scratch / "rrsi"
        hide += [p for p in sorted(root.iterdir()) if not p.is_symlink() and p.resolve() != work.resolve()] \
            if root.is_dir() else []
        hide += [p for p in sorted(ws.scratch.iterdir()) if p.name not in ("rrsi", "rrsi_roles")] \
            if ws.scratch.is_dir() else []
        return [p for p in hide if p.exists()]

    def _session(self, ctx, prompt: str, path: Path, work: Path, session: str | None, log: Path) -> StepResult:
        """One proposer session (or a resumed one) of the `codex` or `claude` Base Agent, in the variant's working
        directory (its candidate under adapter/, its context under context/), with everything else the run keeps
        hidden (`Limits.hidden`: the run's sandbox covers it, or the Base Agent refuses the call), the Trainer's
        allowed tools, and its event log at `log`. Its home is the variant's own: a later variant never reads an
        earlier one's sessions."""
        params = ctx.params
        backend = params.get("backend", "codex")
        home = work / f".{backend}-home"
        if not _owned(work, ctx.workspace.scratch) or not _owned(home, ctx.workspace.scratch):
            return StepResult("failed", "the proposer's directory or home was replaced by a link or a file",
                              extra={"proposer_fault": True})
        _write(path, prompt)
        allowed = params.get("tools")
        tools = [t for name, t in ctx.tools.items() if allowed is None or name in allowed]
        # The paths are hidden here rather than through AgentEvolver's `hide`, which also covers the framework's
        # examples/: a run whose output_dir lies under examples/ would lose its own workspace. With the run's sandbox
        # off nothing can be hidden, and the session runs as every learner of such a run does.
        hidden = [*self._hidden_from_proposer(ctx, work), *self._more_hidden(ctx)] \
            if getattr(ctx.sandbox, "enabled", False) else []
        ws = ctx.workspace
        limits = Limits(hidden=hidden, readable=[Mount(ctx.workspace.task_info, ctx.workspace.task_info)])
        tries = int(params.get("session_retries", 2))
        tail = ""
        for attempt in range(1 + tries):
            # a session can write its directory's parent (the run's scratch): its directory or home replaced by a
            # link would be bound into the next session read-write, past what is hidden; and the host writes into
            # the home (the sign-in, the settings), so a link left inside it is removed, never followed
            if not _owned(work, ws.scratch) or not _owned(home, ws.scratch):
                return StepResult("failed", "the proposer's directory or home was replaced by a link or a file",
                                  extra={"proposer_fault": True})
            _unlink_links(home)
            kept = log if attempt == 0 else log.with_name(f"{log.stem}.retry{attempt}{log.suffix}")
            kept.parent.mkdir(parents=True, exist_ok=True)
            agent = make(backend, {**self.settings(ctx, backend), "home": home, "log": kept})
            result = agent.run(prompt, workspace=work, tools=tools, limits=limits, session=session)
            self._usage += result.usage
            extra, tail = result.extra, str(result.extra.get("output_tail") or "")
            if extra.get("cancelled") or (ctx.cancel is not None and ctx.cancel.is_set()):
                raise Stop("stopped by the Trainer during a proposer session")
            # the Base Agent says whether the plan refused it (hermit >= #69); before, its output's tail is read
            refused = extra["refused"] if "refused" in extra else codex_login.refused(tail)
            if (not extra.get("started") and extra.get("unavailable")) or (not extra.get("served") and refused):
                # the model's plan held the session back past its wait, or refused it: not the proposer's doing, so
                # the variant is drafted again later, never recorded as a gate failure
                raise Stop(f"the model's plan did not serve a proposer session: {tail[-200:]}")
            if result.exit_code == 0 or extra.get("served") or not extra.get("unavailable"):
                return StepResult("done" if result.exit_code == 0 else "failed",
                                  f"{backend} session exited with {result.exit_code}", session=result.session,
                                  usage=result.usage)
            # the provider could not serve it (429, 5xx, overloaded): not the proposer's doing either; the same
            # session is resumed after a pause, and a provider that keeps failing stops the step
            self._lines.append(f"A proposer session failed on the provider ({tail[-160:]}); retry {attempt + 1}.")
            session = result.session or session
            wait = float(params.get("session_retry_wait_s", 120)) * (attempt + 1)
            if attempt < tries and ctx.cancel is not None and ctx.cancel.wait(wait):
                raise Stop("stopped by the Trainer during a proposer session")
            if attempt < tries and ctx.cancel is None:
                time.sleep(wait)
        raise Stop(f"the model's provider kept failing a proposer session: {tail[-200:]}")

    @staticmethod
    def _check_done(done: Path, budget: int, reserved: bool, explore: dict, changed: bool,
                    links: list | None = None, not_text: list | None = None) -> tuple[dict | None, list, bool]:
        """Upstream's done() contract (rrsi/propose.py): an edits array within the budget, every edit tagged from
        K with its fields, a reserved exploration slot honoured, and file changes behind the declared edits.
        Returns (proposal, problems, final): problems go back to the session unless `final` (upstream's no_proposal:
        nothing changed and nothing declared)."""
        from rrsi.components import K
        if links:
            return None, [f"symbolic links are not allowed in a candidate: {', '.join(links[:5])}; replace them with "
                          "files"], False
        if not_text:
            return None, [f"a candidate's files are UTF-8 text (no NUL bytes, no other encoding, names in UTF-8): "
                          f"{', '.join(not_text[:5])}; write them as text (CSV, JSON, Markdown, code)"], False
        if done.is_symlink() or not done.is_file():
            return None, [f"{done} was not written (as a file, not a link)"], False
        try:
            act = json.loads(done.read_text())
        except ValueError as exc:
            return None, [f"{done} is not valid JSON: {exc}"], False
        if not isinstance(act, dict):
            return None, [f"{done} must hold one JSON object"], False
        edits = act.get("edits") or act.get("candidates") or []
        if not isinstance(edits, list):
            return None, ["edits must be an array"], False
        if not changed and edits:
            return None, ["you declared edits but made ZERO file changes. Implement them in the working directory, "
                          "then write done.json again."], False
        if not changed:
            return None, ["no file of the adapter changed and no edit was declared"], True
        problems = []
        if not edits:
            problems.append("edits array is empty")
        if len(edits) > budget:
            problems.append(f"{len(edits)} edits exceed the budget b_t = {budget}")
        for e in edits:
            if not isinstance(e, dict):
                problems.append("every edit must be an object")
                continue
            miss = [f for f in REQUIRED if not e.get(f)]
            if miss:
                problems.append(f"edit {e.get('id', '?')} missing {miss}")
            if e.get("predicted_affected") and not isinstance(e["predicted_affected"], list):
                problems.append(f"edit {e.get('id')} predicted_affected must be an array of task ids")
            if e.get("component") and str(e["component"]).lower() not in K:
                problems.append(f"edit {e.get('id')} component {e['component']!r} not in {K}")
        if reserved and explore.get("untried") and edits and not any(
                isinstance(e, dict) and str(e.get("component", "")).lower() in explore["untried"] for e in edits):
            problems.append("this variant holds a RESERVED EXPLORATION SLOT: at least one edit must be on a "
                            f"never-exercised component from {explore['untried']}")
        if problems:
            return None, problems, False
        for e in edits:
            e["component"] = str(e.get("component", "")).lower()
            e.setdefault("mechanism", e.get("hypothesis"))
        return {"summary": act.get("summary"), "edits": edits,
                "mechanism": act.get("summary") or "; ".join(str(e.get("hypothesis")) for e in edits)[:200],
                "targets_mode": ", ".join(str(e.get("targets_mode")) for e in edits)[:200]}, [], False

    def _draft(self, ctx, st: dict, cfg, dom, t: int, vid: str, reserved: bool, bundle: dict,
               roles: Roles) -> dict:
        from rrsi.components import K, normalize
        from rrsi.critic import review
        vdir = bundle["rdir"] / vid
        vdir.mkdir(parents=True, exist_ok=True)
        prep = vdir / "prep.json"
        if prep.is_file():
            return json.loads(prep.read_text())
        # no prep.json: a draft that failed before it was decided left its files here (its proposal, critic verdicts,
        # diff); the redraft starts without them
        shutil.rmtree(vdir)
        vdir.mkdir()
        ws = ctx.workspace
        root = ws.scratch / "rrsi"
        if not _real(root, ws.scratch) or (root.exists() and not root.is_dir()):
            _remove(root)                           # the proposers' parent, replaced by a link or a file
        root.mkdir(parents=True, exist_ok=True)
        for p in list(root.iterdir()):              # a link a proposer left for a later variant to follow
            if p.is_symlink():
                p.unlink()
        work = root / f"r{t}{vid}"
        _remove(work)
        cand = work / "adapter"
        staged = work / "staged"
        fr = self._frontier(ctx)
        base = _store(ctx) / fr["incumbent"]["cid"] / "adapter"
        _copy_tree(base, cand)
        shutil.copytree(ws.adapter / ".hermit", cand / ".hermit")
        base_digest = tree_digest(base)
        cdir = self._context(ctx, dom, t, vid, reserved, bundle, work, cfg.m)
        done = work / DONE
        text = (Path(self.directory) / ctx.params.get("prompt", "PROPOSE.md")).read_text()
        values = {"variant": vid, "round": t, "domain_brief": dom.briefs["proposer"], "candidate": cand,
                  "context": cdir, "done": done, "components": " | ".join(K), "work": work,
                  "task_info": ws.task_info}
        for key, value in values.items():
            text = text.replace("{" + key + "}", str(value))
        text = text.replace("{{", "{").replace("}}", "}")
        logs = ws.output / f"r{t}{vid}"
        n_session = 0

        def run(prompt: str, session: str | None) -> StepResult:
            nonlocal n_session
            n_session += 1
            return self._session(ctx, prompt, work / f"prompt_{n_session}.md", work, session,
                                 logs / f"session_{n_session}.jsonl")

        def finish(gate: str, detail: str = "", edits=None, diff_path=None) -> dict:
            info = {"gate_failure": gate, "detail": detail[:1200], "edits": edits or [], "diff_path": diff_path,
                    "cid": None}
            _write(prep, json.dumps(info, ensure_ascii=False, indent=1))
            self._lines.append(f"r{t}{vid}: {gate} {detail[:200]}")
            return info

        def current() -> tuple[list, list, str | None]:
            """The candidate's links, its staged files that are not UTF-8 text, and the digest of its staged copy
            (None while it holds either)."""
            if not cand.is_dir() or not _real(cand, ws.scratch):
                return ["adapter (it must stay a directory, not a link)"], [], None
            links = links_in(cand)
            if links:
                return links, [], None
            stage(cand, staged)
            not_text = not_text_in(staged, base)
            return [], not_text, None if not_text else tree_digest(staged)

        def propose(prompt: str, session: str | None, since: str) -> tuple[dict | None, str | None, list, bool]:
            """A session, then upstream's done() check against the files changed since `since` (H_t's digest for the
            first call, the repair's start for a repair: upstream counts a call's own edits); problems go back to the
            same session (three times)."""
            result = run(prompt, session)
            session = result.session or session
            for attempt in range(4):
                if not _owned(work, ws.scratch):
                    return None, session, ["the variant's directory was replaced by a link or a file"], True
                links, not_text, digest = current()
                prop, problems, final = self._check_done(done, bundle["budget"], reserved, bundle["explore"],
                                                         digest is not None and digest != since, links, not_text)
                if prop is None and result.status != "done" and not (result.extra or {}).get("proposer_fault"):
                    # The session failed (it exited non-zero) and left no proposal: no choice of the proposer's.
                    # Upstream's driver stops on a proposer call that raises; here the step fails (counted toward
                    # max_failures) and the variant is drafted again at the next step.
                    raise RuntimeError(f"r{t}{vid}: a proposer session failed without a proposal "
                                       f"({result.summary})")
                if prop is not None or final or session is None or attempt == 3:
                    return prop, session, problems, final
                _remove(done)
                result = run(f"ERROR: {problems}. Fix them and write {done} again (drop or merge edits if over "
                             f"budget; add the required edit if a slot is reserved).", session)
                session = result.session or session
            return None, session, problems, False

        prop, session, problems, _ = propose(text, None, base_digest)
        _write(vdir / "proposal.json", json.dumps({"proposal": prop, "problems": problems, "session": session},
                                                  ensure_ascii=False, indent=1))
        if prop is None:
            return finish("no_proposal", "; ".join(map(str, problems)))
        # Critic(H_t, H') with bounded repair (upstream's _draft), on the staged copy: exactly what is stored
        verdict, diff = None, ""
        patterns = list(dom.critic_patterns) + list(dom.leak_patterns(self._seen_records(ctx, st)))
        for attempt in range(1 + cfg.repair_rounds):
            diff = diff_of(base, staged)
            (vdir / "diff.patch").write_text(diff)
            screened = dom.screen(staged)
            if screened:                            # the domain's own check of the stored files, as a precheck
                verdict = {"verdict": "reject", "reasons": [f"precheck: {p}" for p in screened], "risk_notes": []}
            else:
                with bound(roles.generate, roles.sandboxed_subprocess()):
                    verdict = review(_Patterns(dom, patterns), diff, prop.get("mechanism") or "",
                                     prop.get("targets_mode") or "", edits=prop.get("edits"), model=None)
            if verdict.get("verdict") == "accept":
                tagged = [normalize(e.get("component"), diff, dom.component_signals) for e in prop.get("edits") or []]
                for e, c in zip(prop.get("edits") or [], tagged):
                    if c != str(e.get("component", "")).lower():
                        self._lines.append(f"r{t}{vid}: edit {e.get('id')} declared '{e.get('component')}' but the "
                                           f"diff reads as '{c}'; re-tagged")
                if reserved and bundle["explore"].get("untried") and not any(
                        c in bundle["explore"]["untried"] for c in tagged):
                    verdict = {"verdict": "reject",
                               "reasons": ["this variant holds a RESERVED EXPLORATION SLOT: at least one edit must be "
                                           f"on a never-exercised component from {bundle['explore']['untried']}, "
                                           "judged by the DIFF, and none is. The diff evidences a component this way: "
                                           + dom.evidence],
                               "risk_notes": verdict.get("risk_notes"), "slot_check": True}
            _write(vdir / f"critic_a{attempt}.json", json.dumps(verdict, indent=1))
            if verdict.get("verdict") == "accept" or attempt >= cfg.repair_rounds or session is None:
                break
            brief = {"reasons": verdict.get("reasons"), "risk_notes": verdict.get("risk_notes"),
                     "your_declared_edits": prop.get("edits")}
            if not _owned(work, ws.scratch):
                return finish("no_proposal", "the variant's directory was replaced by a link or a file",
                              prop.get("edits"))
            _remove(done)
            _, _, start = current()
            repaired, session, problems, _ = propose(REPAIR.replace("{done}", str(done))
                                                     + json.dumps(brief, ensure_ascii=False, indent=1), session,
                                                     start or "")
            _write(vdir / f"proposal_r{attempt + 1}.json",
                   json.dumps({"proposal": repaired, "problems": problems}, ensure_ascii=False, indent=1))
            if repaired is None:
                prop = {"edits": []}                    # upstream: the failed repair's result is the proposal
                break
            prop = repaired
        _write(vdir / "critic.json", json.dumps(verdict, indent=1))
        edits = prop.get("edits") or []
        if not verdict or verdict.get("verdict") != "accept":
            return finish("critic_reject", str((verdict or {}).get("reasons")), edits, str(vdir / "diff.patch"))
        links, not_text, digest = current()
        if links or not_text:
            return finish("no_proposal", f"symbolic links or files that are not text in the candidate: "
                                         f"{(links + not_text)[:5]}", edits)
        dropped = stage(cand, staged)
        # tag (l', h', d'): the declared component validated against the diff of what is stored
        diff = diff_of(base, staged)
        (vdir / "diff.patch").write_text(diff)
        for e in edits:
            e["component"] = normalize(e.get("component"), diff, dom.component_signals)
        # the template's check, on the candidate as it will be stored (its .hermit/ the template's), then the
        # liveness smoke: does the candidate run at all (not a selection rule)
        cid = f"r{t:02d}{vid}"
        dest = _store(ctx) / cid
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True)
        shutil.copytree(staged, dest / "adapter")
        checked = dest / "checked"
        _copy_tree(dest / "adapter", checked)
        shutil.copytree(ws.adapter / ".hermit", checked / ".hermit")
        try:
            check = ctx.tools["check_spec"](path=str(checked))
            if not check.get("passed"):
                issues = [f"{i.get('path')}: {i.get('problem')}" for i in check.get("issues") or []][:5]
                _write(vdir / "smoke.json", json.dumps({"ok": False, "stage": "check_spec", "issues": issues},
                                                       indent=1))
                shutil.rmtree(dest, ignore_errors=True)
                return finish("smoke_fail", json.dumps({"stage": "check_spec", "issues": issues}), edits,
                              str(vdir / "diff.patch"))
            ids = dom.smoke_ids(bundle["inc_ev"].per_task) if ctx.params.get("smoke", True) else []
            if ids:
                results = ctx.tools["run_agent"](task_ids=list(ids), adapter=str(checked)).get("results") or []
                if any(dom.halted_result(r) for r in results):
                    shutil.rmtree(dest, ignore_errors=True)
                    raise Stop("the smoke's rollouts were refused for the model's plan limit")

                def failing(rows):
                    return [r for r in rows if r.get("status") != "ok" or dom.smoke_failure(r)]
                again = [str(r.get("id")) for r in failing(results)]
                if again:
                    # a failed smoke rollout is run once more: one transient failure (a dropped connection, the
                    # model's output limit) does not refuse a candidate for good
                    rerun = ctx.tools["run_agent"](task_ids=again, adapter=str(checked)).get("results") or []
                    if any(dom.halted_result(r) for r in rerun):
                        shutil.rmtree(dest, ignore_errors=True)
                        raise Stop("the smoke's rollouts were refused for the model's plan limit")
                    rerun_ids = {str(r.get("id")) for r in rerun}
                    first = {str(r.get("id")): str(r.get("output") or r.get("status"))[:300]
                             for r in failing(results)}
                    results = [r for r in results if str(r.get("id")) not in rerun_ids] + rerun
                bad = {str(r.get("id")): str(r.get("output") or r.get("status"))[:300] for r in failing(results)}
                if len(results) < len(ids):
                    bad["missing_trials"] = f"{len(results)}/{len(ids)}"
                _write(vdir / "smoke.json", json.dumps({"ok": not bad, "stage": "smoke_run", "ids": list(ids),
                                                        "n": len(results), **({"failures": bad} if bad else {}),
                                                        **({"run_again": again, "first_failures": first}
                                                           if again else {})},
                                                       indent=1))
                if bad:
                    shutil.rmtree(dest, ignore_errors=True)
                    return finish("smoke_fail", json.dumps({"stage": "smoke_run", **bad}), edits,
                                  str(vdir / "diff.patch"))
        finally:
            shutil.rmtree(checked, ignore_errors=True)
        digest = tree_digest(dest / "adapter")
        st["candidates"][cid] = {"round": t, "variant": vid, "status": "queued", "digest": digest,
                                 "edits": [{k: e.get(k) for k in ("id", "component", "hypothesis")} for e in edits]}
        st["order"].append(cid)
        info = {"cid": cid, "edits": edits, "diff_path": str(vdir / "diff.patch"), "mechanism": prop.get("mechanism"),
                "summary": prop.get("summary"), "gate_failure": None, "detail": "",
                **({"dropped": dropped} if dropped else {})}
        _write(prep, json.dumps(info, ensure_ascii=False, indent=1))
        self._lines.append(f"r{t}{vid}: {len(edits)} edit(s) on {[e['component'] for e in edits]} -> {cid}"
                           + (f" (left out: {dropped[:5]})" if dropped else ""))
        return info

    def _seen_records(self, ctx, st: dict) -> list[dict]:
        """The learning-material records of every evaluation so far (for a domain's denylist of revealed values)."""
        out = []
        for c in st["candidates"].values():
            for e in c.get("evaluations") or []:
                tasks = ctx.workspace.learning_material / e["material"] / "tasks"
                for p in sorted(tasks.glob("*.json")) if tasks.is_dir() else []:
                    try:
                        out.append(json.loads(p.read_text()))
                    except ValueError:
                        pass
        return out

    def finalize(self, ctx) -> FinalResult:
        st = self._load(ctx)
        frontier = ctx.workspace.state / RRSI / "frontier.json"
        if not frontier.is_file():
            return FinalResult(adapter=ctx.workspace.adapter, summary="the baseline was never evaluated; "
                                                                      "current adapter")
        fr = json.loads(frontier.read_text())
        cid = fr["incumbent"]["cid"]
        source = _store(ctx) / cid / "adapter"
        intact = _real(source, ctx.workspace.state) and \
            tree_digest(source) == st["candidates"].get(cid, {}).get("digest")
        if not intact:
            return FinalResult(adapter=ctx.workspace.adapter, summary=f"H_T = {cid}, but its stored files changed "
                               "after it was stored: the current adapter is handed back instead",
                               extra={"incumbent": cid, "intact": False})
        _install(source, ctx.workspace.adapter)
        accepted = [x for x in fr["trajectory"] if x.get("cid") != "h0"]
        rounds, T = int(st.get("t") or 0), self._cfg(ctx).T
        complete = rounds >= T
        # a run the Evolver stopped after failed steps, or that ran out of epochs or budget, ends before round T
        ended = f"after {rounds} rounds" if complete else f"after {rounds} of {T} rounds: the run ended early"
        return FinalResult(adapter=ctx.workspace.adapter,
                           summary=f"H_T = {cid} {ended} (S {fr['incumbent']['S']:.4f}, S* "
                                   f"{fr['S_star']:.4f}, H_0 {fr['trajectory'][0]['S']:.4f}; "
                                   f"{len({x['cid'] for x in accepted})} accepted)",
                           extra={"incumbent": cid, "S": fr["incumbent"]["S"], "S_star": fr["S_star"],
                                  "rounds": rounds, "T": T, "complete": complete})


class _Patterns:
    """The domain as the critic sees it: its briefs, with the run's leak patterns added to its own."""

    def __init__(self, domain, patterns: list):
        self._domain, self.critic_patterns = domain, patterns

    def __getattr__(self, name):
        return getattr(self._domain, name)
