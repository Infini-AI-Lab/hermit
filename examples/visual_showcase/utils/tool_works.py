#!/usr/bin/env python3
"""Offline "tool works" check of a toolsmith checkpoint: does the learned tool return a true picture of
the page in front of it, independently of any judge score?

    python examples/visual_showcase/utils/tool_works.py --tree runs/trees-v3/waves --tag waves-ts/ckpt-step-12 \\
        --case waves --out tool_works.json [--tool NAME] [--scratch DIR] [--reference-capture DIR]

The tag is exported from the training tree (hermit.tree.Tree.export) into the scratch directory and opened
through hermit.Session with confine.run as the confined runner, the path the acting harness uses. For every
admitted tool (tool_catalog) the check seeds a fresh workspace and calls tool_call(name, {}) twice:

1. probe: reference/webgl_probe.html as index.html. Pass when at least one image comes back whose dominant colour is
   within PROBE_TOLERANCE of rgb(30,120,200), the colour the probe clears its scene to. A tool that starts Chromium
   without the GPU flags gets a white frame here (SwiftShader), so this proves the GPU path, not just "a PNG exists".
2. reference: reference/best_<case>.html (a page the judge scored 1.0; reference/SOURCES.md). Pass when at least one
   image comes back and pngstats.thumb_corr between it and the host capturer's desktop-t0.png of the same page is at
   least MATCH_MIN.

The host capture (utils/harness.capture: browser_capture.cjs under the exclusive capture lock) needs VISUAL_NODE and
VISUAL_CHROME in the host environment; when they are unset this script sets them from confine.NODE and the GPU wrapper
confine.WRAPPER. The reference capture is cached per case and page under the scratch root; --reference-capture names
an existing capture directory (with desktop-t0.png) to reuse. Scratch lives under ~/.cache/visual_showcase/tool_works,
never /tmp (the sandbox replaces /tmp). Output: {ckpt, case, tool, pass, reasons, stats}; exit code 0 when the check
passes, 1 when it fails, 2 on a usage error. Nothing here calls a model.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time

HERE = Path(__file__).resolve().parent
REFERENCE = HERE / "reference"
PROBE_PAGE = REFERENCE / "webgl_probe.html"
PROBE_RGB = (30, 120, 200)      # what webgl_probe.html clears its scene to (confine.TARGET_RGB)
PROBE_TOLERANCE = 10            # why: ANGLE and SwiftShader differ by far more than one quantisation step; white is 255
MATCH_MIN = 0.8                 # why: tolerant of headless-shell vs chrome and a few hundred ms of animation drift
CASES = ("waves", "lava", "silk")


def load_by_path(name: str, path: Path):
    """Import a sibling module by file path (the examples are not a package)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scratch_root(name: str = "tool_works") -> Path:
    """A scratch directory outside /tmp (the sandbox replaces /tmp); ~/.cache may be a symlink, so resolve it."""
    root = Path(os.path.realpath(Path.home() / ".cache" / "visual_showcase" / name))
    root.mkdir(parents=True, exist_ok=True)
    return root


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ensure_host_env() -> None:
    """utils/harness.py reads VISUAL_NODE at import and browser_capture.cjs VISUAL_CHROME at launch; default both to
    what confine resolved (node, and the wrapper that adds the GPU flags) when the caller's environment lacks them."""
    if os.environ.get("VISUAL_NODE") and os.environ.get("VISUAL_CHROME"):
        return
    confine = load_by_path("visual_confine", HERE / "confine.py")
    os.environ.setdefault("VISUAL_NODE", confine.NODE)
    os.environ.setdefault("VISUAL_CHROME", confine.WRAPPER)


def host_capture(case: str, page: Path, out_dir: Path) -> Path:
    """The host capturer's desktop-t0.png of `page` (LOCK_EX inside harness.capture); cached when it already exists."""
    out_dir = Path(out_dir)
    shot = out_dir / "capture" / "desktop-t0.png"
    if shot.is_file():
        return shot
    ensure_host_env()
    harness = load_by_path("visual_harness", HERE / "harness.py")
    workspace = out_dir / "workspace"
    seed(workspace, page)
    if (out_dir / "capture").exists():
        shutil.rmtree(out_dir / "capture")
    summary = harness.capture(workspace, out_dir / "capture", case)
    if not shot.is_file():
        raise RuntimeError(f"host capture of {page} wrote no desktop-t0.png: {summary.get('status')} "
                           f"{summary.get('capture_errors')}")
    return shot


