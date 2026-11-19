"""What Hermit reads of a CLI session's stream beyond its Base Agent's own `outcome`, for a session in a remote
environment (a task's Harbor environment): for Codex, a lost sign-in or key, a provider that could not serve, the
plan's usage limit and a refusal of the run's configuration, each read from Codex's own notices only, never from what
its log quotes of the session's work; for every CLI, whether a refusal was of the run's configuration or of the
account's bill (`session_verdict`). Ported unchanged from Hermit's Harbor runner (hermit/harbor/signin.py's readers
and the runner's CONFIG_REFUSED, BILLING_REFUSED, QUOTA_SAID and session_verdict), which applied them to every session
in a task's container.
"""
from __future__ import annotations

import json
import re

# Codex's report of a sign-in or a key the provider refused, which no wait restores (the wording of Harbor's
# AgentAuthenticationError, harbor/agents/installed/base.py ERROR_PATTERNS, and Codex 0.164's own for no sign-in at all,
# for the ChatGPT workspace routing a login's requests go through refusing it, and for a refresh of the ChatGPT sign-in
# the provider refused: "Your access token could not be refreshed[ because your refresh token was already used / has
# expired / was revoked]. Please log out and sign in again."), matched as Codex writes it; never in a recovery notice
# (CODEX_RECOVERY), which Codex goes on from; and only since the session last worked
CODEX_AUTH = re.compile(r"Not logged in|\binvalid_api_key\b|Invalid API key\b|API key is invalid\b|"
                        r"Incorrect API key provided\b|unexpected status 401 Unauthorized\b|"
                        r"Invalid proxy server token passed|\btoken_not_found_in_db\b|"
                        r"\bno Codex credentials were found\b|workspace routing discovery unauthorized \(401\)|"
                        r"Your access token could not be refreshed\b")
# ... and a 403, only in the error a turn failed with (`turn.failed`): Codex's reconnect notices of a 403
# ("Reconnecting... unexpected status 403") are common, and recovered from
CODEX_FORBIDDEN = re.compile(r"unexpected status 403\b")
# Codex's notices of an error it recovers from, the session going on: its retry ("Reconnecting... 2/5 (unexpected
# status 401 Unauthorized: ...)") and its fall back from a WebSocket the provider refused to HTTPS
CODEX_RECOVERY = re.compile(r"\s*(?:Reconnecting\.\.\.|Falling back from WebSockets to HTTPS transport\b)")
# Of those, its retry of a request that failed, with what failed (in its parentheses, when it names it); its fall back
# to HTTPS retries none (the request goes on over HTTPS)
CODEX_RETRY = re.compile(r"\s*Reconnecting\.\.\.(?:\s*\d+\s*/\s*\d+)?\s*(?:\((?P<reason>.*)\))?", re.S)
# Codex's own words for a plan that refuses every session until someone acts, which no window's reset ends: a
# workspace's credits spent or its spend cap reached, a member's limit the owner set, a ChatGPT plan without Codex
# (the runner's BILLING_REFUSED: they end the run, as an API account's spent quota does). Its plan's usage limit, which
# the window's reset ends, is the Base Agent's (hermit.agent.base.login.LIMIT: "You've hit your usage limit").
CODEX_PLAN_REFUSED = (r"\bYour workspace is out of credits\b|\bYou(?:'re| are) out of credits\b|"
                      r"\bYou hit your spend cap\b|\breached your usage limit\. Increase your limits\b|"
                      r"\bTo use Codex with your ChatGPT plan, upgrade to Plus\b")
# Codex's failure of a request that no retry changes, in the error its turn failed with: a status it does not retry
# ("unexpected status 402 Payment Required: ..."; a 400, a request setting the model does not take or a prompt too
# long, which the runner's CONFIG_REFUSED tells apart; a 402, the account's bill; a 404, a model or an endpoint not
# there), a provider's error body it gives as it is (a 400 it reads as an invalid request: '{"error": {"message":
# "Unsupported value: ..."}}', '{"detail": "The ... model is not supported when using Codex with a ChatGPT
# account."}'), its own words for an API account's quota (OpenAI's insufficient_quota), and CODEX_PLAN_REFUSED. A 401
# or a 403 is a lost sign-in (CODEX_AUTH, CODEX_FORBIDDEN); a 429 or a 5xx the provider's (the Base Agent's
# UNAVAILABLE).
CODEX_REJECTED = re.compile(r"unexpected status (?:400|402|404)\b|^\s*Quota exceeded\. Check your plan and billing "
                            r"details\b|^\s*\{\s*\"(?:error|detail)\"\s*:|" + CODEX_PLAN_REFUSED, re.M)
