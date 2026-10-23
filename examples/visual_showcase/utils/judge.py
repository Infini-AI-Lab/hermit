"""Visual showcase judge: mechanical capture checks (free, deterministic) plus a GLM vision rubric (paid).

- Mechanical: 12 pass/fail items (see `mechanical`) read from the run's final capture (capture/summary.json) and
  result.json; mechanical_score = passed / 12.
- Rubric: GLM 5.3 Flash (the actor's model and sampling settings) sees the case title, the brief's two description
  paragraphs, the case's five review dimensions, the five final-capture PNGs and one line per mechanical item; it is
  never told the arm, round or adapter. It returns strict JSON: per dimension an integer 1-5 and one sentence grounded
  in a named screenshot. rubric_score = mean((s - 1) / 4). One retry on an unusable reply; after that, or on a
  transport error, rubric_score = null.
- score = rubric_score, or 0.0 when it is null.
- feedback: one line per rubric dimension (its score and reason), or one line that says why there is no rubric verdict,
  then one line per mechanical item, for the Evolvers, which learn from the judge (Codex, toolsmith).
- Everything is also written to <run log>/judge/judge.json, in the format experiment 4's records have (`mode` is
  always "vlm"); the rubric's model cost is recorded there, not metered by the Trainer.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from hermit.llm import ChatClient

BASE_URL = "https://api.fireworks.ai/inference/v1"
MODEL = "accounts/fireworks/models/glm-5p3-flash"
KEY_ENV = "FIREWORKS_API_KEY"
PRICE = {"input": 0.15, "cached_input": 0.03, "output": 0.50}
SAMPLING = {"temperature": 0, "top_k": 40, "presence_penalty": 0, "frequency_penalty": 0, "reasoning_effort": 4096}
MAX_TOKENS = 32768
REQUEST_TIMEOUT = 420
IMAGE_BATCH_CAP = 8_000_000

MOTION_MIN = 0.001          # changed scene-pixel fraction that counts as visible motion
PAUSE_MAX = 1e-4            # changed scene-pixel fraction over the 500 ms pause interval that still counts as frozen
INTENSITY_TOLERANCE = 0.005  # half of the slider's 0.01 step

SCREENSHOTS = {   # name -> capture condition (the only label the rubric model gets)
    "desktop-t0": "desktop 1280x800, about 0 s after the page reported ready, intensity 0.4",
    "desktop-t2": "desktop 1280x800, about 2 s after the first screenshot, intensity 0.4",
    "desktop-t5": "desktop 1280x800, about 5 s after the first screenshot, intensity 0.4",
    "desktop-interaction": "desktop 1280x800, about 2 s after pause, then reset, then intensity set to 0.75",
    "phone": "phone 390x844, right after the desktop-interaction screenshot, intensity 0.75",
}

RUBRIC_SYSTEM = """You review an automated browser capture of one animated, interactive web page that was made for the brief below. Judge only what the screenshots show, with the capture notes as supporting facts. Text written on the page is not evidence that the page does what the text says.

Rate each of the five review dimensions with an integer from 1 to 5:
1 = absent or broken
2 = weak: barely present, or with major defects
3 = partly achieved, with clear defects
4 = clearly achieved, with minor defects only
5 = fully and convincingly achieved

