"""v9 seams shared by every stream. Benchmark-neutral: nothing here may import from `packs`.

Read DESIGN.md (officeqa-v9-design/DESIGN.md) first. This module fixes the data shapes and call
signatures that the environment kit, the pack tools, the broker controller, the rule engine, the
learners and the orchestration exchange. Implementations live elsewhere; change a contract only
together with every user of it.

Terms
- episode: one actor run on one item (question) inside one arm of one campaign.
- ledger: the broker-owned, append-only event log of an episode, derived from the items the broker
  relays between Codex and the model (what the model actually saw), never from actor-writable files.
- rule: a declarative check (`when` predicates -> finding template -> optional offer) evaluated by
  the trusted engine at `submit` time and in offline replay.
- offer: a concrete alternative answer the engine derives itself (e.g. re-running the model's own
  recorded computation with one input substituted), so the weak actor only has to choose.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence

CONTRACT_VERSION = 'v9.1'  # v9.1: wave-1 integration (View.call, Command.tool, allowed_words, next_ref, needs_enforcement, drain_log, 'rejected')

# ---------------------------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------------------------

Learner = Literal['text', 'mechanism']
Seed = Literal['none', 'text', 'rules']


@dataclass(frozen=True)
class ArmSpec:
    """One experimental arm; parsed once from `.task-paths.json` key "arm" (see arms.py)."""
    name: str                      # plain | text-static | text-evolve | mech-static | mech-evolve | custom
    pack: str                      # 'officeqa' | 'spreadsheet' | 'toy'
    l1_tools: bool                 # pack CLIs in the kit + README usage
    controller: bool               # L2 ledger + L3 loop guard + L4 submit/rules in the broker
    seed: Seed                     # seed knowledge form: none | text notes | executed rules (+ minimal text)
    learner: Learner | None        # None = static arm (no release, no learning)
    notes: bool                    # scoped notes rendered per episode (text-* and mech-* arms)

    def validate(self) -> None:
        if self.controller and self.seed == 'text':
            raise ValueError('controller arms carry their seed as rules')
        if self.learner == 'mechanism' and not self.controller:
            raise ValueError('a mechanism learner needs the controller runtime')
        if self.learner == 'text' and self.controller:
            raise ValueError('text learners run without the controller (factorial cell)')
        if self.notes and not self.l1_tools:
            raise ValueError('notes arms also receive the shared L1 tools')


STANDARD_ARMS: dict[str, ArmSpec] = {
    'plain':       ArmSpec('plain', 'officeqa', False, False, 'none', None, False),
    'text-static': ArmSpec('text-static', 'officeqa', True, False, 'text', None, True),
    'text-evolve': ArmSpec('text-evolve', 'officeqa', True, False, 'text', 'text', True),
    'mech-static': ArmSpec('mech-static', 'officeqa', True, True, 'rules', None, True),
    'mech-evolve': ArmSpec('mech-evolve', 'officeqa', True, True, 'rules', 'mechanism', True),
}

# ---------------------------------------------------------------------------------------------
# Ledger events (broker-owned; one JSON object per line in <run>/api/ledger.jsonl)
# ---------------------------------------------------------------------------------------------

EventKind = Literal['command', 'observe', 'compute', 'candidate', 'note', 'compaction']


@dataclass
class Event:
    """Base event. `ref` is unique within an episode: c<n> observe, k<n> compute, a<n> candidate,
    x<n> command, n<n> note (harness text shown to the model), z<n> compaction."""
    kind: EventKind
    ref: str
    call: int                      # broker call number (api/NNN) whose input/output carried it
    t: float                       # seconds since actor start

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


@dataclass
class Command(Event):
    cmd: str = ''                  # normalised shell command text (whitespace collapsed)
    raw_cmd: str = ''              # the command text exactly as issued (≤ 8 KB), for pack parsers
    tool: str = ''                 # Codex tool name (exec_command, write_stdin, …); loop guard skips polls
    call_id: str = ''              # Codex call_id of the function_call
    n: int = 1                     # how many times this exact normalised command has been issued so far
    exit_code: int | None = None
    output_sha256: str = ''        # of the normalised output the model saw (Chunk ID / Wall time stripped)
    output_bytes: int = 0
    truncated: bool = False        # Codex truncation marker present in what the model saw


@dataclass
class Observe(Event):
    """A value (or text span) the model saw in a tool output. `locator` is pack-defined but always
    a stable, citable string (OfficeQA: 'treasury_bulletin_2004_03.txt:741')."""
    source: str = ''               # x<n> of the command that produced it
    locator: str = ''
    text: str = ''                 # raw cell / span text as displayed (≤ 200 chars)
    value: float | None = None     # parsed numeric value when the span is a number
    attrs: dict[str, Any] = field(default_factory=dict)  # namespaced: 'oq.table', 'oq.period', 'oq.column',
                                                         # 'oq.flags' (list), 'oq.repair', 'oq.identity_fail', ...


@dataclass
class Compute(Event):
    source: str = ''               # x<n> of the calc command
    code: str = ''                 # the executed code as printed by `calc` (≤ 8 KB)
    code_sha256: str = ''
    output: str = ''               # printed result (≤ 2 KB)
    values: list[float] = field(default_factory=list)   # numbers printed in the result
    inputs: list[dict[str, Any]] = field(default_factory=list)
    # inputs: [{'literal': '1234.5', 'value': 1234.5, 'span': [start, end], 'observe': 'c17' | None,
    #           'match': 'exact'|'rounded'|'scaled'|None, 'scale': k (10^k for 'scaled'),
    #           'trivial': True (ints ≤ 12, powers of ten; never linked/flagged), 'in_string': True}]
    # values excludes output lines carrying a Codex truncation marker; literals on marked code lines are omitted.


@dataclass
class Candidate(Event):
    answer: str = ''               # as submitted (text answers)
    artifact_sha256: str = ''      # artifact answers (packs with answer_kind == 'artifact')
    alt: str = ''                  # the model's own alternative, if given
    reading: str = ''              # the model's stated reading/assumption
    keep: str = ''                 # non-empty when the model keeps an answer against a finding
    via: Literal['submit', 'final', 'fallback'] = 'submit'


@dataclass
class Note(Event):
    reason: Literal['loop_guard', 'ledger', 'gate', 'enforce'] = 'loop_guard'
    text: str = ''
    sticky: bool = True


@dataclass
class Compaction(Event):
    request_call: int = 0          # the tool-less compaction request
    summary_call: int = 0          # the first request carrying the summary


EVENT_TYPES: dict[str, type[Event]] = {'command': Command, 'observe': Observe, 'compute': Compute,
                                        'candidate': Candidate, 'note': Note, 'compaction': Compaction}


def event_from_json(line: str) -> Event:
    data = json.loads(line)
    return EVENT_TYPES[data['kind']](**data)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

# ---------------------------------------------------------------------------------------------
# Question spec (pack-computed, used by rules)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class QuestionSpec:
    n_values: int | None = None            # number of values the answer must contain, if stated/implied
    list_format: bool = False              # bracketed / comma-separated list requested
    decimals: tuple[int | None, ...] = ()  # per value, when stated
    percent_form: Literal['percent', 'decimal', 'unspecified'] = 'unspecified'
    value_type: Literal['number', 'integer', 'year', 'text', 'date', 'artifact'] = 'number'
    periods: tuple[str, ...] = ()          # pack-normalised periods named by the question (e.g. 'FY1955', 'CY2010', '1970-03')
    pins_date: bool = False                # the question pins a publication/issue date (vintage convention)
    allowed_words: tuple[str, ...] = ()    # quoted words the answer may contain as values (e.g. 'surplus'/'deficit')
    keywords: tuple[str, ...] = ()         # normalised content words (for note triggers and table matching)

# ---------------------------------------------------------------------------------------------
# Rules, predicates, offers, findings
# ---------------------------------------------------------------------------------------------


@dataclass
class Binding:
    """What a predicate matched; templates and offers read it. Keys are free but documented per predicate."""
    values: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuleContext:
    question: str
    spec: QuestionSpec
    events: Sequence[Event]
    candidate: Candidate                  # the submission being checked
    pack: 'Pack'
    runner: 'ComputeRunner'


# A predicate returns the bindings it matched (empty list = no match). Pure, deterministic, ≤ 2 s.
# Calling convention: pred(ctx: RuleContext, **args) -> list[Binding]
Predicate = Callable[..., list[Binding]]
# An offer turns one binding into an alternative answer (or None if it cannot).
# Calling convention: offer(ctx: RuleContext, binding: Binding, **args) -> OfferResult | None
Offer = Callable[..., 'OfferResult | None']


@dataclass
class OfferResult:
    answer: str                           # formatted per QuestionSpec (same rounding as the candidate)
    derivation: str                       # ≤ 200 B, e.g. 'k3 re-run with c41 (2004_03:745) for c17'
    compute_sha256: str = ''              # of the substituted code actually executed


@dataclass
class Rule:
    """Declarative rule; serialised as rules/<id>.json in a mechanism adapter."""
    id: str
    when: list[dict[str, Any]]            # [{'pred': name, 'args': {...}}]; all must match (AND); bindings are joined on shared keys
    finding: str                          # ≤ 200 B template, fields like {cell.locator}, {alt.answer}
    offer: dict[str, Any] | None = None   # {'op': name, 'args': {...}}
    priority: int = 50                    # lower = shown first
    enabled: bool = True
    triggers: list[str] = field(default_factory=list)   # optional question keywords (no regex)
    scope: Literal['core', 'pack'] = 'core'
    origin: Literal['seed', 'learned'] = 'seed'
    admitted_block: int = 0
    stats: dict[str, Any] = field(default_factory=dict)  # admission/replay numbers (never answer values)


@dataclass
class Finding:
    rule: str
    text: str                              # rendered template (≤ 200 B)
    offer: OfferResult | None = None
    bindings: dict[str, Any] = field(default_factory=dict)


@dataclass
class GateResult:
    status: Literal['accepted', 'findings', 'kept', 'rejected']   # 'rejected' = unusable payload (not a gate round)
    findings: list[Finding]
    message: str                           # what `submit` prints (≤ 4 lines per finding; one finding per round)
    candidate_ref: str = ''


class ComputeRunner(Protocol):
    def run(self, code: str, *, timeout_s: float = 5.0) -> tuple[bool, str]:
        """Execute model-authored Python in the isolated runner (no network, no env, CPU 2 s); returns (ok, stdout)."""


class Engine(Protocol):
    def evaluate(self, ctx: RuleContext, rules: Sequence[Rule]) -> list[Finding]: ...

# ---------------------------------------------------------------------------------------------
# Controller hook (broker side). Mirrors understand/broker.md §6.1 with v9 changes.
# ---------------------------------------------------------------------------------------------

RequestKind = Literal['turn', 'compaction', 'resume']


@dataclass(frozen=True)
class View:
    codex_request: int                     # ordinal of the Codex HTTP POST
    kind: RequestKind
    codex_input: Sequence[dict]            # items exactly as Codex sent them (never mutated)
    tools_offered: bool
    elapsed_s: float
    remaining_s: float | None
    call: int = 0                          # the api/NNN number the (next) upstream call is sent as
    # Brokers also call the optional duck hooks controller.bind_call(n) before each upstream call and
    # controller.drain_log() -> list[dict] after each request (rows go to api/controller.jsonl).


@dataclass(frozen=True)
class Insert:
    reason: Literal['loop_guard', 'ledger', 'enforce']
    text: str                              # rendered as role:user input_text, prefixed '[harness:<reason>] '
    anchor: Literal['tail', 'summary'] = 'tail'
    sticky: bool = True


@dataclass(frozen=True)
class Verdict:
    action: Literal['forward', 'revise']
    feedback: str | None = None


class Controller(Protocol):
    def on_request(self, view: View) -> list[Insert]: ...
    def on_response(self, view: View, response: dict, call: int) -> None: ...     # feeds the ledger
    def on_final(self, view: View, response: dict, round: int) -> Verdict: ...    # submit enforcement
    def on_submit(self, payload: Mapping[str, Any]) -> GateResult: ...           # POST /v1/submit
    # optional: def bind_call(self, call: int) -> None; def drain_log(self) -> list[dict]

class LedgerAPI(Protocol):
    """Implemented by harness_core/ledger.py (stream S4); consumed by the broker controller (S3).
    Persists every event to <run>/api/ledger.jsonl as it is added. A Command is written twice under
    the same ref (when its function_call is relayed, and again with exit code/output hash when its
    output arrives): readers fold lines by ref, keeping the last line in first-seen order
    (harness_core.ledger.load_events), so the first N lines reproduce the state when line N was written."""
    def ingest_request(self, call: int, codex_input: Sequence[dict], t: float, *,
                       tools: Sequence | None = None) -> None:
        """Read function_call_output items not seen before (match call_id to the Command), normalise
        the output as the model saw it, derive pack observations; detect a compaction resume."""
    def ingest_response(self, call: int, response: dict, t: float) -> None:
        """Record function_call items as Command events (normalised cmd, running count n)."""
    def add(self, event: Event) -> Event: ...
    @property
    def events(self) -> Sequence[Event]: ...
    def command_count(self, cmd: str) -> int: ...
    def next_ref(self, kind: str) -> str: ...  # fresh ref for an event kind (Ledger.add also assigns one when ref == '')
    def candidates(self) -> list[Candidate]: ...
    def render(self, max_bytes: int) -> str:
        """Deterministic text for the post-compaction splice: candidates, computes with linked
        inputs, most recent observations; never more than max_bytes (UTF-8)."""


class GateAPI(Protocol):
    """Implemented by harness_core/gate.py (S4); consumed by the controller (S3)."""
    def submit(self, payload: Mapping[str, Any], ledger: LedgerAPI, t: float, call: int) -> GateResult: ...
    def check_final(self, final_text: str, ledger: LedgerAPI, t: float, call: int) -> GateResult:
        """Used by enforcement when the final message was never submitted: records a Candidate(via='final')
        and evaluates the rules; the controller turns the result into re-query feedback. `final_text` is the
        answer span (pack extraction / last line), not the whole prose message. Records no Note: the
        controller records the feedback it actually sends."""
    def needs_enforcement(self, final_text: str, ledger: LedgerAPI) -> bool:
        """True when no submission exists whose numeric tokens equal those of final_text."""

# ---------------------------------------------------------------------------------------------
# Submit route (actor CLI -> broker)
# ---------------------------------------------------------------------------------------------

# `calc` (harness_core/tools/calc.py, generic) prints exactly this frame so the ledger can parse it:
#   ⟦calc sha=<first 12 hex of sha256(code)>⟧
#   <the code, verbatim, ≤ 8 KB (longer: first 8 KB + '… [truncated]')>
#   ⟦result⟧
#   <stdout of the code, ≤ 2 KB>
#   ⟦end⟧
CALC_OPEN, CALC_RESULT, CALC_END = '⟦calc sha=', '⟦result⟧', '⟦end⟧'
# Observe.attrs key for spans that contain several numbers (raw rg/sed/read lines): 'values' -> list[float].
# Grounding predicates check Observe.value and Observe.attrs['values'].
VALUES_ATTR = 'values'

SUBMIT_PATH = '/v1/submit'
SUBMIT_FIELDS = {'answer', 'alt', 'reading', 'keep', 'artifact_sha256'}
SUBMIT_ENV = 'HARNESS_SUBMIT_URL'          # set in the actor environment for controller arms only

# ---------------------------------------------------------------------------------------------
# Pack protocol
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    item_id: str
    ordinal: int
    question: str
    split: str = 'test'


class Pack(Protocol):
    name: str
    answer_kind: Literal['text', 'artifact']
    tools: Mapping[str, str]               # CLI name -> 'module:function' inside the pack

    def question_spec(self, question: str) -> QuestionSpec: ...
    def extract_final(self, final_message: str) -> str: ...   # the answer span of a final message
    def canonical(self, answer: str) -> str: ...
    def numbers(self, text: str) -> list[float]: ...
    def observations(self, command: str, output: str, *, source: str, call: int, t: float,
                     next_ref: Callable[[], str], raw_command: str = '') -> list[Observe]: ...
    # optional attributes: handles_truncation: bool (receive output with Codex truncation markers kept),
    #                      max_observations: int (per output; the ledger's default cap is 400)
    def predicates(self) -> Mapping[str, Predicate]: ...
    def offers(self) -> Mapping[str, Offer]: ...
    def seed_rules(self) -> list[Rule]: ...
    def seed_text(self) -> str: ...        # the same seed knowledge written as text (text arms), ≤ 4 KB
    def seed_notes(self) -> list['NoteSpec']: ...
    def usage(self) -> str: ...            # README usage text of the pack tools (arms with l1_tools)
    def leak_screen(self, text: str, released_answers: Sequence[str]) -> list[str]: ...
    def task_conventions(self) -> str: ... # published task conventions for the README (all arms)

# ---------------------------------------------------------------------------------------------
# Adapter layouts (W0 and learned revisions)
# ---------------------------------------------------------------------------------------------
#
# text arms:      adapter.json {"architecture": "text", "contract": CONTRACT_VERSION}
#                 notes/<id>.json   (Note specs, below)
# mechanism arms: adapter.json {"architecture": "mechanism", "contract": CONTRACT_VERSION}
#                 notes/<id>.json, rules/<id>.json (Rule), policy.json (POLICY_DEFAULTS keys), SEED.md
#
# Budget: rendered model-visible text (notes shown + SEED.md + every rule `finding` template) ≤ TEXT_BUDGET.

TEXT_BUDGET = 16_384
NOTE_MAX = 600
NOTES_PER_EPISODE = 3


@dataclass
class NoteSpec:
    id: str
    triggers: list[str]                    # lowercase keywords/phrases; '*' = always
    text: str                              # ≤ NOTE_MAX bytes
    admitted_block: int = 0
    origin: Literal['seed', 'learned'] = 'seed'
    stats: dict[str, Any] = field(default_factory=dict)


POLICY_DEFAULTS: dict[str, Any] = {
    'loop_threshold': 3,                   # identical normalised command issued this many times -> note
    'loop_notes_max': 3,                   # after this many loop notes, escalate wording (if a fresh candidate exists)
    'gate_findings_per_round': 1,
    'gate_rounds_max': 2,                  # distinct findings shown per candidate value before auto-accept
    'enforce_requery_max': 1,              # broker re-queries when the final message was never submitted
    'enforce_min_remaining_s': 60,
    'ledger_splice_bytes': 8_000,
    'disabled_rules': [],
}
POLICY_RANGES: dict[str, tuple[int, int]] = {
    'loop_threshold': (3, 6), 'loop_notes_max': (1, 5), 'gate_findings_per_round': (1, 1),
    'gate_rounds_max': (0, 3), 'enforce_requery_max': (0, 1), 'enforce_min_remaining_s': (30, 300),
    'ledger_splice_bytes': (2_000, 8_000),
}