# The provider's content filter ending a response ("stream disconnected before completion: Incomplete response
# returned, reason: content_filter"): what the task's or the adapter's input made the request, the agent's result
CODEX_FILTERED = re.compile(r"\bcontent_filter\b")
# The type of each event `codex exec --json` writes: a line of another JSON object inside a record of Codex's log is
# text the record quotes (a file, a command's output), never an event
CODEX_EVENTS = frozenset({"thread.started", "turn.started", "turn.completed", "turn.failed", "item.started",
                          "item.updated", "item.completed", "error"})
# A record of Codex's own log on its stderr ("<time> <LEVEL> <target>: <message>", its target in group 1), after the
# spans the record was logged in ("<time> ERROR session_loop{thread_id=...}:turn{...}: <target>:"). The target is one
# of Codex's own crates' (codex_core::tools::router, codex_exec, ...) or of the MCP client it runs (rmcp::...): a line
# of that shape with another program's target (one a patch or a log file the session read quotes, "<time> ERROR
# server::auth: ...") starts no record, and inside a record it is what the record quoted
CODEX_RECORD = re.compile(r"\d{4}-\d\d-\d\dT\S+\s+(?:TRACE|DEBUG|INFO|WARN|ERROR)\s+"
                          r"(?:(?:[\w.]+(?:\{[^}]*\})?:)+\s+)?((?:codex_\w+|rmcp)(?:::\w+)*)[{:]")
# ... of which those of the session's tool calls (codex_core::tools) quote the model's text: a command it refused or a
# patch whose lines it did not find, each quoted line on a line of its own that starts no record
CODEX_TOOLS = re.compile(r"codex_core::tools(?:::|$)")


def lines(raw: str):
    """A Codex stream's events and Codex's own lines (its stderr) in their order, as (the event or None, the line),
    less the lines that are the session's own work: a record of Codex's log of one of the session's tool calls
    (CODEX_TOOLS) and every line that continues a record (one that starts no record and follows one before the next
    event: the text the record quoted). A line that starts no record and follows none (codex exec's own error, docker's)
    and Codex's other records are kept; so a lost sign-in Codex reports on a line of its own right after a record of
    its log goes unseen, as on main (the trial then scores 0, and the run goes on). Inside a record, a line of a JSON
    object that is no event of Codex's (its `type` not one of CODEX_EVENTS) is what the record quoted. U+0085, U+2028
    and U+2029 end no line (an event's strings may hold them unescaped)."""
    record = None                                   # the target of the record since the last event, if any
    for line in raw.split("\n"):
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):        # no JSON, or nested past the parser's depth
            event = None
        if isinstance(event, dict) and (record is None or event.get("type") in CODEX_EVENTS):
            record = None
            yield event, line
            continue
        if not line.strip():
            continue
        started = CODEX_RECORD.match(line)
        if started is None and record is not None:  # what a record quoted
            continue
        if started is not None:
            record = started.group(1)
        if not CODEX_TOOLS.match(record or ""):
            yield None, line


def codex_errors(event: dict) -> list[str]:
    """An event's error messages, as Codex's Base Agent reads them (hermit.agent.base.cli.Codex.errors)."""
    from hermit.agent.base.cli import Codex
    return [m for m in Codex.errors(event) if m.strip()]


def codex_signin_lost(raw: str) -> bool:
    """Whether the stream of a Codex session that served no turn says its sign-in or key was refused: CODEX_AUTH in a
    line or an error since the session last worked (an item the provider served, a completed turn), a recovery notice
    (CODEX_RECOVERY) aside, or CODEX_FORBIDDEN in the error a turn failed with. A served session lost nothing (the
    caller checks it is served first: Codex.served)."""
    recent, failed = [], []
    for event, line in lines(raw):
        if event is None:                           # a line of its own (its stderr)
            recent.append(line)
            continue
        item = event.get("item") if isinstance(event.get("item"), dict) else None
        warning = item is not None and item.get("type") == "error"
        if event.get("type") == "turn.completed" or (item is not None and not warning):
            recent = []                             # the session worked: it went on from what came before
        said = codex_errors(event)
        if event.get("type") == "turn.failed":
            failed += said
        recent += [m for m in said if not CODEX_RECOVERY.match(m)]
    return bool(CODEX_AUTH.search("\n".join(recent + failed)) or CODEX_FORBIDDEN.search("\n".join(failed)))


