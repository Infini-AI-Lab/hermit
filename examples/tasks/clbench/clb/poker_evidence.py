"""Evidence rendering for the exploitable-poker venue.

One block is a handful of hands, rendered one entry per hand: position,
cards, board, both sides' actions, showdown when it happened, chip delta,
record address. No counts across hands (Part 3.3, 2026-09-06): what the
opponent did over many hands is for the learner to tally from the records.

Two deliberate omissions.

The harness's instance_id is replaced by the hand number. It reads
`poker:calling_station:hand:stage0_seed42_hand1` — the strategy's real name,
the schedule stage (when the opponent changes) and the dealing seed (with
the dealing library, every card of the hand) are in it, and the host never
sees that field. Operator principle: the learner reads the host's whole
trajectory, so it sees everything the host saw, and it must not see MORE.
Stripping only the codename (until 2026-09-13) still left the stage index,
a direct read of the drift schedule; the learner-visible id is now `h021`,
the same address the record file carries.

Hole cards of the opponent appear only where the hand reached showdown,
because that is the only place the environment reveals them.
"""

import collections
import json
import os

from . import record as _record
import re


_OPPONENT = re.compile(r"^Opponent:\s*(.+)$", re.M)
_YOUR_HAND = re.compile(r"^Your hand:\s*(.+)$", re.M)
_BOARD = re.compile(r"^Board:\s*(.+)$", re.M)
_POSITION = re.compile(r"^Your position:\s*(.+)$", re.M)
_OPP_ACTIONS = re.compile(r"^Opponent's actions?:\s*(.+)$", re.M)
_SHOWDOWN = re.compile(r"SHOWDOWN:(.*?)(?:\n\n|$)", re.S)
_DELTA = re.compile(r"Net chip change this hand:\s*([+-]?\d+)")
_NOTICE = re.compile(r"^NOTICE: (.+)$", re.M)


def learner_hand_id(hand_num: int) -> str:
    """The only hand address the learner sees: the global hand number, the
    stem of the record file. The harness's `poker:<policy>:hand:<stage>_
    <seed>_<hand>` never leaves the operator's run log."""
    return f"h{int(hand_num):03d}"


def group_hands(turns: list) -> list:
    """One dict per hand, in play order, from a block's turn list."""
    by_hand = collections.OrderedDict()
    for t in turns:
        meta = (t.get("query") or {}).get("metadata") or {}
        key = meta.get("hand_num")
        # The task omits hand_num on the terminal query. The runner does not
        # send that one through respond(), so this should not fire — but a
        # None key would reach write_raw's f"h{key:03d}" and take the whole
        # block down, and a lost block is worse than a lost turn.
        if not isinstance(key, int):
            continue
        by_hand.setdefault(key, []).append(t)

    out = []
    for hand_num, all_ts in by_hand.items():
        # wm calls (channel turns) are listed per hand; they are not poker
        # actions and must not read as one.
        chan = [t for t in all_ts if t.get("channel")]
        ts = [t for t in all_ts if not t.get("channel")] or all_ts
        first, last = ts[0], ts[-1]
        prompt = (first.get("query") or {}).get("prompt") or ""
        meta = (first.get("query") or {}).get("metadata") or {}
        acts = [(t.get("response") or {}).get("action") or {} for t in ts]

        # What the opponent did, read off the per-turn prompts rather than
        # from any hidden state: the same view the host had.
        opp = []
        for t in ts:
            p = (t.get("query") or {}).get("prompt") or ""
            m = _OPP_ACTIONS.search(p)
            if m:
                opp += [a.strip() for a in m.group(1).split("->")]

        # The board grows street by street, so the LAST prompt that showed one
        # carries the most cards. Reading it off the first prompt would print
        # "No board" for every hand that reached the river.
        board = "No board"
        for t in ts:
            m = _BOARD.search((t.get("query") or {}).get("prompt") or "")
            if m and m.group(1).strip() != "No board":
                board = m.group(1).strip()

        fb = ((last.get("observation") or {}).get("content")) or ""
        sd = _SHOWDOWN.search(fb)
        delta = _DELTA.search(fb)
        notice = _NOTICE.search(prompt)

        out.append({
            "hand_num": hand_num,
            "opponent": (meta.get("opponent_name")
                         or (_OPPONENT.search(prompt) or [None, "?"])[1]),
            "position": (_POSITION.search(prompt) or [None, "?"])[1],
            "your_hand": (_YOUR_HAND.search(prompt) or [None, "?"])[1],
            "board": board,
            "your_actions": [f"{a.get('action')}"
                             + (f" {a.get('amount')}" if a.get("amount")
                                else "") for a in acts if a.get("action")],
            "opponent_actions": opp,
            "channel": [{"name": ((t.get("response") or {}).get("action") or {}).get("name", ""),
                         "args": ((t.get("response") or {}).get("action") or {}).get("args", {}),
                         "result": ((t.get("observation") or {}).get("content") or "")}
                        for t in chan],
            "showdown": sd.group(1).strip() if sd else None,
            "delta": int(delta.group(1)) if delta else 0,
            "opponent_changed": bool(notice),
            "instance_id": learner_hand_id(hand_num),
            "turns": all_ts,          # the raw record keeps wm turns too
        })
    return out