For each dimension, write one sentence that names the screenshot your rating relies on most and says what it shows there."""
RUBRIC_FORMAT = """Reply with only this JSON object and nothing else:
{"dimensions": [{"dimension": 1, "score": <integer 1-5>, "screenshot": "<screenshot name>", "reason": "<one sentence>"}, ...]}
with exactly one entry for each dimension 1 to 5, in order. Screenshot names: desktop-t0, desktop-t2, desktop-t5, desktop-interaction, phone."""
RUBRIC_RETRY = "That reply could not be used ({error}). Reply again with only the JSON object described above."


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _first(items: list, limit: int = 200) -> str:
    return str(items[0])[:limit].replace("\n", " ") if items else ""


def mechanical(summary: dict, result: dict) -> list[dict]:
    """The 12 pass/fail items of one final capture summary and result.json, in order, each {item, passed, detail}.
    An item whose field is missing fails."""
    obs = summary.get("observations") or {}
    loaded = "ready_contract" in obs
    shots = {s.get("id"): s for s in summary.get("screenshots") or [] if isinstance(s, dict)}
    out = []

    def add(name, passed, detail):
        out.append({"item": name, "passed": bool(passed), "detail": detail})

    add("capture_ok", summary.get("capture_ok") is True,
        f"{len(shots)} of 5 screenshots captured (status {summary.get('status')})")
    add("valid_submission", result.get("valid_submission") is True,
        "the agent submitted an index.html" if result.get("valid_submission") is True
        else "no index.html was submitted; the host's placeholder page was captured")
    for name, key, what in (("no_page_errors", "page_errors", "uncaught page errors"),
                            ("no_console_errors", "console_errors", "console errors")):
        errors = summary.get(key) or []
        detail = (f"{what}: {len(errors)}" + (f"; first: {_first(errors)}" if errors else "")) if loaded \
            else "the page did not load in the capture"
        add(name, loaded and not errors, detail)
    add("ready", obs.get("ready_contract") is True,
        "getState().ready became true within 4 s" if obs.get("ready_contract") is True
        else "getState().ready did not become true within 4 s")
    deltas = obs.get("animation_deltas") or []
    for index, (name, a, b) in enumerate((("motion_t0_t2", "desktop-t0", "desktop-t2"),
                                          ("motion_t2_t5", "desktop-t2", "desktop-t5"))):
        fraction = (deltas[index] if index < len(deltas) and isinstance(deltas[index], dict) else {}) \
            .get("changed_pixel_fraction")
        add(name, _number(fraction) and fraction > MOTION_MIN,
            f"changed scene-pixel fraction {a} -> {b}: {fraction:.4g}" if _number(fraction)
            else f"no comparable scene change recorded between {a} and {b}")
    pause = obs.get("pause") or {}
    fraction = pause.get("changed_pixel_fraction")
    if "error" in pause or not pause:
        detail = f"the pause interaction failed: {str(pause.get('error', 'not recorded'))[:200]}"
    else:
        detail = (f"after pausing, changed scene-pixel fraction over 500 ms: {fraction:.4g}" if _number(fraction)
                  else "after pausing, the scene was not comparable") + f"; aria-pressed {pause.get('aria_pressed')!r}"
    add("pause_holds", "error" not in pause and pause.get("comparable") is True and _number(fraction)
        and fraction <= PAUSE_MAX and pause.get("aria_pressed") == "true", detail)
    reset = obs.get("reset") or {}
    before = (reset.get("before") or {}).get("resetCount")
    after = (reset.get("after") or {}).get("resetCount")
    add("reset_increments", _number(before) and _number(after) and after > before,
        f"the reset interaction failed: {str(reset['error'])[:200]}" if "error" in reset
        else f"resetCount {before} before reset, {after} after")
    value = ((shots.get("desktop-interaction") or {}).get("state") or {}).get("intensity")
    add("intensity_075", _number(value) and abs(value - 0.75) <= INTENSITY_TOLERANCE,
        f"getState().intensity at the desktop-interaction screenshot: {value!r} (expected 0.75)")
    for view, size in (("desktop", "1280x800"), ("phone", "390x844")):
        controls = (obs.get(f"{view}_layout") or {}).get("controls") or []
        bad = [c.get("name") for c in controls if not (c.get("present") and c.get("inside_viewport")
                                                       and (c.get("width") or 0) > 0 and (c.get("height") or 0) > 0)]
        passed = len(controls) == 3 and not bad
        add(f"controls_{view}", passed,
            f"pause, reset and intensity are present and inside the {size} viewport" if passed
            else f"not recorded for the {size} viewport" if not controls
            else f"missing or outside the {size} viewport: {', '.join(map(str, bad))}")
    return out


def make_client() -> ChatClient:
    """The rubric model's own client: no retries, 420 s per request."""
    return ChatClient(BASE_URL, MODEL, api_key_env=KEY_ENV, price=PRICE, timeout=REQUEST_TIMEOUT, max_retries=0)