def _failure(raw: str) -> tuple[str, bool]:
    """(the last failure of a Codex session that served no turn, whether Codex was retrying when it ended): the error
    its turn failed with, else its last error or line since it last worked (an item the provider served, a completed
    turn), never a notice it went on from (a recovery notice, CODEX_RECOVERY, or a warning, an `error` item); in a
    stream with no event (no session began: a container that could not start, whose `docker run` ends its error with a
    help line), all its lines. Codex was retrying when a retry notice (CODEX_RETRY) came since the session last worked
    and no error of the session's own came after it: none but the error it retried (the one its turn fails with when
    its retries run out) or a recovery notice, so a session that failed on its own after a retry Codex recovered from
    was not retrying."""
    lasting, failed, began, since = [], [], False, []
    retried, own = None, False          # what the last retry since the session last worked retried; an error after it
    for event, line in lines(raw):
        if event is None:                           # a line of its own (its stderr)
            lasting.append(line)
            since.append(line)
            continue
        began = True
        item = event.get("item") if isinstance(event.get("item"), dict) else None
        warning = item is not None and item.get("type") == "error"
        if event.get("type") == "turn.completed" or (item is not None and not warning):
            since, retried, own = [], None, False   # the session worked: it went on from what came before
        said = codex_errors(event)
        if event.get("type") == "turn.failed":
            failed += said
        for message in said:
            retry = CODEX_RETRY.match(message)
            if retry:
                # a retry that names no failure ("Reconnecting... 2/5") leaves `retried` empty: any error after it may
                # be the one it retried, so none is taken for the session's own
                retried, own = (retry.group("reason") or "").strip(), False
            elif retried and not warning and not CODEX_RECOVERY.match(message) and \
                    message.strip() not in retried and retried not in message:
                own = True                          # the session's own failure: Codex went on from the retry
        kept = [m for m in said if not CODEX_RECOVERY.match(m)]
        lasting += kept
        since += [] if warning else kept
    last = failed[-1] if failed else "\n".join(since[-1:] if began else lasting)
    return last, retried is not None and not own


def codex_unavailable(raw: str) -> bool:
    """Whether the stream of a Codex session that served no turn says its provider could not serve it: the Base
    Agent's UNAVAILABLE (hermit.agent.base.cli) in the session's last failure (_failure), or a turn that failed, or a
    stream that ended, while Codex was retrying, unless the sign-in was lost (codex_signin_lost): Codex retries what the
    provider or the network failed, and the error it fails the turn with when its retries run out need not say so
    (Codex 0.164 when chatgpt.com cannot be reached: "Reconnecting... 5/5 (workspace routing discovery failed)", then
    the turn failed with "workspace routing discovery failed"). A last failure the provider's content filter ended
    (CODEX_FILTERED, which Codex writes as a disconnected stream) is none: the agent's result."""
    from hermit.agent.base.cli import UNAVAILABLE
    last, retrying = _failure(raw)
    if CODEX_FILTERED.search(last):
        return False
    return bool(UNAVAILABLE.search(last)) or (retrying and not codex_signin_lost(raw))


def codex_refused(raw: str) -> bool:
    """Whether the stream of a Codex session that served no turn says the plan refused it for its usage limit (the
    Base Agent's words, hermit.agent.base.login.LIMIT), in a line of Codex's own or an error since the session last
    worked, or in the error its turn failed with: never in what Codex's log quotes of the session's work (`lines`),
    which the Base Agent's reading of the whole stream takes for its notices too."""
    from hermit.agent.base.login import refused
    recent, failed = [], []
    for event, line in lines(raw):
        if event is None:
            recent.append(line)
            continue
        item = event.get("item") if isinstance(event.get("item"), dict) else None
        if event.get("type") == "turn.completed" or (item is not None and item.get("type") != "error"):
            recent = []                             # the session worked: it went on from what came before
        said = codex_errors(event)
        if event.get("type") == "turn.failed":
            failed += said
        recent += [m for m in said if not CODEX_RECOVERY.match(m)]
    return refused("\n".join(recent + failed))


def codex_rejected(raw: str) -> str | None:
    """The last failure of a Codex session that served no turn (_failure) when it is a refusal of the run's
    configuration or bill, which no wait or retry changes (CODEX_REJECTED: a 400, 402 or 404 Codex does not retry, an
    API account's quota), else None. Whether a 400 refused the configuration or the request's input is the runner's
    reading (CONFIG_REFUSED), as for the other CLIs."""
    last, _ = _failure(raw)
    return last if CODEX_REJECTED.search(last) else None