def write_raw(hands: list, evidence_dir: str, block: int) -> dict:
    """The complete turn records, one file per hand; returns hand_num->path.

    Every instance_id is replaced by the hand number on the way out,
    including inside the raw turns — the raw record is what the learner
    reads when a hand deserves a close look, and the omission has to hold
    there too or it holds nowhere.
    """
    bdir = os.path.join(evidence_dir, f"block{block:03d}")
    os.makedirs(bdir, exist_ok=True)
    paths = {}
    for h in hands:
        scrubbed = json.loads(json.dumps(h["turns"]))
        hid = learner_hand_id(h["hand_num"])
        for t in scrubbed:
            meta = (t.get("query") or {}).get("metadata") or {}
            if "instance_id" in meta:
                meta["instance_id"] = hid
            if (t.get("query") or {}).get("instance_id"):
                t["query"]["instance_id"] = hid
            obs = t.get("observation")
            if isinstance(obs, dict) and isinstance(obs.get("content"), str):
                obs["content"] = _record.clip_obs(obs["content"])
        name = f"h{h['hand_num']:03d}.json"
        with open(os.path.join(bdir, name), "w") as f:
            json.dump(scrubbed, f, indent=1)
        paths[h["hand_num"]] = f"/evidence/block{block:03d}/{name}"
    return paths


def one_hand(h: dict, raw_path: str) -> str:
    lines = [
        f"### Hand {h['hand_num']} vs {h['opponent']}"
        + ("   [opponent changed at this hand]" if h["opponent_changed"]
           else ""),
        f"your position: {h['position']}   your cards: {h['your_hand']}",
        f"board: {h['board']}",
        f"your actions: {' -> '.join(h['your_actions']) or '(none)'}",
        f"opponent actions: {' -> '.join(h['opponent_actions']) or '(none)'}",
    ]
    for c in h.get("channel") or []:
        r = " ".join(c["result"].split())
        lines.append(f"world-model wm {c['name']} {json.dumps(c['args'])}"
                     + ("   -> " + (r[:300] + " ..." if len(r) > 300 else r)
                        if r else ""))
    if h["showdown"]:
        lines.append("showdown: " + " ".join(h["showdown"].split()))
    else:
        lines.append("no showdown — the opponent's cards were not revealed")
    lines.append(f"net: {h['delta']:+d} chips")
    lines.append(f"raw record: {raw_path}")
    return "\n".join(lines)


def render(hands: list, evidence_dir: str, block: int) -> str:
    from . import evidence_readme
    evidence_readme.write(evidence_dir, "poker")
    paths = write_raw(hands, evidence_dir, block)
    # Part 3.3 (2026-09-06, user rule): no wrapper-computed statistics —
    # one block per hand, facts and address only; counting is the
    # learner's own work.
    body = ["=" * 70]
    for h in hands:
        body += [one_hand(h, paths[h["hand_num"]]), "=" * 70]
    return "\n".join(body)