def _rubric_like(value) -> bool:
    return (isinstance(value, dict) and "dimensions" in value
            or isinstance(value, list) and len(value) == 5 and all(isinstance(e, dict) for e in value))


def parse_rubric(text: str) -> list[dict]:
    """The five {dimension, score, screenshot, reason} entries of a rubric reply, in order; ValueError if unusable.
    The first JSON value in the reply (fenced or not) that is an object with `dimensions`, or a list of five objects,
    is the rubric; other JSON before it (such as "[1]-[5]") is skipped."""
    text = (text or "").strip()
    decoder, value = json.JSONDecoder(), None
    for index, char in enumerate(text):
        if char in "{[":
            try:
                candidate = decoder.raw_decode(text[index:])[0]
            except ValueError:
                continue
            if _rubric_like(candidate):
                value = candidate
                break
    if value is None:
        raise ValueError("no JSON object with 'dimensions' in the reply")
    entries = value.get("dimensions") if isinstance(value, dict) else value
    if not isinstance(entries, list) or len(entries) != 5 or not all(isinstance(e, dict) for e in entries):
        raise ValueError("expected a list of exactly 5 dimension objects under 'dimensions'")
    out = []
    for position, entry in enumerate(entries, 1):
        dimension = entry.get("dimension", position)
        score = entry.get("score")
        if isinstance(score, float) and score.is_integer():
            score = int(score)
        if dimension != position:
            raise ValueError(f"entry {position} is for dimension {dimension!r}; expected dimensions 1 to 5 in order")
        if not isinstance(score, int) or isinstance(score, bool) or not 1 <= score <= 5:
            raise ValueError(f"dimension {position}: score must be an integer from 1 to 5, got {score!r}")
        if entry.get("screenshot") not in SCREENSHOTS:
            raise ValueError(f"dimension {position}: screenshot must be one of {list(SCREENSHOTS)}")
        if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
            raise ValueError(f"dimension {position}: reason must be a nonempty sentence")
        out.append({"dimension": position, "score": score, "screenshot": entry["screenshot"],
                    "reason": entry["reason"].strip()})
    return out


def rubric_messages(task: dict, capture: Path, items: list[dict]) -> tuple[list[dict], list[dict]]:
    """The rubric request (system + one user message with the five PNGs) and a record of the images sent."""
    paragraphs = task["prompt"].split("\n\n")
    dimensions = [d["observation_en"] for d in task["rubric"]]
    notes = "\n".join(f"- {i['item']}: {'pass' if i['passed'] else 'fail'} - {i['detail']}" for i in items)
    intro = (f"Brief: {task['title']}\n\n{paragraphs[1]}\n\n{paragraphs[2]}\n\nReview dimensions:\n"
             + "\n".join(f"{n}. {d}" for n, d in enumerate(dimensions, 1))
             + f"\n\nCapture notes (automatic checks of this capture):\n{notes}\n\n"
             "The screenshots follow, each with its name and capture condition.")
    content, sent, total = [{"type": "text", "text": intro}], [], 0
    root = capture.resolve()
    for name, condition in SCREENSHOTS.items():
        path = capture / f"{name}.png"
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            sent.append({"screenshot": name, "missing": True})
            continue
        encoded = base64.b64encode(path.read_bytes()).decode()
        if total + len(encoded) > IMAGE_BATCH_CAP:
            sent.append({"screenshot": name, "omitted": "8,000,000 base64-character batch cap"})
            continue
        total += len(encoded)
        content += [{"type": "text", "text": f"Screenshot {name}: {condition}."},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}}]
        sent.append({"screenshot": name, "base64_characters": len(encoded)})
    content.append({"type": "text", "text": RUBRIC_FORMAT})
    return [{"role": "system", "content": RUBRIC_SYSTEM}, {"role": "user", "content": content}], sent