# A rejection that refused the run's configuration, which no input to the session changes, as the CLI's own notices
# (the session's `failure`) name it: its key or token (401, unauthorized, a failed authentication, an invalid key or
# token, a key not valid, not logged in, OpenCode's provider.auth), its permission (403, forbidden, no access), its
# model or endpoint (404, not found, a model that does not or may not exist, an invalid or unknown model, one that is
# not a valid model), its account's bill (402, a credit balance too low, an insufficient quota, Codex's "Quota exceeded.
# Check your plan and billing details.", billing; not a rate limit's advice, "billing details" or a link to the billing
# page). A status is read only where one is written: leading
# the text or a line of it (Pi's "401 {...}"), after the prefix Hermit's endpoint gives a CLI's failure in its reply
# ("...(exit 1): 401 {...}", "...(timed out): "; endpoint._no_reply), after "API Error: " (Claude Code's) or
# "Error code: " (OpenAI's SDK), in "(HTTP 401)" (OpenCode's) or in "API error (401)" (Pi's), after "status" ("status
# code", "status_code") or a JSON "code"; never another number of the text (a token count: "you requested 4402 tokens
# (402 in the messages, ...)", "input tokens (402): 403 over"). So is a request setting the model does not take (an
# unsupported parameter or value: max_tokens where it takes max_completion_tokens, an effort it has not; Azure's
# unrecognized request argument; OpenCode's provider.unsupported-operation), and what Hermit's own endpoint refuses a
# CLI session cannot take (its code, tools.UNSUPPORTED: config_refused). Any other rejection (a
# prompt too long, an invalid request, a content filter) is what the adapter's or the task's input made the request:
# the agent's result. (A rate limit or an overload is `unavailable`, read before any rejection.)
CONFIG_REFUSED = re.compile(r"(?:^|\((?:exit -?\d+|timed out)\): |\bAPI error(?:: | \()|\bError code: |"
                            r"(?:\bstatus(?:[ _]code)?|\"code\")\"?:? ?\"?)40[1-4]\b|\(HTTP 40[1-4]\)|"
                            r"unauthori[sz]ed|forbidden|authenticat|\bauth\b|not logged in|"
                            r"invalid[\w -]{0,20}?(?:key|token)\b|\b(?:key|token) not valid|permission|access denied|"
                            r"not have access|not[ _]found|(?:may|does|model) not exist|(?:invalid|unknown)[ _]model|"
                            r"not a valid model|credit balance is too low|insufficient[_ ]?quota|"
                            r"^\s*Quota exceeded\. Check your plan and billing details\b|"
                            r"(?<!/)\bbilling(?! details)|unsupported[ _](?:parameter|value)|model is not supported\b|"
                            r"unrecognized request argument|unsupported[ _-]operation|" + CODEX_PLAN_REFUSED,
                            re.I | re.M)
# ... of them, an account the provider does not bill (its quota or its credit spent, a payment it needs): nothing later
# can be served, as after an API account's usage limit, so it ends the run (usage_limit, a halt)
BILLING_REFUSED = re.compile(r"(?:^|\((?:exit -?\d+|timed out)\): |\bAPI error(?:: | \()|\bError code: |"
                             r"(?:\bstatus(?:[ _]code)?|\"code\")\"?:? ?\"?)402\b|\(HTTP 402\)|payment required|"
                             r"credit balance is too low|insufficient[_ ]?(?:quota|balance|credits?|funds)\b|"
                             r"^\s*Quota exceeded\. Check your plan and billing details\b|(?<!/)\bbilling(?! details)|"
                             + CODEX_PLAN_REFUSED, re.I | re.M)
# ... and OpenAI's code for an API account whose quota or credit is spent, which comes with a 429 (the status of a rate
# limit, which a pause ends: `unavailable`), in the error's code or type (the answer agent's relay records it) or in
# the words a CLI's failure quotes of the reply: nothing later can be served, so it ends the run too
QUOTA_SPENT = "insufficient_quota"
QUOTA_SAID = re.compile(r"\binsufficient_quota\b")


def session_verdict(facts: dict, tokens: int | None) -> str | None:
    """What a session that served nothing says beyond its own result, as the Harbor runner's `session_verdict` read
    it from any CLI's facts (its Base Agent's outcome): "usage_limit" when the provider does not bill the account (a
    refusal of the configuration that is billing's, BILLING_REFUSED, or a provider that could not serve for a quota
    spent, QUOTA_SAID: nothing later can be served, the run ends), "model_rejected" when it refused the run's
    configuration before serving anything (CONFIG_REFUSED, no model refusal, no tokens: never run again), else None:
    a request refused for what the adapter's or the task's input made it (a prompt too long, an invalid request, a
    content filter) and the model's own refusal are the agent's result."""
    if facts.get("served"):
        return None
    failure = str(facts.get("failure") or "")
    if facts.get("unavailable") and not facts.get("refused") and QUOTA_SAID.search(failure):
        return "usage_limit"
    if facts.get("rejected") and not facts.get("model_refusal") and not tokens and CONFIG_REFUSED.search(failure):
        return "usage_limit" if BILLING_REFUSED.search(failure) else "model_rejected"
    return None
