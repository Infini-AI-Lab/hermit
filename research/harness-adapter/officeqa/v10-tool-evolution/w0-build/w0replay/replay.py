"""Replay the collected invocations through the v9 and the v10 tools and compare (task R, step 2).

    python -B w0replay/replay.py [--out DIR] [--workers 4] [--timeout 20] [--analyze-only] [--limit N]

Each invocation runs as a subprocess of the frozen actor python (`-I -B`, as the kit wrappers do): the v9
cli.py on the v9 index and the v10 cli.py on the v10 index.  Raw outputs go to `<out>/results.jsonl`
(scratchpad only; they contain table values).  `analyze()` then writes `<out>/summary.json` and
`<out>/samples.md` (numbers masked as <v>) that REPORT.md quotes.

Definitions used in the summary:
- regression: v9 exit 0 -> v10 exit >= 2 (the tool refused or failed a command that used to work);
- crash: exit 3 (`internal error`) or a Python traceback on stderr; timeout: no exit within `--timeout`;
- removed line: a line of the v9 page that does not occur anywhere in the v10 page (exact text);
  changed page: any removed or added line.  W0 is meant to be additive, so removed lines are the signal.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures
import difflib
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

SAMPLE_PER_TOOL = {"find_tables": 7, "show_table": 7, "printings": 6}
LINE_KINDS = (("group", re.compile(r"^\[")),
              ("part", re.compile(r"^\s+treasury_bulletin_\d{4}_\d{2}\.txt:\d+ part")),
              ("row", re.compile(r"^\d+\s+\d+\s")),
              ("listing", re.compile(r"^\s+\d{4}_\d{2}\s+treasury_bulletin_")),
              ("summary", re.compile(r"^\s+\[[^\]]+\] part\d|^\s+[^:]+: \d{4}_\d{2}\.\.|^\s+first printing")),
              ("column", re.compile(r"^\s+(?:c\d+|v\d+\s+=)")),
              ("cut", re.compile(r"^\.\.\. ")))


def run_one(cli: Path, index: Path, tool: str, args: list, timeout: float) -> dict:
    """One tool subprocess; never raises."""
    cmd = [str(C.PYTHON), "-I", "-B", str(cli), tool, *args]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, env=C.run_env(index), capture_output=True, text=True, timeout=timeout)
        return {"exit": p.returncode, "stdout": p.stdout, "stderr": p.stderr[-2000:], "secs": round(time.time() - t0, 2),
                "timeout": False}
    except subprocess.TimeoutExpired as e:
        return {"exit": None, "stdout": (e.stdout or b"").decode("utf8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
                "stderr": "timeout", "secs": round(time.time() - t0, 2), "timeout": True}


def replay(invocations: list, out: Path, workers: int, timeout: float) -> None:
    """Run every invocation not already in results.jsonl (resumable); 4 workers = 4 subprocesses at a time."""
    path = out / "results.jsonl"
    done = set()
    if path.exists():
        for line in path.read_text().splitlines():
            r = json.loads(line)
            done.add((r["tool"], tuple(r["args"])))
    todo = [x for x in invocations if (x["tool"], tuple(x["args"])) not in done]
    print(f"{len(todo)} to run ({len(done)} already done)")
    lock = threading.Lock()

    def job(x):
        r9 = run_one(C.V9_CLI, C.V9_INDEX, x["tool"], x["args"], timeout)
        r10 = run_one(C.V10_CLI, C.V10_INDEX, x["tool"], x["args"], timeout)
        rec = {"tool": x["tool"], "args": x["args"], "right": x["right"], "sources": x["sources"], "v9": r9, "v10": r10}
        with lock:
            with path.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
        return rec

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for i, _ in enumerate(ex.map(job, todo), 1):
            if i % 100 == 0:
                print(f"  {i}/{len(todo)}", flush=True)


# ---- analysis

def line_kind(line: str) -> str:
    for kind, rx in LINE_KINDS:
        if rx.search(line):
            return kind
    return "other"


def diff_lines(a: str, b: str) -> tuple:
    """(removed, added): lines of a absent from b and lines of b absent from a (exact, rstripped)."""
    la, lb = [x.rstrip() for x in a.splitlines()], [x.rstrip() for x in b.splitlines()]
    sa, sb = set(la), set(lb)
    return [x for x in la if x not in sb], [x for x in lb if x not in sa]


def masked_diff(rec: dict, max_lines: int = 40) -> str:
    """A unified diff of the two pages with every value masked, cut to `max_lines`."""
    a = C.mask_values(rec["v9"]["stdout"]).splitlines()
    b = C.mask_values(rec["v10"]["stdout"]).splitlines()
    d = list(difflib.unified_diff(a, b, "v9", "v10", n=1, lineterm=""))
    if len(d) > max_lines:
        d = d[:max_lines] + [f"... ({len(d) - max_lines} more diff lines)"]
    return "\n".join(d)


def summarize(records: list) -> tuple:
    """(summary dict, sample markdown) over the replay records."""
    s = {"invocations": len(records), "per_tool": dict(collections.Counter(r["tool"] for r in records)),
         "exit_transitions": {}, "regressions": [], "crashes": [], "timeouts": [], "right": {}}
    trans = collections.Counter()
    for r in records:
        e9, e10 = r["v9"]["exit"], r["v10"]["exit"]
        trans[f"{e9}->{e10}"] += 1
        argv = C.mask_argv(r["tool"], r["args"])
        if e9 == 0 and (e10 is None or e10 >= 2):
            s["regressions"].append({"argv": argv, "v9_exit": e9, "v10_exit": e10,
                                     "v10_stderr": C.mask_values(r["v10"]["stderr"][-300:])})
        for v in ("v9", "v10"):
            if r[v]["exit"] == 3 or "Traceback" in r[v]["stderr"]:
                s["crashes"].append({"version": v, "argv": argv, "stderr": C.mask_values(r[v]["stderr"][-300:])})
            if r[v]["timeout"]:
                s["timeouts"].append({"version": v, "argv": argv, "secs": r[v]["secs"]})
    s["exit_transitions"] = dict(sorted(trans.items(), key=lambda kv: -kv[1]))
    s["exit_changed"] = [{"argv": C.mask_argv(r["tool"], r["args"]), "v9": r["v9"]["exit"], "v10": r["v10"]["exit"],
                          "v9_stderr": C.mask_values(r["v9"]["stderr"][-160:]), "v10_first_line": C.mask_values(r["v10"]["stdout"][:160])}
                         for r in records if r["v9"]["exit"] != r["v10"]["exit"]]
    s["slowest_v10_secs"] = sorted((r["v10"]["secs"] for r in records), reverse=True)[:5]
    s["max_page_bytes"] = {v: max(len(r[v]["stdout"].encode()) for r in records) for v in ("v9", "v10")}
    s["pages_over_8000_bytes"] = {v: sum(len(r[v]["stdout"].rstrip("\n").encode()) > 8000 for r in records) for v in ("v9", "v10")}
    s["right"] = _right_cells(records)
    s["content_loss"], lossy = content_loss(records)
    return s, _samples(lossy)


def _right_cells(records: list) -> dict:
    """Line-level comparison restricted to invocations that a RIGHT cell ran (both exits 0)."""
    right = [r for r in records if r["right"] and r["v9"]["exit"] == 0 and r["v10"]["exit"] == 0]
    out = {"invocations": len(right), "changed": 0, "with_removed_lines": 0, "per_tool": {},
           "removed_line_kinds": collections.Counter(), "added_line_kinds": collections.Counter(),
           "identical": 0, "byte_growth_median": None}
    per = collections.defaultdict(lambda: {"n": 0, "changed": 0, "with_removed_lines": 0, "identical": 0})
    growth = []
    for r in right:
        removed, added = diff_lines(r["v9"]["stdout"], r["v10"]["stdout"])
        t = per[r["tool"]]
        t["n"] += 1
        growth.append(len(r["v10"]["stdout"]) - len(r["v9"]["stdout"]))
        if not removed and not added:
            out["identical"] += 1
            t["identical"] += 1
            continue
        out["changed"] += 1
        t["changed"] += 1
        if removed:
            out["with_removed_lines"] += 1
            t["with_removed_lines"] += 1
        out["removed_line_kinds"].update(line_kind(x) for x in removed)
        out["added_line_kinds"].update(line_kind(x) for x in added)
    out["per_tool"] = dict(per)
    out["removed_line_kinds"] = dict(out["removed_line_kinds"].most_common())
    out["added_line_kinds"] = dict(out["added_line_kinds"].most_common())
    if growth:
        out["byte_growth_median"] = sorted(growth)[len(growth) // 2]
    return out


def _samples(lossy: dict) -> str:
    """Twenty masked diffs of right-cell pages with a content loss, the heaviest losses first, spread over
    the three tools (one page per distinct argv)."""
    parts, picks = [], []
    for tool, k in SAMPLE_PER_TOOL.items():
        picks += sorted(lossy.get(tool, []), key=lambda t: -t[0])[:k]
    spare = sorted((x for t in lossy for x in lossy[t] if x not in picks), key=lambda t: -t[0])
    picks += spare[:max(0, sum(SAMPLE_PER_TOOL.values()) - len(picks))]
    for n, (weight, m, r) in enumerate(picks, 1):
        src = r["sources"][0]
        argv = C.mask_argv(r["tool"], r["args"])
        metrics = {x: y for x, y in m.items() if x not in ("kind", "flipped")}
        parts.append(f"### sample {n}: `{argv}`\n\nfrom {src[2]} cell {src[0]} (right); loss metrics {json.dumps(metrics)}; "
                     f"v9 {len(r['v9']['stdout'])} B -> v10 {len(r['v10']['stdout'])} B\n\n```diff\n{masked_diff(r)}\n```\n")
    return "\n".join(parts)


# ---- content-level comparison (what a right cell could actually lose)

ROW_KEY_RX = re.compile(r"^(\d+)\s+(\d+)\s")                                   # source line, panel
LISTING_KEY_RX = re.compile(r"^\s+(\d{4}_\d{2})\s+(treasury_bulletin_\S+)\s+(p\d+)\s+(\S+(?: est| to date)?)")
DAY_PERIOD_RX = re.compile(r"^\d{4}-\d{2}-\d{2}|^\d{4}-\d{2}\S")                  # a daily/weekly row's filled period
VERDICT_RX = re.compile(r"^\s+(\[[^\]]+\] part\d+)(?: panel '[^']*')? (\S+(?: \(\d+\))?).*?: (\d+) printings?, (\d+) distinct values? - (same in all|REVISED)")
PAGE_ROWS_RX = re.compile(r"page rows (\d+)-(\d+) of (\d+)")


def _rows(page: str) -> dict:
    """(source line, panel) -> row text (unstacked panels repeat the line numbers)."""
    return {(m.group(1), m.group(2)): l.rstrip() for l in page.splitlines() for m in [ROW_KEY_RX.match(l)] if m}


def _listing_keys(page: str) -> set:
    return {m.groups() for l in page.splitlines() for m in [LISTING_KEY_RX.match(l)] if m}


def _verdicts(page: str) -> dict:
    """SUMMARY key -> (printings, distinct, verdict); the v10 key adds a panel stub, so keys are compared
    on the (period, part, column) prefix that both versions print (the first panel of a key wins)."""
    out = {}
    for l in page.splitlines():
        m = VERDICT_RX.match(l)
        if m:
            out.setdefault(f"{m.group(1)} {m.group(2)}", (int(m.group(3)), int(m.group(4)), m.group(5)))
    return out


def find_tables_loss(r: dict) -> dict:
    """Did the v10 page lose what the v9 page showed: the top group, its parts, any part line?"""
    g9, g10 = C.parse_find_page(r["v9"]["stdout"]), C.parse_find_page(r["v10"]["stdout"])
    if not g9:
        return {"kind": "find_tables", "empty_v9": True}
    key = lambda g: (g["id"], re.sub(r"\W+", "", C.TABLE_N_RX.sub("", g["title"]).lower()))  # noqa: E731
    parts9 = {p for g in g9 for p in g["parts"]}
    parts10 = {p for g in g10 for p in g["parts"]}
    top_parts9 = set(g9[0]["parts"])
    top10 = next((g for g in g10 if key(g) == key(g9[0])), None)
    n9, n10 = len(top_parts9), len(top10["parts"]) if top10 else 0
    all_flag = "--all" in r["args"]
    why = ("top group absent" if top10 is None else "none" if not (top_parts9 - parts10) else
           "12-part cap under --all" if all_flag and n9 > 12 and n10 >= 12 else
           "budget cut before the top block" if n10 == 0 else "budget cut inside the top block" if n10 < min(n9, 12)
           else "different parts chosen")
    return {"kind": "find_tables", "top_same": bool(g10) and key(g9[0]) == key(g10[0]),
            "top_in_v10": top10 is not None, "top_parts_v9": n9, "top_parts_v10": n10, "top_loss_why": why,
            "parts_lost": len(parts9 - parts10), "top_parts_lost": len(top_parts9 - parts10),
            "groups_lost": sum(1 for g in g9 if not any(key(g) == key(h) for h in g10)),
            "v9_cut": "OUTPUT CUT" in r["v9"]["stdout"], "v10_cut": "OUTPUT CUT" in r["v10"]["stdout"]}


def show_table_loss(r: dict) -> dict:
    """Rows of the v9 page missing from the v10 page (by source line number) or printed differently."""
    r9, r10 = _rows(r["v9"]["stdout"]), _rows(r["v10"]["stdout"])
    lost = [k for k in r9 if k not in r10]
    changed = [k for k in r9 if k in r10 and r9[k] != r10[k]]
    m9, m10 = PAGE_ROWS_RX.search(r["v9"]["stdout"]), PAGE_ROWS_RX.search(r["v10"]["stdout"])
    return {"kind": "show_table", "rows_v9": len(r9), "rows_lost": len(lost), "rows_changed": len(changed),
            "page_rows_v9": m9.group(0) if m9 else None, "page_rows_v10": m10.group(0) if m10 else None,
            "notes_added": sum(1 for l in r["v10"]["stdout"].splitlines() if l.startswith("  ") and
                               any(k in l for k in ("YEARS ANCHORED", "realigned", "repaired", "0 rows:", "no table starts")))}


def printings_loss(r: dict) -> dict:
    """Listing lines of the v9 page missing from the v10 page, and SUMMARY verdicts that flipped."""
    k9, k10 = _listing_keys(r["v9"]["stdout"]), _listing_keys(r["v10"]["stdout"])
    v9, v10 = _verdicts(r["v9"]["stdout"]), _verdicts(r["v10"]["stdout"])
    flipped = [k for k in v9 if k in v10 and v9[k][2] != v10[k][2]]
    lost = k9 - k10
    lost_keys = [k for k in v9 if k not in v10]
    if not lost_keys:
        key_cause = "none"
    elif "no matching row/column" in r["v10"]["stdout"]:
        key_cause = "v10 no-match (regression)"
    elif "snapshot table" in r["v10"]["stdout"]:
        key_cause = "snapshot table re-keyed by as-of (W0-3)"
    elif all(DAY_PERIOD_RX.match(k[1:]) for k in lost_keys):
        key_cause = "daily/weekly keys hidden by the month key (W0-11)"
    else:
        key_cause = "other"
    return {"kind": "printings", "listing_v9": len(k9), "listing_lost": len(lost), "summary_key_loss_cause": key_cause,
            "listing_lost_daily": sum(1 for k in lost if DAY_PERIOD_RX.match(k[3])),
            "summary_keys_v9": len(v9), "summary_keys_v10": len(v10), "summary_keys_lost": sum(1 for k in v9 if k not in v10),
            "verdicts_flipped": len(flipped), "flipped": flipped[:3],
            "v9_cut": "OUTPUT CUT" in r["v9"]["stdout"], "v10_omitted_listing": "listing lines omitted" in r["v10"]["stdout"],
            "v9_no_match": "no matching row/column" in r["v9"]["stdout"], "v10_no_match": "no matching row/column" in r["v10"]["stdout"]}


def _header_change(m: dict) -> str:
    """Classify a changed `page rows A-B of N` header: total N changed (filter semantics) or only the window."""
    a, b = PAGE_ROWS_RX.search(m["page_rows_v9"] or ""), PAGE_ROWS_RX.search(m["page_rows_v10"] or "")
    if not a or not b:
        return "header missing on one side"
    if a.group(3) != b.group(3):
        return "row total N changed (filter semantics)" + (" - v10 has more rows" if int(b.group(3)) > int(a.group(3)) else " - v10 has fewer rows")
    return "window changed, same total"


LOSS = {"find_tables": find_tables_loss, "show_table": show_table_loss, "printings": printings_loss}


def content_loss(records: list) -> tuple:
    """Aggregate the per-tool loss metrics over RIGHT-cell invocations (both exits 0); returns the
    aggregate and the records with a real loss (for the samples)."""
    agg = collections.defaultdict(collections.Counter)
    lossy = collections.defaultdict(list)
    for r in records:
        if not (r["right"] and r["v9"]["exit"] == 0 and r["v10"]["exit"] == 0):
            continue
        m = LOSS[r["tool"]](r)
        a = agg[r["tool"]]
        a["pages"] += 1
        if m.get("empty_v9"):
            a["v9 page had no groups"] += 1
            continue
        if r["tool"] == "find_tables":
            a["top group unchanged"] += m["top_same"]
            a["v9 top group absent from v10 page"] += not m["top_in_v10"]
            a["pages losing a v9 part line"] += m["parts_lost"] > 0
            a["pages losing a top-group part line"] += m["top_parts_lost"] > 0
            if m["top_parts_lost"] or not m["top_in_v10"]:
                a[f"  top-group loss cause: {m['top_loss_why']}"] += 1
            a["pages losing a v9 group line"] += m["groups_lost"] > 0
            a["v9 cut"] += m["v9_cut"]
            a["v10 cut"] += m["v10_cut"]
            prio = {"top group absent": 4, "budget cut before the top block": 3, "different parts chosen": 2}.get(m["top_loss_why"], 1)
            weight = prio * 1000 + m["top_parts_lost"] * 10 + m["parts_lost"]
        elif r["tool"] == "show_table":
            a["pages losing a row"] += m["rows_lost"] > 0
            a["pages with a changed row text"] += m["rows_changed"] > 0
            a["pages with new NOTES/diagnosis lines"] += m["notes_added"] > 0
            a["page-rows header changed"] += m["page_rows_v9"] != m["page_rows_v10"]
            if m["page_rows_v9"] != m["page_rows_v10"]:
                a[f"  header change: {_header_change(m)}"] += 1
            weight = m["rows_lost"] * 100 + m["rows_changed"]
        else:
            a["pages losing a listing line"] += m["listing_lost"] > 0
            if m["listing_lost"]:
                a["  listing loss with the 'listing lines omitted; SUMMARY complete' trailer"] += m["v10_omitted_listing"]
                a["  listing loss = daily/weekly rows of the month only (W0-11 month key)"] += (
                    not m["v10_omitted_listing"] and m["listing_lost_daily"] == m["listing_lost"])
                a["  listing loss unexplained"] += not m["v10_omitted_listing"] and m["listing_lost_daily"] != m["listing_lost"]
            a["pages losing a SUMMARY key"] += m["summary_keys_lost"] > 0
            if m["summary_keys_lost"]:
                a[f"  SUMMARY key loss cause: {m['summary_key_loss_cause']}"] += 1
            a["pages with a flipped verdict (same<->REVISED)"] += m["verdicts_flipped"] > 0
            a["v9 cut"] += m["v9_cut"]
            a["v10 listing omitted for SUMMARY"] += m["v10_omitted_listing"]
            a["v9 no-match"] += m["v9_no_match"]
            a["v10 no-match"] += m["v10_no_match"]
            weight = (5000 if m["summary_key_loss_cause"].startswith("v10 no-match") else 0) + m["verdicts_flipped"] * 1000 \
                + m["summary_keys_lost"] * 100 + m["listing_lost"]
        if weight:
            lossy[r["tool"]].append((weight, m, r))
    return {t: dict(c) for t, c in agg.items()}, lossy


def analyze(out: Path) -> dict:
    records = [json.loads(l) for l in (out / "results.jsonl").read_text().splitlines()]
    s, samples = summarize(records)
    (out / "summary.json").write_text(json.dumps(s, indent=1))
    (out / "samples.md").write_text(samples)
    leaks = C.leak_scan(samples)
    s["samples_leak_scan"] = leaks[:20]
    print(f"leak scan of samples.md: {len(leaks)} value-like tokens left" + (f": {leaks[:8]}" if leaks else ""))
    print(json.dumps({k: v for k, v in s.items() if k not in ("regressions", "crashes", "timeouts", "right")}, indent=1))
    print(f"regressions {len(s['regressions'])}, crashes {len(s['crashes'])}, timeouts {len(s['timeouts'])}")
    return s


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", default=str(C.SCRATCH))
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--limit", type=int, default=0, help="run only the first N invocations (smoke test)")
    p.add_argument("--analyze-only", action="store_true")
    p.add_argument("--invocations", help="collect.py output to replay (default: <out>/invocations.json)")
    a = p.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if not a.analyze_only:
        inv = json.loads(Path(a.invocations or out / "invocations.json").read_text())
        replay(inv[:a.limit] if a.limit else inv, out, a.workers, a.timeout)
    analyze(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