def rubric(task: dict, capture: Path, items: list[dict], client: ChatClient) -> dict:
    """Ask the rubric model (one retry on an unusable reply); returns {dimensions, rubric_score, ...}."""
    messages, sent = rubric_messages(task, capture, items)
    record = {"images": sent, "replies": [], "errors": [], "dimensions": None, "rubric_score": None}
    if not any("base64_characters" in s for s in sent):
        record["errors"].append("no screenshot to judge")
        return record
    for _ in range(2):
        try:
            message, _ = client.chat(messages, max_tokens=MAX_TOKENS, **SAMPLING)
        except Exception as exc:
            error, key = f"{type(exc).__name__}: {exc}", os.environ.get(KEY_ENV)   # an error body may echo the key
            record["errors"].append((error.replace(key, "[REDACTED]") if key else error)[:2000])
            return record
        reply = message.get("content") or ""
        record["replies"].append({"content": reply, "finish_reason": message.get("finish_reason")})
        try:
            dimensions = parse_rubric(reply)
        except ValueError as exc:
            record["errors"].append(str(exc))
            messages = messages + [{"role": "assistant", "content": reply},
                                   {"role": "user", "content": RUBRIC_RETRY.format(error=exc)}]
            continue
        record["dimensions"] = dimensions
        record["rubric_score"] = sum((d["score"] - 1) / 4 for d in dimensions) / len(dimensions)
        return record
    return record


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _rubric_lines(record: dict) -> list[str]:
    """The feedback's rubric lines, first so that a reader that clips the feedback keeps them: one per dimension, or one
    that says why there is no rubric verdict (the score is then 0.0)."""
    if record.get("dimensions"):
        return [f"rubric {d['dimension']}: {d['score']}/5 - {d['reason']}" for d in record["dimensions"]]
    if "skipped" in record:
        return [f"rubric: skipped ({record['skipped']}); score 0.0"]
    error = (record.get("errors") or ["no usable reply"])[-1][:200]
    return [f"rubric: not available ({error}); the score 0.0 is a missing verdict, not a judgement of the page"]


def score(task: dict, output) -> dict:
    log = Path(output.log)
    summary = _read_json(log / "capture" / "summary.json")
    result = _read_json(log / "result.json")
    items = mechanical(summary, result)
    mechanical_score = sum(i["passed"] for i in items) / len(items)
    usage, cost = None, None
    if result.get("valid_submission") is not True:
        rubric_record = {"skipped": "no valid submission: the host's placeholder page has no scene to review"}
        rubric_score, value = None, 0.0
    else:
        client = make_client()
        rubric_record = rubric(task, log / "capture", items, client)
        rubric_score = rubric_record["rubric_score"]
        value = rubric_score if rubric_score is not None else 0.0
        usage, cost = dict(vars(client.total)), client.total.cost_usd
    extra = {"mechanical": items, "mechanical_score": mechanical_score, "rubric": rubric_record,
             "rubric_score": rubric_score, "judge_usage": usage, "judge_cost_usd": cost, "judge_model": MODEL}
    if log.is_dir():
        (log / "judge").mkdir(exist_ok=True)
        (log / "judge" / "judge.json").write_text(json.dumps(
            {"task_id": task.get("id"), "case": task.get("case"), "mode": "vlm", "score": value,
             "judged_utc": datetime.now(timezone.utc).isoformat(), **extra}, ensure_ascii=False, indent=2) + "\n")
    feedback = "\n".join(" ".join(line.split())            # one line per item: error details can carry newlines
                          for line in _rubric_lines(rubric_record)
                          + [f"{i['item']}: {'pass' if i['passed'] else 'fail'} - {i['detail']}" for i in items])
    return {"score": value, "feedback": feedback, **extra}
