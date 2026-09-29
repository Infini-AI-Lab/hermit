"""Read-only Chinese progress/results report; no model calls and no gold reads."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from html import escape
import json
import os
import re
from pathlib import Path
import tempfile
from urllib.parse import quote

import data

ROOT = Path(__file__).resolve().parent
MODEL = "accounts/fireworks/models/glm-5p3-flash"
ARM_NAMES = {"baseline": "GLM + Codex", "adapted": "GLM + Codex + evolved adapter"}


def esc(value):
    return escape(str(value), quote=True)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def number(value, decimals=0):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    return f"{value:,.{decimals}f}"


def percent(value):
    return f"{100 * value:.1f}%" if isinstance(value, (int, float)) else "未评分"


def status_label(value):
    labels = {"not_started": "尚未开始", "pending": "等待运行", "preparing": "准备中", "preflight": "预检中",
              "running": "运行中", "completed": "已结束", "complete": "已完成", "finished": "已结束",
              "submitted": "已提交", "failed": "失败", "timeout": "超时", "api_error": "API 错误",
              "budget_exhausted": "预算耗尽", "admitted": "已自动接纳", "rejected": "未接纳", "blocked": "受阻",
              "stopped_for_infrastructure_revision": "已中止 · 未评分"}
    return labels.get(str(value), str(value or "等待运行"))


def safe_path(root, value):
    if not isinstance(value, (str, Path)) or not str(value):
        return None
    path = Path(value)
    candidates = [path] if path.is_absolute() else [root / path, root.parent / path]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_relative_to(root) and resolved.exists():
            relative = resolved.relative_to(root)
            if "private" not in relative.parts and "private-evaluation" not in relative.parts:
                return resolved
    return None


def link(root, output, value, label):
    path = safe_path(root, value)
    if path is None:
        return ""
    url = quote(os.path.relpath(path, output.parent), safe="/")
    return f'<a href="{esc(url)}" target="_blank" rel="noopener">{esc(label)} ↗</a>'


def _records_links(root, output, row):
    values = row.get("records", [])
    if isinstance(values, dict):
        pairs = [(key, val) for key, val in values.items() if isinstance(val, (str, Path))]
    elif isinstance(values, list):
        pairs = [("记录 " + str(index + 1), val) for index, val in enumerate(values) if isinstance(val, (str, Path))]
    elif isinstance(values, str):
        pairs = [("原始记录", values)]
    else:
        pairs = []
    for key in ("result_path", "output_directory", "output"):
        if row.get(key):
            pairs.append(("运行记录", row[key]))
    links, seen = [], set()
    for name, value in pairs:
        path = safe_path(root, value)
        if path is None:
            continue
        if path.is_dir() and (path / "result.json").is_file():
            path = path / "result.json"
        if path in seen:
            continue
        seen.add(path)
        rendered = link(root, output, path, name)
        if rendered:
            links.append(rendered)
    return '<div class="record-links">' + " · ".join(links) + "</div>" if links else ""


def _usage(rows):
    total = {"input": 0, "output": 0, "cached": 0}
    available = False
    for row in rows:
        usage = row.get("usage")
        if not isinstance(usage, dict):
            continue
        for name, aliases in (("input", ("input_tokens", "prompt_tokens")),
                              ("output", ("output_tokens", "completion_tokens")),
                              ("cached", ("cached_tokens", "cached_input_tokens"))):
            for key in aliases:
                if isinstance(usage.get(key), (int, float)):
                    total[name] += usage[key]
                    available = True
                    break
        if not any(key in usage for key in ("cached_tokens", "cached_input_tokens")):
            details = usage.get("input_tokens_details", usage.get("prompt_tokens_details", {}))
            if isinstance(details, dict) and isinstance(details.get("cached_tokens"), (int, float)):
                total["cached"] += details["cached_tokens"]
    return total if available else None


def _failure_reason(root, row):
    """Display saved termination metadata only; never alter outcomes or learner inputs."""
    if row.get("status") == "completed":
        return None
    records = safe_path(root, row.get("records"))
    if not records or not records.is_dir() or not records.is_relative_to(root / "runs") or records.name != "records":
        return {"code": "unspecified", "label": "未完成", "detail": "当前记录未提供可核对的终止原因。"}
    base = records.parent
    summary = read_json(base / "api/summary.json") or {}
    limits = summary.get("limits") or {}
    calls = sorted(path for path in (base / "api").glob("*") if path.is_dir() and path.name.isdigit())
    # The latest response/call is authoritative; earlier incomplete replies may have continued.
    latest = calls[-1] if calls else None
    response = read_json(latest / "response.json") if latest else None
    transport = read_json(latest / "transport.json") if latest else None
    response = response if isinstance(response, dict) else {}
    transport = transport if isinstance(transport, dict) else {}
    native_errors = []
    events = records / "events.jsonl"
    if events.is_file() and not events.is_symlink():
        try:
            for line in events.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "turn.failed":
                    native_errors.append(str((event.get("error") or {}).get("message", "")))
                elif event.get("type") == "error":
                    native_errors.append(str(event.get("message", "")))
        except OSError:
            pass
    incomplete = response.get("incomplete_details") or {}
    if (response.get("status") == "incomplete" and isinstance(incomplete, dict) and
            incomplete.get("reason") == "max_output_tokens"):
        request = read_json(latest / "request.json") or {}
        cap = request.get("max_output_tokens", limits.get("per_response"))
        cap_text = number(cap) if isinstance(cap, int) else "记录中的"
        corroborated = any("max_output_tokens" in value for value in native_errors)
        return {"code": "single_response_limit", "label": "单次输出达到上限",
                "detail": f"Fireworks 返回 incomplete / max_output_tokens，本次请求的输出上限为 {cap_text} tokens。"
                          + ("Codex 的 turn.failed 记录也包含相同原因。" if corroborated else "")
                          + "这是输出长度截断，不能按网络掉线解释。",
                "evidence": latest / "response.json"}
    if summary.get("failure_kind") == "budget_exhausted":
        if isinstance(limits.get("requests"), int) and summary.get("calls", 0) >= limits["requests"]:
            return {"code": "request_budget", "label": "请求次数预算耗尽",
                    "detail": f"达到每题 {limits['requests']} 次逻辑模型请求上限。", "evidence": base / "api/summary.json"}
        if (isinstance(limits.get("completion_tokens"), int) and
                (summary.get("usage") or {}).get("output_tokens", 0) >= limits["completion_tokens"]):
            return {"code": "total_output_budget", "label": "总输出预算耗尽",
                    "detail": f"达到每题累计 {number(limits['completion_tokens'])} 个输出 tokens 上限。", "evidence": base / "api/summary.json"}
        return {"code": "model_budget", "label": "模型调用预算耗尽",
                "detail": "中继记录为 budget_exhausted；没有足够证据进一步区分具体上限。", "evidence": base / "api/summary.json"}
    if row.get("status") == "timeout":
        return {"code": "timeout", "label": "本题运行超时",
                "detail": "记录状态为 timeout；时限包含排队与限流等待。", "evidence": records / "result.json"}
    if summary.get("transport_failed"):
        attempts = transport.get("attempts") or []
        if attempts and attempts[-1].get("status") == "rate_refused":
            return {"code": "provider_rate_limit", "label": "服务端限流未恢复",
                    "detail": "最后一次网络尝试明确被服务端限流拒绝。", "evidence": latest / "transport.json"}
        return {"code": "transport_failure", "label": "模型传输失败",
                "detail": "中继将本题标记为 transport_failed；此字段不能单独证明是本地网络问题。",
                "evidence": base / "api/summary.json"}
    if native_errors:
        return {"code": "native_turn_failure", "label": "Codex 本轮未完成",
                "detail": "原始 Codex 事件记录了错误，未找到足以归入上述类型的终止信息。", "evidence": events}
    return {"code": "no_valid_final", "label": "未形成有效最终答案",
            "detail": "此题已结束但没有可采纳的最终提交；报告不推测未记录的原因。",
            "evidence": records / "result.json"}


def _feedback(root, row):
    """Read only explicit GLM feedback files, never arbitrary learner artifacts."""
    merged = dict(row)
    out = safe_path(root, row.get("output"))
    if out:
        result_path = out / "result.json" if out.is_dir() else out
        more = read_json(result_path)
        if isinstance(more, dict):
            merged = {**more, **row}
    options = [merged.get("feedback_path")]
    public = merged.get("public_feedback")
    if isinstance(public, dict):
        options.append(public.get("path"))
    candidate = safe_path(root, merged.get("candidate"))
    if candidate:
        options.extend([candidate.parent / "scratch/feedback.json", candidate.parent / "scratch/feedback.md"])
    for value in options:
        path = safe_path(root, value)
        if path and path.name in ("feedback.json", "feedback.md") and path.is_file() and path.stat().st_size <= 200_000:
            return merged, path, path.read_text(encoding="utf-8")
    return merged, None, None


def _lineage(root, learning):
    """Label saved paths, using actual native invocations rather than planned W numbers."""
    def canonical(value):
        return str(Path(value).resolve()) if isinstance(value, str) and value else None
    labels = {str(root.parent / "MetaHarnessAdapter/harnessadapter/architectures/mwm"): "W0"}
    for row in sorted(learning, key=lambda item: item.get("block", 0)):
        if row.get("activated") is True and safe_path(root, row.get("candidate")):
            labels[canonical(row["candidate"])] = f'W{row["block"]}'
    transitions = {}
    for row in learning:
        source_path, active_path = canonical(row.get("source")), canonical(row.get("candidate"))
        source = labels.get(source_path, "来源版本待核对")
        active = labels.get(active_path, "保留版本待核对")
        if row.get("activated") is True:
            transitions[row["block"]] = f"来源 {source} → 接纳 {active}"
        elif source_path and active_path == source_path:
            transitions[row["block"]] = f"本轮未接纳；继续使用 {active}"
        else:
            transitions[row["block"]] = "本轮未接纳；保留版本记录待核对"
    per_task, per_batch = {}, {}
    for ordinal in range(1, 71):
        path = safe_path(root, root / "runs/adapted" / f"{ordinal:03}" / "records/invocation.json")
        invocation = read_json(path) if path else None
        if isinstance(invocation, dict):
            per_task[ordinal] = labels.get(canonical(invocation.get("adapter")), "版本待核对")
    for block in range(1, 15):
        versions = {per_task[ordinal] for ordinal in range((block-1)*5+1, block*5+1) if ordinal in per_task}
        per_batch[block] = ("实际使用 " + next(iter(versions))) if len(versions) == 1 else (
            "已记录版本不一致，待核对" if versions else "实际版本：待运行")
    return per_task, per_batch, transitions


def _learning_cost(rows):
    def label(selected):
        usage = _usage(selected)
        if not usage:
            return "尚无完成记录，不按 0 计算"
        unknown = sum(row.get("usage_complete") is False for row in selected)
        text = f'输入 {number(usage["input"])} / 输出 {number(usage["output"])} tokens'
        if unknown:
            text += f"；{unknown} 轮含未知用量，仅为已知下界"
        return text
    during = [row for row in rows if isinstance(row.get("block"), int) and 1 <= row["block"] <= 13]
    after = [row for row in rows if row.get("block") == 14]
    return ('<div class="notice"><b>额外学习用量（不含两组答题用量）</b>'
            f'<p><b>第 1–13 轮，面向后续测试题：</b>{label(during)}</p>'
            f'<p><b>第 14 轮，Q70 后且不影响本次成绩：</b>{label(after)}</p>'
            f'<p><b>全部学习合计：</b>{label(rows)}</p>'
            '<p class="small">失败或未接纳的学习也计入成本。第 1–13 轮有机会影响后续题，不等于每轮都成功接纳或提高成绩。以上只汇总已返回用量，未知用量不是 0。</p></div>')


def _preflight(root, output):
    api = read_json(root / "preflight/api-responses.json")
    api_response = api.get("response", {}) if isinstance(api, dict) else {}
    api_good = (isinstance(api, dict) and api.get("http_status") == 200 and
                api_response.get("model") == MODEL and api_response.get("status") == "completed")
    if api_good:
        api_text = "已记录 HTTP 200，返回模型与 GLM-5.3-Flash 一致，Responses 请求完成。这里只证明 API 联通。"
        api_state = "已验证"
    elif api is not None:
        api_text, api_state = "已发现 API 记录；尚不满足 HTTP 200 + 指定模型 + completed 的完整检查。", "待检查"
    else:
        api_text, api_state = "尚无成功 API 预检记录。", "待验证"
    api_card = f'<div class="check"><span class="pill {"good" if api_good else "muted"}">{api_state}</span><h3>Fireworks 模型连接</h3><p>{api_text}</p>{link(root, output, "preflight/api-responses.json", "原始 API 预检")}</div>'
    probes = []
    directory = root / "preflight"
    if directory.is_dir():
        candidates = [*directory.glob("*.json"), *directory.glob("*/verification.json")]
        for path in sorted(set(candidates)):
            if path.name == "api-responses.json":
                continue
            if not any(word in path.name.lower() for word in ("sandbox", "isolation", "security", "verification", "test", "probe", "preflight", "readiness")):
                continue
            obj = read_json(path)
            if not isinstance(obj, dict):
                continue
            passed = obj.get("passed") if isinstance(obj.get("passed"), bool) else None
            items = obj.get("checks", obj.get("tests"))
            if passed is None and isinstance(items, list) and items:
                flags = [item.get("passed", item.get("ok")) for item in items if isinstance(item, dict)]
                if len(flags) == len(items) and all(isinstance(flag, bool) for flag in flags):
                    passed = all(flags)
            state = "通过" if passed is True else "未通过" if passed is False else "已记录，未汇总"
            probes.append(f'<li><span class="pill {"good" if passed else "muted"}">{state}</span> {link(root, output, path, path.stem)}</li>')
    sandbox = ('<ul class="probe-list">' + "".join(probes) + "</ul>") if probes else '<p class="notice">尚未发现可展示的沙箱验证记录；下方是要求，不代表已经实测通过。</p>'
    audit_entries = []
    for name, label in (("partial-audit.json", "阶段审计快照"), ("final-audit.json", "最终审计")):
        path = directory / name
        obj = read_json(path)
        if not isinstance(obj, dict):
            continue
        counts = obj.get("counts") or {}
        failed, pending = counts.get("failed"), counts.get("pending")
        complete = obj.get("complete_campaign_audited") is True
        badge = "最终检查通过" if complete and obj.get("passed") is True else (
            "部分检查通过" if obj.get("passed") is True else "存在未通过检查")
        audit_entries.append(f'<div><span class="pill {"good" if obj.get("passed") else "bad"}">{badge}</span>'
                             f'<p>{link(root, output, path, label)}：{number(counts.get("passed"))} 项通过，'
                             f'{number(failed)} 项失败，{number(pending)} 项待完成。'
                             f'<br><span class="small">记录时间：{esc(obj.get("created_utc", "未记录"))}</span></p></div>')
    audit_html = ('<div class="check"><h3>冻结文件、GLM 来源与封存审计</h3>' +
                  ("".join(audit_entries) if audit_entries else '<p>尚无审计快照；不提前声称通过。</p>') +
                  '<p class="small">审计核对保存的文件、原生调用、版本继承、GLM 写入来源以及评分前封存。阶段审计只覆盖当时已结束记录，待完成不是通过；此检查不重新判答案，也不是独立网络取证、反馈质量认证或无预训练污染证明。</p></div>')
    return api_card + '<div class="check"><h3>沙箱与隔离测试</h3>' + sandbox + '<p class="small">测试通过只能说明已覆盖的检查通过，不是对所有潜在行为的独立安全证明。</p></div>' + audit_html


def _model_audit(root):
    groups = []
    for label, pattern in (("接口试跑", "preflight/*/*/api/*/transport.json"),
                           ("正式答题", "runs/*/*/api/*/transport.json")):
        rows = [read_json(path) for path in sorted(root.glob(pattern))]
        rows = [row for row in rows if isinstance(row, dict)]
        matched = sum(row.get("model") == MODEL and row.get("response_model") == MODEL for row in rows)
        mismatched = sum(bool(row.get("response_model")) and row.get("response_model") != MODEL for row in rows)
        unknown = sum(not row.get("response_model") for row in rows)
        if not rows:
            detail = "尚无模型身份审计记录。"
        elif matched == len(rows):
            detail = f"已记录的 {matched} 次请求及返回模型全部匹配 GLM-5.3-Flash。"
        else:
            detail = f"{len(rows)} 次已记录传输：{matched} 次请求／返回匹配，{mismatched} 次返回不匹配，{unknown} 次未记录返回模型。"
        groups.append(f'<p><b>{label}：</b>{detail}</p>')
    calls = [read_json(path) for path in sorted(root.glob("evolution/*/sessions/*/control/fireworks-calls/*.json"))]
    calls = [call for call in calls if isinstance(call, dict)]
    matched = sum(call.get("requested_model") == MODEL and call.get("meta", {}).get("response_model") == MODEL for call in calls)
    unknown = sum(not call.get("meta", {}).get("response_model") for call in calls)
    mismatched = sum(bool(call.get("meta", {}).get("response_model")) and call["meta"]["response_model"] != MODEL for call in calls)
    groups.append(f'<p><b>GLM learner：</b>{len(calls)} 次已保存请求，{matched} 次请求／返回均匹配，{mismatched} 次返回不匹配，{unknown} 次返回模型未知。只有模型来源检查通过的候选才可接纳。</p>')
    return '<div class="model-audit"><h3>模型身份按 Fireworks 返回记录核对</h3>' + ''.join(groups) + '<p class="small">若 Codex 提示 model metadata fallback，指本地模型参数说明采用通用配置；这个提示本身不表示替换模型。实际调用身份以上述请求与返回记录为依据。</p></div>'


def _version(root):
    match = re.fullmatch(r"officeqa-codex70(?:-v(\d+))?-work", Path(root).name)
    return "v" + (match.group(1) or "1") if match else "当前版本"


def _restart_appendix(root, output):
    """Walk restart receipts, reading only historical stop/usage metadata."""
    receipt = read_json(root / "restart-provenance.json")
    if not isinstance(receipt, dict):
        return "", ""
    version = _version(root)
    history, seen = [], {root}
    next_receipt = receipt
    for _ in range(10):
        raw = next_receipt.get("previous_root")
        if not isinstance(raw, str):
            break
        previous = Path(raw)
        if (previous.parent != root.parent or previous.is_symlink() or previous in seen or
                not re.fullmatch(r"officeqa-codex70(?:-v\d+)?-work", previous.name)):
            break
        seen.add(previous)
        stop = read_json(previous / "infrastructure-stop.json") or {}
        history.append((previous, stop, next_receipt))
        next_receipt = read_json(previous / "restart-provenance.json")
        if not isinstance(next_receipt, dict):
            break
    def prior_link(path, label):
        if not path.is_file() or path.is_symlink():
            return ""
        return f'<a href="{esc(quote(os.path.relpath(path, output.parent), safe="/"))}" target="_blank" rel="noopener">{esc(label)} ↗</a>'
    sections = []
    for previous, stop, restart in reversed(history):
        prior_version = _version(previous)
        rows = []
        for arm in ARM_NAMES:
            summaries = [read_json(path) for path in sorted((previous / "runs" / arm).glob("*/api/summary.json"))]
            summaries = [row for row in summaries if isinstance(row, dict)]
            unknown = sum(len(set(row.get("usage_unknown_calls", [])) | set(row.get("in_flight_calls", []))) for row in summaries)
            rows.append((ARM_NAMES[arm], len(summaries), sum(row.get("calls", 0) for row in summaries), _usage(summaries), str(unknown)))
        learner = [read_json(path) for path in sorted((previous / "evolution").glob("*/result.json"))]
        learner = [row for row in learner if isinstance(row, dict)]
        unknown_learner = sum(len(row.get("usage_unknown_calls", [])) if "usage_unknown_calls" in row else row.get("model_calls", 0) if not row.get("response_models") else 0 for row in learner)
        rows.append(("旧版 GLM learner", len(learner), sum(row.get("model_calls", 0) for row in learner), _usage(learner), f"至少 {unknown_learner}"))
        entries = []
        for name, count, calls, usage, unknown in rows:
            values = (f'{number(usage["input"])} / {number(usage["output"])}' if usage else "无已知用量")
            entries.append(f'<tr><th scope="row">{esc(name)}</th><td>{count} 份 / {calls} 次</td><td>{values}</td><td>{esc(unknown)}</td></tr>')
        reason = str(stop.get("reason", "尚无停止原因记录"))
        if "text.format" in reason:
            explanation = "把 Responses 的文本格式配置误识别为已生成文字，导致本可重试的生成前限流没有重试；中止后只修正传输分类。"
        elif "exhausted48" in reason:
            explanation = "Fireworks 限流，首轮 learner 在 48 次请求后仍未正常完成；没有新版本被接纳。"
        else:
            explanation = "停止原因保留如下；报告没有根据旧答案或旧成绩筛选重启。"
        evidence = " ".join((prior_link(previous / "infrastructure-stop.json", prior_version + " 中止记录"), prior_link(output.parent / f"officeqa-pro70-interrupted-{prior_version}.html", prior_version + " 归档快照")))
        confirmed = stop.get("graded") is False and restart.get("grades_computed") is False
        sections.append(f'<article><h3>{esc(prior_version)} · {"已中止、未评分" if confirmed else "停止／评分状态待核实"}</h3><p>{explanation}</p><details><summary>原始停止原因</summary><p class="small">{esc(reason)}</p></details><div class="table-wrap"><table><thead><tr><th>旧版环节</th><th>汇总记录 / API 调用</th><th>已知输入 / 输出 tokens</th><th>用量未知的调用</th></tr></thead><tbody>{"".join(entries)}</tbody></table></div><div class="resource-links">{evidence}</div></article>')
    previous_labels = "、".join(_version(item[0]) for item in reversed(history)) or "历史版本"
    banner = f'<p class="notice"><b>本页是重新开始的 {esc(version)}。</b>{esc(previous_labels)} 因基础设施问题中止，均未评分。本次仍用相同的 70 题、顺序和文档库，从通用 W0 开始；旧答案、旧反馈和旧 adapter 不进入当前运行。</p>'
    appendix = f'<section id="restart" class="panel"><div class="eyebrow">SEPARATE INFRASTRUCTURE HISTORY</div><h2>历史中断：分别保存原因与已知成本</h2>{"".join(sections)}<p class="small">这里只读取各旧版 API 用量、learner 汇总和停止记录，不读取旧答案或建议。中止时未形成汇总的请求可能不在表内；已知用量是下界，未知用量不是 0。未核实账单金额，不虚构美元成本。主表、学习时间线和最终准确率仅来自 {esc(version)}。</p><div class="resource-links">{link(root, output, "restart-provenance.json", version + " 重启记录")}</div></section>'
    return banner, appendix


def build(root: Path = ROOT) -> Path:
    root = Path(root).resolve()
    output = root.parent / "output/officeqa-pro70-codex-adapter-comparison.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    state = read_json(root / "state.json")
    state = state if isinstance(state, dict) else {"status": "not_started"}
    restart_banner, restart_appendix = _restart_appendix(root, output)
    version = _version(root)
    try:
        tasks = data.load_tasks(root)
        task_error = None
    except (OSError, ValueError, KeyError) as exc:
        tasks, task_error = [], type(exc).__name__
    arms = state.get("arms", {})
    arms = arms if isinstance(arms, dict) else {}
    all_rows = {arm: arms.get(arm, {}).get("results", []) for arm in ARM_NAMES}
    all_rows = {arm: [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else [] for arm, rows in all_rows.items()}
    by_id = {arm: {row.get("task_id"): row for row in rows} for arm, rows in all_rows.items()}
    reasons = {arm: {row.get("task_id"): _failure_reason(root, row) for row in rows
                     if row.get("status") != "completed"} for arm, rows in all_rows.items()}
    failure_counts = {arm: Counter(reason["label"] for reason in reasons[arm].values() if reason) for arm in ARM_NAMES}
    failure_summary = ""
    if any(failure_counts.values()):
        counts_html = "".join(f'<p><b>{esc(ARM_NAMES[arm])}：</b>' +
                              ("；".join(f"{esc(label)} {count} 题" for label, count in failure_counts[arm].items()) or "当前没有失败记录") +
                              "</p>" for arm in ARM_NAMES)
        failure_summary = '<div class="notice"><b>已记录的非正常结束原因</b>' + counts_html + '<p class="small">分类只解释日志，不重写答案或得分，也不会送回 learner。“单次输出上限”“整题预算”和“传输失败”分别统计；HTTP 200 仍可能包含 incomplete 响应。</p></div>'
    metrics = state.get("metrics")
    metrics = metrics if isinstance(metrics, dict) and isinstance(metrics.get("arms"), dict) else None
    scored = {row.get("task_id"): row for row in metrics.get("results", []) if isinstance(row, dict)} if metrics else {}
    learning = state.get("learning", [])
    learning = [row for row in learning if isinstance(row, dict)] if isinstance(learning, list) else []
    learn_by_block = {row.get("block"): row for row in learning}
    task_versions, batch_versions, transitions = _lineage(root, learning)
    complete_count = {arm: arms.get(arm, {}).get("completed", len(all_rows[arm])) for arm in ARM_NAMES}
    complete_count = {arm: value if isinstance(value, int) else len(all_rows[arm]) for arm, value in complete_count.items()}
    cards = []
    for arm, title in ARM_NAMES.items():
        n = complete_count[arm]
        score = metrics["arms"].get(arm, {}) if metrics else {}
        usage = _usage(all_rows[arm])
        usage_text = f'仅答题 actor 用量：输入 {number(usage["input"])} / 输出 {number(usage["output"])} tokens；不含额外 learner 用量' if usage else "尚无答题 actor 用量记录；不含额外 learner 用量"
        incomplete_usage = sum(row.get("usage_complete") is False for row in all_rows[arm])
        if incomplete_usage:
            usage_text += f'；{incomplete_usage} 题含未知请求用量，当前合计仅为已知下界'
        score_text = f'{number(score.get("correct"))} / 70 · {percent(score.get("strict_accuracy"))}' if score else "答案暂不评分"
        secondary = f'<div class="small">1% 容差：{percent(score.get("one_percent_accuracy"))}</div>' if score else '<div class="small">等待两组共 140 份预测封存后统一评分</div>'
        submitted_count = sum(row.get("status") == "completed" for row in all_rows[arm])
        secondary += f'<div class="small">有效最终提交 {submitted_count} / 70；其余结束记录不等于成功提交。</div>'
        cards.append(f'<article class="arm-card {arm}"><div class="eyebrow">{"对照组 A" if arm == "baseline" else "学习组 B"}</div><h2>{title}</h2><div class="progress-count">{n}<span> / 70 题已结束</span></div><div class="progress" role="progressbar" aria-label="{esc(title)} 已结束题数" aria-valuenow="{min(n,70)}" aria-valuemin="0" aria-valuemax="70"><span style="width:{max(0,min(100,n/70*100)):.3f}%"></span></div><div class="score">{score_text}</div>{secondary}<p class="usage">{usage_text}</p><p>{"每题重新开始，不保留跨题经验。" if arm == "baseline" else "每 5 题由 GLM 总结经验，后续题使用已自动接纳的 adapter。"}</p></article>')
    delta = "尚无准确率差异可报告"
    if metrics and isinstance(metrics.get("strict_delta_percentage_points"), (int, float)):
        delta = f'学习组 − 对照组：{metrics["strict_delta_percentage_points"]:+.2f} 个百分点'
    pair_stats = ""
    if metrics:
        pairs = metrics.get("paired_strict", {})
        pair_stats = '<div class="pair-stats">' + "".join(f'<div><strong>{number(pairs.get(key))}</strong><span>{label}</span></div>' for key, label in (("both_correct", "两组都对"), ("adapted_only", "仅学习组对"), ("baseline_only", "仅对照组对"), ("both_wrong", "两组都错"))) + '</div>'
        wins, losses = pairs.get("adapted_only"), pairs.get("baseline_only")
        if isinstance(wins, int) and isinstance(losses, int):
            pair_stats += (f'<p class="notice"><b>如何理解这次结果：</b>严格判分下，学习组独有答对 {wins} 题，对照组独有答对 {losses} 题，净差 {wins-losses:+d} 题。'
                           '这是同一批 70 题的一次运行，不能据此断言稳定提升。1% 数值容差是单列的次要指标，不能替代上面的严格主指标；学习组另有额外学习用量。</p>')
    table_rows = []
    blocks = []
    for index, task in enumerate(tasks, 1):
        task_id = task["task_id"]
        grade_row = scored.get(task_id, {})
        disagreement = bool(grade_row and grade_row.get("baseline") != grade_row.get("adapted"))
        pending = any(task_id not in by_id[arm] for arm in ARM_NAMES)
        cells = []
        for arm in ARM_NAMES:
            row = by_id[arm].get(task_id)
            if row is None:
                cells.append(f'<td class="answer-cell {arm}"><span class="mobile-label">{ARM_NAMES[arm]}</span><span class="pill muted">等待运行</span><p class="empty">尚无提交答案</p></td>')
                continue
            answer = row.get("answer", row.get("final_answer", row.get("final_text", "")))
            answer_html = '<div class="answer">' + esc(answer) + '</div>' if str(answer).strip() else '<div class="empty">空答案（已发生的失败仍计入分母）</div>'
            grade_label = f'<span class="pill {"good" if grade_row[arm] else "bad"}">{"正确" if grade_row[arm] else "未命中"}</span>' if arm in grade_row else '<span class="pill muted">未评分</span>'
            revision = row.get("adapter_revision")
            version_text = task_versions.get(index, "版本待核对") if arm == "adapted" else ""
            revision_html = f'<div class="small">adapter 实际使用：{esc(version_text)}<br><code title="{esc(revision)}">{esc(str(revision)[:18])}…</code></div>' if revision else ""
            elapsed = f'{number(row.get("elapsed_seconds"), 1)} 秒' if isinstance(row.get("elapsed_seconds"), (int, float)) else "耗时未记录"
            reason = reasons[arm].get(task_id)
            reason_html = ""
            if reason:
                reason_html = f'<details class="termination"><summary>{esc(reason["label"])}</summary><p class="small">{esc(reason["detail"])}</p>{link(root, output, reason.get("evidence"), "终止依据")}</details>'
            cells.append(f'<td class="answer-cell {arm}"><span class="mobile-label">{ARM_NAMES[arm]}</span><div class="answer-meta"><span class="pill">{esc(status_label(row.get("status")))}</span>{grade_label}</div>{reason_html}{answer_html}<div class="small">{elapsed}</div>{revision_html}{_records_links(root, output, row)}</td>')
        table_rows.append(f'<tr data-pending="{str(pending).lower()}" data-disagreement="{str(disagreement).lower()}"><th scope="row"><div class="task-index">{index:02d} <span>{esc(task_id)}</span></div><details><summary>查看题目</summary><p class="question">{esc(task["question"])}</p></details></th>{"".join(cells)}</tr>')
    for block in range(1, 15):
        start, end = (block - 1) * 5 + 1, block * 5
        obj = learn_by_block.get(block)
        block_tasks = tasks[start-1:end]
        count = sum(task["task_id"] in by_id["adapted"] for task in block_tasks)
        if obj:
            admitted = obj.get("activated") is True
            text = "已接纳" if admitted else status_label(obj.get("status"))
            css = "done" if admitted else "attempted"
        else:
            text = "待学习" if count == 5 else "待答题"
            css = ""
        transition = transitions.get(block, text)
        blocks.append(f'<a class="block {css}" href="#learn-{block}"><span>第 {block} 批</span><strong>Q{start}–{end}</strong><small>{esc(batch_versions[block])}</small><i>答题后：{esc(transition)}</i></a>')
    learning_cards = []
    learning_usage_rows = []
    for block in range(1, 15):
        row = learn_by_block.get(block)
        title = f'第 {block} 次学习 · Q{(block-1)*5+1}–{block*5} 后'
        if row is None:
            learning_cards.append(f'<details id="learn-{block}" class="learn-card"><summary><span>{title}</span><span class="pill muted">尚无记录</span></summary><p>此轮尚无可展示的 GLM feedback。不会用人工内容补写。</p></details>')
            continue
        merged, feedback_path, content = _feedback(root, row)
        merged["block"] = block
        learning_usage_rows.append(merged)
        summary = "自动接纳" if merged.get("activated") is True else status_label(merged.get("status"))
        body = ""
        if content is not None:
            parsed = read_json(feedback_path) if feedback_path.suffix == ".json" else None
            if isinstance(parsed, dict) and isinstance(parsed.get("episodes"), list):
                for episode in parsed["episodes"]:
                    if not isinstance(episode, dict):
                        continue
                    body += f'<article class="feedback-episode"><h4>{esc(episode.get("id", ""))}</h4>'
                    for key, label in (("observations", "GLM 记录的观察"), ("self_assessment", "GLM 自评"), ("uncertainty", "GLM 标注的不确定性"), ("proposed_lesson", "GLM 提出的经验")):
                        body += f'<div class="feedback-field"><strong>{label}</strong><p>{esc(episode.get(key, "未记录"))}</p></div>'
                    body += '</article>'
            else:
                body = '<pre class="feedback-text">' + esc(content) + '</pre>'
            body += link(root, output, feedback_path, "原始 GLM feedback")
        else:
            body = '<p class="notice">此轮没有可读取的 feedback 文件。状态来自实际记录，不补写建议。</p>'
        body = f'<p class="notice">{esc(transitions.get(block, "版本继承记录待核对"))}。{"此轮发生在 Q70 后，不影响当前 70 题成绩。" if block == 14 else "仅影响后续批次，不能改写已提交答案。"}</p>' + body
        body += _records_links(root, output, merged)
        if merged.get("revision"):
            body += '<p class="small">本轮结束后保留的版本校验值：<code>' + esc(merged["revision"]) + '</code></p>'
        learning_cards.append(f'<details id="learn-{block}" class="learn-card"><summary><span>{title}</span><span class="pill {"good" if merged.get("activated") else "muted"}">{esc(summary)}</span></summary><div class="feedback-body">{body}</div></details>')
    learning_usage = _learning_cost(learning_usage_rows)
    by_block = ""
    if metrics and tasks:
        bars = []
        for block in range(14):
            selected = tasks[block*5:block*5+5]
            values = {arm: sum(scored.get(task["task_id"], {}).get(arm, 0) for task in selected) for arm in ARM_NAMES}
            bars.append(f'<div class="block-score"><span>{block+1:02d}</span><div><i class="baseline" style="width:{values["baseline"]*20}%"></i><i class="adapted" style="width:{values["adapted"]*20}%"></i></div><small>{number(values["baseline"])} / {number(values["adapted"])}</small></div>')
        by_block = '<section class="panel"><div class="section-title"><div><div class="eyebrow">分批结果</div><h2>每 5 题的严格命中数</h2></div><p class="small">蓝：对照组 / 绿：学习组<br>每批难度不同，曲线不等于学习因果证明。</p></div><div class="block-chart">' + ''.join(bars) + '</div></section>'
    task_notice = '<p class="notice">尚无通过校验的 70 题清单；暂不显示题目。准备完成后重新生成报告。</p>' if task_error else ""
    prepared_link = link(root, output, "prepared/manifest.json", "题目顺序与数据清单")
    protocol_link = link(root, output, "PROTOCOL.md", "完整预先协议")
    state_link = link(root, output, "state.json", "运行状态原件")
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    status = status_label(state.get("status", "not_started"))
    diagram = '''<svg class="flow" viewBox="0 0 960 190" role="img" aria-labelledby="flow-title"><title id="flow-title">两个独立 Codex 沙箱读取同一完整文档库，只有模型传输连接 Fireworks；学习组每五题由 GLM 更新 adapter。</title><defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#6c7d8b"/></marker></defs><rect x="8" y="57" width="180" height="80" rx="16" fill="#edf0f4"/><text x="98" y="88" text-anchor="middle">同一份 697 文档</text><text x="98" y="114" text-anchor="middle" class="svg-small">只读 corpus + 索引</text><path d="M188 96 L250 45 M188 96 L250 145" stroke="#6c7d8b" stroke-width="2" fill="none" marker-end="url(#arrow)"/><rect x="257" y="9" width="255" height="71" rx="16" fill="#edf4ff"/><text x="384" y="38" text-anchor="middle">A · Codex 独立沙箱</text><text x="384" y="61" text-anchor="middle" class="svg-small">不保留跨题 adapter</text><rect x="257" y="109" width="255" height="71" rx="16" fill="#e8f5ee"/><text x="384" y="138" text-anchor="middle">B · Codex 独立沙箱</text><text x="384" y="161" text-anchor="middle" class="svg-small">每五题 → GLM feedback → W</text><path d="M512 44 L607 83 M512 145 L607 107" stroke="#6c7d8b" stroke-width="2" fill="none" marker-end="url(#arrow)"/><rect x="612" y="53" width="169" height="84" rx="16" fill="#f5edde"/><text x="696" y="88" text-anchor="middle">受控模型传输</text><text x="696" y="112" text-anchor="middle" class="svg-small">固定 URL / 固定模型</text><path d="M782 95 L819 95" stroke="#6c7d8b" stroke-width="2" marker-end="url(#arrow)"/><rect x="826" y="53" width="126" height="84" rx="16" fill="#182d42"/><text x="889" y="88" text-anchor="middle" fill="white">Fireworks</text><text x="889" y="112" text-anchor="middle" fill="white" class="svg-small">GLM 5.3 Flash</text></svg>'''
    html = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OfficeQA Pro 70 · Codex × GLM adapter</title><style>{CSS}</style></head><body>
<header class="hero"><div class="wrap"><div class="topline"><a class="brand" href="#top">METAWORLD <span>/ EXPERIMENT 70 · {esc(version)}</span></a><span class="status">{esc(status)}</span></div><div class="hero-grid"><div><div class="eyebrow">同一模型 · 同一批题 · 两个独立沙箱</div><h1 id="top">经验能否让<br><em>下一题</em>做得更好？</h1><p class="lead">OfficeQA Pro：GLM 5.3 Flash + Codex，与增加 GLM 自主学习 adapter 后的逐题对照。</p></div><div class="hero-note"><strong>70 × 2</strong><span>固定 70 道题，两组各答一次</span><p><b>learn_every = 5</b><br>学习只影响后续题。没有人工／Astra 的逐题批改建议。</p></div></div><nav><a href="#progress">实时快照</a><a href="#protocol">运行机制</a><a href="#learning">GLM 每轮反馈</a><a href="#answers">70 题答案</a><a href="#evidence">证据与限制</a></nav><p class="timestamp">报告快照：{date} · 实验开始：{esc(state.get("started_utc", "尚未记录"))}。此页不会自行访问网络。</p></div></header>
<main class="wrap">{restart_banner}<section id="progress"><div class="section-title"><div><div class="eyebrow">PAIRED PROGRESS</div><h2>完成进度与真实得分</h2></div><span class="pill">结束 ≠ 答对</span></div><div class="arm-grid">{"".join(cards)}</div><div class="delta">{delta}</div>{pair_stats}<p class="small">“已结束”包含成功、超时与失败。严格准确率的分母固定为 70；空答案计 0。没有评分记录时不估算得分。</p>{failure_summary}</section>
<section id="protocol" class="panel"><div class="eyebrow">HOW IT WORKS</div><h2>harness 管执行，adapter 保留经验</h2><p><b>Codex harness</b> 是实际运行工具、文件操作和模型循环的程序。<b>Adapter</b> 是可读取的经验记忆；本实验更新 Markdown 知识，不修改 GLM 模型权重。</p>{diagram}<div class="explain-grid"><div><h3>共同输入</h3><p>固定 70 题及相同顺序、完整 697 份解析文本、相同工具与每题预算。每道题使用新的对话和工作目录。</p></div><div><h3>唯一学习通道</h3><p>学习组每完成 5 题，GLM 读取这 5 题的实际轨迹，自评并更新 adapter。下一批读取已接纳版本；失败更新则沿用旧版本。</p></div><div><h3>反馈来自哪里</h3><p>工具返回原始执行事实；只有 GLM 撰写解释和经验。Astra 编写程序、固定任务和学习规则，但不按答题结果写建议，也不进行人工语义择优。</p></div></div><p class="notice">GLM 自评没有标准答案标签，因此可能判断错误。“结构校验通过”表示格式与来源满足规定，不表示知识正确或成绩提升。</p><div class="notice"><b>{esc(version)} 固定资源预算</b><p>每组最多同时运行 1 道题，共最多 2 个答题进程。每题最多 24 次逻辑模型请求、65,536 个总输出 tokens、单次 32,768 个输出 tokens，以及 1,800 秒（含排队和限流等待）。Learner 每轮最多 64 次请求、120 次工具调用、65,536 个总输出 tokens、单次 32,768 个输出 tokens，以及 1,800 秒。</p><p>答题与学习共享限速器：最多 2 次 API 请求同时进行，两次发出至少间隔 1 秒。仅在服务端明确表示限流、尚未生成内容时，允许原样重发，最多 5 次；15 / 30 / 60 / 120 秒退避，并遵守更长的 Retry-After。部分生成、未知错误和答案好坏不触发重试。额外网络尝试与未知用量单独留痕。</p></div><div class="resource-links">{protocol_link} {prepared_link}</div></section>
<section id="sandbox"><div class="section-title"><div><div class="eyebrow">ISOLATION</div><h2>隔离目标与实测证据分开看</h2></div></div><div class="checks">{_preflight(root, output)}</div><div class="boundary-grid"><div><h3>允许</h3><p>当前题、完整只读文档库、当前工作区；学习组额外读取自己的 adapter。受控模型传输只连接 Fireworks 指定模型。</p></div><div><h3>禁止</h3><p>答案键、source_docs/source_files 提示、未来题目、其他工作区和旧成绩。工具进程只能访问本任务的 Fireworks 中继端口；其他本地端口、通用网络、网页搜索、其他模型与人工反馈不进入实验。Learner 的文件工具另行完全断网。</p></div></div><p class="small">隔离由覆盖整个 Codex 及其子进程的外部操作系统沙箱执行。因为 macOS 不允许嵌套应用此沙箱，Codex 内部沙箱仅在这层外部保护之内关闭；不是无隔离运行。两组可并行，共享硬件与 API 会影响耗时，速度不作为严格受控比较。</p>{_model_audit(root)}</section>
<section id="learning"><div class="section-title"><div><div class="eyebrow">LEARN EVERY FIVE</div><h2>14 批答题，14 次学习机会</h2></div><p class="small">W 表示持久经验记忆<br>先答题 → 再总结 → 影响下一批</p></div><div class="timeline">{"".join(blocks)}</div><p class="notice"><b>时间线展示实际版本：</b>版本名称来自保存的 adapter 来源／接纳路径，并与每题原生调用记录核对。第 k 轮只有通过接纳才产生可用 Wk；未接纳时沿用上一版本，不把计划编号当成实际版本。尚无调用记录的批次显示待运行。Q70 后的 W14 只保存，不能回改这 70 题答案。</p>{learning_usage}<div class="learning-list">{"".join(learning_cards)}</div><p class="small">下方反馈逐字来自保存的 GLM 文件。报告只排版，不替 GLM 补建议，不把 GLM 的判断写成已验证事实。</p></section>
{by_block}
<section id="answers"><div class="section-title"><div><div class="eyebrow">ALL 70 QUESTIONS</div><h2>逐题查看两个原始提交</h2></div><p class="small">不展示标准答案<br>不手工纠正模型提交</p></div><div class="filters"><label class="search-label"><span class="sr-only">搜索题号或题目</span><input id="search" type="search" placeholder="搜索题号、题目或模型回答…"></label><label><span class="sr-only">过滤题目</span><select id="filter"><option value="all">全部题目</option><option value="pending">尚未两组结束</option><option value="disagreement">两组得分不同</option></select></label><span id="visible-count">{len(tasks)} 道题</span></div>{task_notice}<div class="table-wrap"><table><thead><tr><th>题号与任务</th><th>对照组 · GLM + Codex</th><th>学习组 · GLM + Codex + adapter</th></tr></thead><tbody>{"".join(table_rows)}</tbody></table></div><p id="no-results" hidden>没有符合当前筛选条件的题目。</p></section>
<section id="evidence" class="panel"><div class="eyebrow">READ THE RESULT CORRECTLY</div><h2>这次实验能够说明什么</h2><div class="explain-grid"><div><h3>比较的是在线适应</h3><p>模型相同，基础 harness 相同，学习组额外获得跨题记忆和学习计算。应同时看准确率、逐题差异及额外用量，不能把全部变化归因于某一句经验。</p></div><div><h3>评分不等于反馈</h3><p>140 份预测全部封存后，隔离的官方 reward.py 按答案匹配打分。主指标为 0% 数值容差，1% 容差另外列示；不使用 LLM judge，评分也不回传给 learner。</p></div><div><h3>不是官方全量排名</h3><p>这里是原版 Pro 的 70/133 题，使用提供的解析 TXT 且断网。官方 PDF + web 条件不同；论文指出部分题需要外部信息，这些题仍保留在分母。</p></div></div><div class="resource-links">{state_link} {protocol_link} {prepared_link}<a href="https://huggingface.co/datasets/databricks/officeqa" target="_blank" rel="noopener">官方数据说明 ↗</a><a href="https://github.com/databricks/officeqa/tree/7b9a3c154ef9fb40215bb67934afc43e6799de16" target="_blank" rel="noopener">固定版官方评分器 ↗</a><a href="https://arxiv.org/html/2603.08655v1" target="_blank" rel="noopener">OfficeQA 论文 ↗</a></div><p class="small">本页是本地日志的展示快照，不是独立网络取证。预检与日志支持的范围之外，不声称“零风险”或“绝对没有预训练污染”。没有已完成结果时，结论保持未定。</p></section>
{restart_appendix}
</main><footer class="wrap">MetaWorld · OfficeQA Pro 70 · GLM-only feedback · {date}</footer><script>{JS}</script></body></html>'''
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix=".officeqa70-report-", suffix=".html", dir=output.parent, delete=False) as stream:
        stream.write(html)
        temporary = Path(stream.name)
    os.replace(temporary, output)
    return output


CSS = r'''
:root{--navy:#172b40;--ink:#24364a;--muted:#64758a;--line:#dce4ea;--paper:#f7f8f6;--blue:#4779bc;--green:#228367;--gold:#dab582}*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:25px}body{margin:0;background:var(--paper);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;font-size:15px;line-height:1.7;overflow-wrap:anywhere}a{color:#305e94;text-underline-offset:4px}a:hover{color:#133f70}.wrap{width:min(1140px,calc(100% - 64px));margin:auto}.hero{background:var(--navy);color:white;padding:29px 0 15px}.topline{display:flex;align-items:center;justify-content:space-between;gap:20px}.brand{color:white;text-decoration:none;font-size:13px;font-weight:800;letter-spacing:2px}.brand span{color:#9ab0c5;font-size:10px}.status{background:#ffffff14;border:1px solid #ffffff30;border-radius:50px;padding:5px 15px;font-size:12px}.hero-grid{display:grid;grid-template-columns:1.5fr 1fr;gap:75px;align-items:center;padding:54px 0 30px}.eyebrow{font-size:11px;letter-spacing:1.8px;font-weight:750;color:var(--muted);margin-bottom:10px}.hero .eyebrow{color:#9cb3ca}h1{font-size:clamp(35px,5.5vw,66px);line-height:1.2;letter-spacing:-2px;margin:0 0 23px}h1 em{font-style:normal;color:#e4c38f}.lead{max-width:610px;font-size:17px;color:#c0cddd}.hero-note{border-left:1px solid #ffffff30;padding-left:35px}.hero-note>strong{font-size:54px;letter-spacing:-2px;display:block}.hero-note>span{color:#bbcbdc}.hero-note p{font-size:13px;color:#b7c7d6;margin-top:25px}nav{display:flex;flex-wrap:wrap;gap:12px 28px;padding:20px 0;border-top:1px solid #ffffff20}nav a{color:#dce5f0;font-size:13px;text-decoration:none}nav a:hover{color:var(--gold)}.timestamp{font-size:10px;color:#91a6ba}main>section{margin:50px 0;min-width:0}h2{font-size:25px;line-height:1.35;letter-spacing:-.5px;margin:0 0 13px}h3{font-size:16px;line-height:1.4;margin:12px 0 8px}h4{font-size:14px;margin:0 0 15px}p{margin:8px 0 15px}.section-title{display:flex;justify-content:space-between;gap:22px;align-items:center;margin-bottom:22px}.section-title p{margin:0}.small{font-size:12px;color:var(--muted)}.arm-grid{display:grid;grid-template-columns:1fr 1fr;gap:22px}.arm-card{background:#fff;border:1px solid var(--line);border-top:4px solid var(--blue);padding:28px;border-radius:8px}.arm-card.adapted{border-top-color:var(--green)}.arm-card h2{font-size:20px}.arm-card .progress-count{font-size:45px;line-height:1.3;margin-top:20px;font-weight:720}.progress-count span{font-size:13px;font-weight:400;color:var(--muted)}.progress{height:7px;background:#e7edf4;border-radius:8px;overflow:hidden;margin:14px 0 22px}.progress span{display:block;height:100%;background:var(--blue)}.adapted .progress span{background:var(--green)}.score{font-size:20px;font-weight:700}.usage{font-size:12px;color:var(--muted);padding-top:14px;border-top:1px solid var(--line);margin-top:21px}.arm-card p:last-child{font-size:13px;margin-bottom:0}.delta{padding:18px 0 10px;font-size:16px;font-weight:650}.pair-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:7px 0 18px}.pair-stats>div{padding:16px;background:#fff;border:1px solid var(--line);border-radius:7px}.pair-stats strong{display:block;font-size:28px}.pair-stats span{font-size:12px;color:var(--muted)}.panel{padding:30px;background:white;border:1px solid var(--line);border-radius:10px}.flow{width:100%;height:auto;margin:15px 0 25px;display:block}.flow text{font-size:16px;fill:var(--ink)}.flow text[fill="white"]{fill:white}.flow .svg-small{font-size:12px}.explain-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:27px}.explain-grid p,.boundary-grid p{font-size:13px;color:#526378}.notice{padding:15px 18px;background:#f5f1e9;border-left:3px solid #d1ad74;font-size:13px;margin:20px 0}.resource-links{display:flex;flex-wrap:wrap;gap:10px 24px;font-size:12px;margin:18px 0 8px}.checks{display:grid;grid-template-columns:1fr 1fr;gap:20px}.check{border:1px solid var(--line);border-radius:8px;padding:22px;background:#fff}.check p{font-size:13px}.check a{font-size:12px}.boundary-grid{display:grid;grid-template-columns:1fr 1fr;gap:30px;margin:15px 0}.pill{display:inline-block;background:#eaf0f6;color:#4b6077;border-radius:20px;font-size:10px;padding:3px 9px;font-weight:600;vertical-align:middle;line-height:1.4;white-space:normal}.pill.good{background:#e5f3eb;color:#237558}.pill.bad{background:#fbeae5;color:#9c503b}.pill.muted{background:#edf0f3;color:#7d8792}.probe-list{list-style:none;padding:0;margin:8px 0}.probe-list li{margin:12px 0}.timeline{display:grid;grid-template-columns:repeat(7,1fr);gap:9px}.block{display:block;min-width:0;text-decoration:none;border:1px solid var(--line);border-radius:8px;background:#fff;padding:13px 12px;color:var(--ink)}.block span{font-size:10px;color:var(--muted)}.block strong{display:block;font-size:13px;margin:4px 0}.block small{display:block;font-size:10px;color:var(--muted)}.block i{display:block;font-size:10px;font-style:normal;margin-top:8px;color:#7a8998}.block.done{background:#edf7f1;border-color:#b2d4c3}.block.attempted{border-color:#d6be9d}.learning-list{display:grid;grid-template-columns:1fr 1fr;gap:10px;align-items:start}.learn-card{background:#fff;border:1px solid var(--line);border-radius:7px;min-width:0;scroll-margin-top:20px}.learn-card>summary{display:flex;justify-content:space-between;gap:12px;font-size:12px;font-weight:650;padding:15px;cursor:pointer;list-style:none}.learn-card>summary:before{content:'+';font-size:16px;color:#8b9aac}.learn-card[open]{grid-column:1/-1}.learn-card[open]>summary:before{content:'−'}.learn-card>p{padding:0 16px;font-size:13px}.feedback-body{padding:5px 20px 20px}.feedback-episode{border-top:1px solid var(--line);padding:22px 0}.feedback-field{display:grid;grid-template-columns:150px minmax(0,1fr);gap:12px;font-size:13px;margin:12px 0}.feedback-field strong{color:#6e7e8f;font-size:11px}.feedback-field p{white-space:pre-wrap;margin:0}.feedback-text{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.8 ui-monospace,monospace}.filters{display:flex;align-items:center;gap:12px;margin:25px 0 18px;flex-wrap:wrap}.search-label{flex:1;min-width:200px}input,select{font:13px/1.6 inherit;border:1px solid #cdd7e0;border-radius:6px;background:white;color:var(--ink);padding:10px 12px;max-width:100%}input{width:100%}.filters>span{font-size:12px;color:var(--muted)}.table-wrap{border:1px solid var(--line);border-radius:8px;background:white;overflow:hidden}table{width:100%;border-collapse:collapse;table-layout:fixed}thead{background:#edf1f5;text-align:left;font-size:12px}thead th{padding:16px}thead th:first-child{width:28%}tbody th,tbody td{border-top:1px solid var(--line);padding:19px 16px;vertical-align:top;text-align:left;min-width:0}tbody th{font-weight:400}.task-index{font-size:18px;font-weight:700}.task-index span{display:block;font-size:11px;color:var(--muted);font-weight:500}.question{font-size:12px;font-weight:400;white-space:pre-wrap}.task-index+details>summary{font-size:11px;cursor:pointer;color:#416a99;margin-top:9px}.answer{font-size:13px;white-space:pre-wrap;margin:13px 0}.termination{margin:10px 0;font-size:11px}.termination summary{cursor:pointer;color:#925633}.termination a{font-size:11px}.answer-meta{display:flex;gap:5px;flex-wrap:wrap}.empty{color:#91a0ad;font-size:12px;margin:14px 0}.record-links{font-size:11px;margin-top:12px}.record-links a{display:inline-block;margin-right:7px}.mobile-label{display:none}.block-chart{display:grid;grid-template-columns:1fr 1fr;gap:12px 30px}.block-score{display:grid;grid-template-columns:23px minmax(0,1fr) 55px;gap:12px;align-items:center}.block-score>span,.block-score small{font-size:11px;color:var(--muted)}.block-score>div{background:#f3f5f7}.block-score i{height:8px;display:block;margin:3px 0;background:var(--blue)}.block-score i.adapted{background:var(--green)}code{font-family:ui-monospace,monospace;font-size:11px;overflow-wrap:anywhere}footer{padding:30px 0 50px;border-top:1px solid var(--line);font-size:11px;color:var(--muted)}[hidden]{display:none!important}.sr-only{position:absolute;width:1px;height:1px;padding:0;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}button:focus-visible,a:focus-visible,summary:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #d2a86f;outline-offset:4px}@media(max-width:800px){.wrap{width:calc(100% - 38px)}.hero-grid{gap:25px;padding-top:40px}.hero-note{padding-left:22px}.hero-note>strong{font-size:42px}.timeline{grid-template-columns:repeat(4,1fr)}.explain-grid{grid-template-columns:1fr}.explain-grid>div{border-top:1px solid var(--line);padding-top:8px}.arm-card{padding:21px}.arm-card h2{font-size:17px}.panel{padding:23px}.learning-list{grid-template-columns:1fr}.learn-card[open]{grid-column:auto}.flow{min-height:125px}.section-title{align-items:flex-start}.section-title p{max-width:220px}.pair-stats{grid-template-columns:repeat(2,1fr)}}@media(max-width:560px){.wrap{width:calc(100% - 28px)}.hero{padding-top:19px}.brand{font-size:11px;letter-spacing:1px}.brand span{display:block;font-size:8px;margin-top:4px}.hero-grid{grid-template-columns:1fr;gap:20px;padding-top:32px}.hero-note{border-left:0;border-top:1px solid #ffffff30;padding:17px 0 0}.hero-note>strong{font-size:30px;display:inline-block;margin-right:15px;letter-spacing:-1px}.hero-note>span{font-size:12px}.hero-note p{margin-top:10px}.lead{font-size:14px}nav{gap:12px 19px}nav a{font-size:11px}.timestamp{font-size:9px}h1{font-size:43px}.arm-grid,.checks,.boundary-grid{grid-template-columns:1fr}.arm-grid{gap:14px}.arm-card{padding:22px}.arm-card h2{font-size:18px}.section-title{display:block}.section-title>p{margin-top:12px;max-width:none}h2{font-size:22px}main>section{margin:35px 0}.panel{padding:20px 17px}.timeline{grid-template-columns:repeat(2,1fr)}.block{padding:12px}.block strong{font-size:15px}.learning-list{gap:8px}.learn-card>summary{padding:13px 10px;font-size:11px}.feedback-body{padding:5px 12px 15px}.feedback-field{grid-template-columns:1fr;gap:4px}.flow{min-height:0}.notice{padding:13px;font-size:12px}.filters{gap:10px}.search-label{flex-basis:100%;min-width:0}.filters select{font-size:12px}.filters>span{font-size:11px}.table-wrap{border:0;border-radius:0;background:transparent}table,tbody{display:block}thead{display:none}tbody tr{display:grid;grid-template-columns:minmax(0,1fr);border:1px solid var(--line);border-radius:8px;background:#fff;margin-bottom:14px;overflow:hidden}tbody th,tbody td{display:block;padding:15px 16px;border-top:1px solid var(--line)}tbody th{border-top:0;background:#eff2f5}.task-index{display:flex;align-items:center;gap:12px}.task-index span{display:inline;font-size:11px}.mobile-label{display:block;font-size:10px;letter-spacing:.3px;font-weight:700;color:var(--blue);margin-bottom:10px}.adapted .mobile-label{color:var(--green)}.block-chart{grid-template-columns:1fr}.resource-links{gap:12px;font-size:11px}footer{font-size:10px}.pair-stats>div{padding:12px}.pair-stats strong{font-size:24px}}@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
'''

JS = r'''
(()=>{const search=document.querySelector('#search'),filter=document.querySelector('#filter'),rows=[...document.querySelectorAll('#answers tbody tr')],count=document.querySelector('#visible-count'),empty=document.querySelector('#no-results');function update(){const q=search.value.toLocaleLowerCase().trim(),mode=filter.value;let n=0;for(const row of rows){const visible=(!q||row.textContent.toLocaleLowerCase().includes(q))&&(mode==='all'||mode==='pending'&&row.dataset.pending==='true'||mode==='disagreement'&&row.dataset.disagreement==='true');row.hidden=!visible;if(visible)n++}count.textContent=`${n} / ${rows.length} 道题`;empty.hidden=n!==0}search.addEventListener('input',update);filter.addEventListener('change',update);document.querySelectorAll('.timeline a').forEach(a=>a.addEventListener('click',()=>{const target=document.querySelector(a.getAttribute('href'));if(target)target.open=true}));})();
'''


if __name__ == "__main__":
    print(build())