def seed(workspace: Path, page: Path) -> Path:
    """A fresh workspace holding `page` as index.html and a placeholder README.md (what the agent's workspace holds)."""
    workspace = Path(workspace)
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    shutil.copyfile(page, workspace / "index.html")
    (workspace / "README.md").write_text("Reference page for the tool_works check; not a brief.\n")
    return workspace


def _near(rgb, target=PROBE_RGB, tolerance: int = PROBE_TOLERANCE) -> bool:
    return rgb is not None and len(rgb) == 3 and all(abs(int(a) - b) <= tolerance for a, b in zip(rgb, target))


def image_stats(workspace: Path, result: dict) -> list[dict]:
    """pngstats of every image the tool listed (paths relative to the workspace, as tool_call returns them)."""
    from hermit.runtime import pngstats
    out = []
    for rel in result.get("images") or []:
        path = Path(workspace) / rel
        out.append({"file": rel, "bytes": path.stat().st_size if path.is_file() else None, **pngstats.stats(path)})
    return out


def call_tool(adapter: Path, workspace: Path, runner, name: str) -> dict:
    """tool_call(name, {}) through a Session bound to `runner` and `workspace`, as the acting harness binds them."""
    from hermit import Session
    session = Session(Path(adapter), resources={"tool_runner": runner, "workspace": str(workspace)})
    return session.call("tool_call", {"name": name, "args": {}})


def catalog(adapter: Path) -> dict:
    from hermit import Session
    return Session(Path(adapter)).call("tool_catalog")


def _summary(result: dict) -> dict:
    keys = ("exit_code", "seconds", "timed_out", "lock_wait_s", "error", "browser_launched")
    return {k: result.get(k) for k in keys} \
        | {"text": str(result.get("text", ""))[:600], "stderr_tail": str(result.get("stderr_tail", ""))[-600:],
           "images": list(result.get("images") or []), "images_rejected": result.get("images_rejected") or []}


def probe_phase(adapter: Path, workspace: Path, runner, name: str) -> dict:
    """Phase 1: the WebGL probe; pass when one image's dominant colour is the probe's clear colour."""
    seed(workspace, PROBE_PAGE)
    result = call_tool(adapter, workspace, runner, name)
    stats = image_stats(workspace, result)
    record = {"result": _summary(result), "images": stats, "pass": False, "reasons": []}
    if result.get("error"):
        record["reasons"].append(f"tool error: {result['error']}")
    if not stats:
        record["reasons"].append("no image returned")
    elif not any(_near(s.get("dominant_rgb")) for s in stats):
        seen = [s.get("dominant_rgb") for s in stats]
        record["reasons"].append(f"dominant colour {seen} is not within {PROBE_TOLERANCE} of {list(PROBE_RGB)} "
                                 "(a white frame: the page did not load in the tool, or Chromium ran without the GPU flags)")
    record["pass"] = not record["reasons"]
    return record


def reference_phase(adapter: Path, workspace: Path, runner, name: str, case: str, reference_png: Path) -> dict:
    """Phase 2: the reference page; pass when one image correlates with the host's desktop-t0.png."""
    from hermit.runtime import pngstats
    seed(workspace, REFERENCE / f"best_{case}.html")
    result = call_tool(adapter, workspace, runner, name)
    stats = image_stats(workspace, result)
    for s in stats:
        s["thumb_corr"] = round(pngstats.thumb_corr(Path(workspace) / s["file"], reference_png), 4) \
            if s.get("error") is None else 0.0
    record = {"result": _summary(result), "images": stats, "reference_png": str(reference_png), "pass": False,
              "reasons": []}
    if result.get("error"):
        record["reasons"].append(f"tool error: {result['error']}")
    if not stats:
        record["reasons"].append("no image returned")
    else:
        best = max(s["thumb_corr"] for s in stats)
        record["best_corr"] = best
        if best < MATCH_MIN:
            record["reasons"].append(f"best correlation with the host capture {best} < {MATCH_MIN}")
    record["pass"] = not record["reasons"]
    return record


def check(adapter: Path, case: str, *, runner, reference_png: Path, scratch: Path, tools: list[str] | None = None,
          ckpt: str = "") -> dict:
    """The whole check for one exported adapter: {ckpt, case, tool, pass, reasons, stats}."""
    if case not in CASES:
        raise ValueError(f"case must be one of {CASES}, not {case!r}")
    adapter, scratch = Path(adapter), Path(scratch)
    listed = catalog(adapter)
    names = [t["name"] for t in listed.get("tools", [])]
    if tools:
        missing = [t for t in tools if t not in names]
        names = [t for t in names if t in tools]
    else:
        missing = []
    out = {"ckpt": ckpt, "case": case, "tool": None, "pass": False, "reasons": [],
           "stats": {"tools": {}, "admitted": [t["name"] for t in listed.get("tools", [])],
                     "rejected": listed.get("rejected", []), "reference_png": str(reference_png),
                     "probe_rgb": list(PROBE_RGB), "probe_tolerance": PROBE_TOLERANCE, "match_min": MATCH_MIN}}
    for name in missing:
        out["reasons"].append(f"{name}: not an admitted tool of this checkpoint")
    if not names:
        out["reasons"].append("no admitted tools" if not listed.get("tools") else "no requested tool is admitted")
        return out
    for name in names:
        started = time.monotonic()
        ws = scratch / "ws" / name
        probe = probe_phase(adapter, ws, runner, name)
        reference = reference_phase(adapter, ws, runner, name, case, reference_png)
        passed = probe["pass"] and reference["pass"]
        out["stats"]["tools"][name] = {"probe": probe, "reference": reference, "pass": passed,
                                       "seconds": round(time.monotonic() - started, 3)}
        for phase, record in (("probe", probe), ("reference", reference)):
            out["reasons"].extend(f"{name}: {phase}: {why}" for why in record["reasons"])
        if passed and out["tool"] is None:
            out["tool"] = name
    out["pass"] = out["tool"] is not None
    if out["pass"]:
        out["reasons"].append(f"{out['tool']}: probe colour and reference correlation both pass")
    return out


def export_tag(tree_dir: Path, tag: str, target: Path) -> Path:
    from hermit.tree import Tree
    return Tree(Path(tree_dir)).export(tag, Path(target))


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "export"


def main(argv: list[str] | None = None, *, runner=None, capture=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tree", required=True, type=Path, help="the training tree (trainer.tree_dir)")
    parser.add_argument("--tag", required=True, help="a checkpoint tag, e.g. waves-ts/ckpt-step-12")
    parser.add_argument("--case", required=True, choices=CASES)
    parser.add_argument("--out", required=True, type=Path, help="where the JSON verdict goes")
    parser.add_argument("--tool", action="append", help="check only this tool (repeatable); default: every admitted one")
    parser.add_argument("--scratch", type=Path, default=None,
                        help="scratch root (default ~/.cache/visual_showcase/tool_works); never under /tmp")
    parser.add_argument("--reference-capture", type=Path, default=None,
                        help="an existing host capture directory of best_<case>.html (holds desktop-t0.png)")
    args = parser.parse_args(argv)
    scratch = Path(os.path.realpath(args.scratch)) if args.scratch else scratch_root()
    if str(scratch).startswith("/tmp/") or str(scratch) == "/tmp":
        parser.error("--scratch must lie outside /tmp (the sandbox replaces /tmp)")
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        adapter = export_tag(args.tree, args.tag, scratch / "export" / _slug(args.tag))
    except Exception as exc:            # a missing tag or tree is a usage error, not a failed tool
        print(f"tool_works: cannot export {args.tag} from {args.tree}: {exc}", file=sys.stderr)
        return 2
    page = REFERENCE / f"best_{args.case}.html"
    if args.reference_capture:
        reference_png = Path(args.reference_capture) / "desktop-t0.png"
        if not reference_png.is_file():
            parser.error(f"{args.reference_capture} holds no desktop-t0.png")
    else:
        capture = capture or host_capture
        reference_png = capture(args.case, page, scratch / "reference" / f"{args.case}-{sha256(page)[:8]}")
    if runner is None:
        runner = load_by_path("visual_confine", HERE / "confine.py").run
    verdict = check(adapter, args.case, runner=runner, reference_png=reference_png, scratch=scratch / _slug(args.tag),
                    tools=args.tool, ckpt=args.tag)
    verdict["tree"], verdict["exported_to"] = str(Path(args.tree).resolve()), str(adapter)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(verdict, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"ckpt": verdict["ckpt"], "tool": verdict["tool"], "pass": verdict["pass"],
                      "reasons": verdict["reasons"]}, ensure_ascii=False))
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
