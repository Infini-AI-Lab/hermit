"""What a task's Harbor environment says about a rollout: status, failure cause, exception, reward. Ported from Hermit's
Harbor runner (hermit/harbor/errors.py, before the runner was retired) with its policy unchanged; the run's verdict of
an environment (hermit.environment.harbor.HarborEnvironments.verdict) is `classify` of what Harbor's trial recorded,
then `verifier_timeout_verdict` and `verifier_verdict`, as the runner read a trial.

Harbor 0.25.0's error policy (harbor/models/trial/error_policy.py) maps exception types to failure causes. It is copied
here so that Hermit's side never imports Harbor: Harbor lives in its own venv (Python 3.13). An unlisted exception
type is `unclassified`. What the policy grades as the agent's result although its cause is the verifier's (GRADED) is
the agent's here too.

    status       condition
    ok           no exception, a verifier reward
    timeout      a verifier that ran out of its time on the agent's solution (VerifierTimeoutError:
                 solution_timed_out; infra with verifier_timeout: infra)
    agent_error  a verifier that left no reward Harbor could read (no reward file, an empty one, one that does not
                 parse: verifier_failed); an unclassified exception whose message is not the host's
    infra        the environment did not come up (sandbox_failed: its build or pull, Docker, the healthcheck; the disk
                 gate, `disk`; the driver's own failure, harbor_failed; a wait past its budget, harbor_timeout); no
                 exception and no reward; an unlisted exception whose message is the host's (HOST_FAILURE); a verifier
                 the host kept from its tests (verifier_failed: its tools never arrived, or the host's disk was full,
                 `verifier_verdict`); a session that never started, or that the provider could not serve
                 (session_failed, model_provider_failed: what the acting agent raised)

An infra rollout is not the agent's result: the run (hermit.trainer.rollout) runs the task again in a fresh
environment (environment.harbor.infra_retries; not a cause that runs no better again, NO_RETRY), marks one that stays
so `unscored`, and the environment judge refuses to score it (hermit.task.verify.Unscored): the run records a failed
run (judge_error), which a learner sees as one and `hermit eval` counts among its errors.
"""
from __future__ import annotations

from pathlib import Path
import re

FAILURE_CAUSE: dict[str, str] = {name: cause for cause, names in {
    "sandbox_failed": ("EnvironmentStartTimeoutError", "SandboxBuildFailedError", "AgentSetupTimeoutError",
                       "HealthcheckError", "SandboxLikelyOutOfMemoryError", "MemoryLimitExceededError",
                       "SSHAccessError", "GKEExecStreamClosedError", "GKEPodNotFoundError", "RuntimeRequestError",
                       "AddTestsDirError", "DownloadVerifierDirError"),
    "model_provider_failed": ("ApiError", "ApiRateLimitError", "ApiInternalServerError", "ApiOverloadedError",
                              "ApiConnectionClosedError", "ApiResponseStalledError", "UnknownApiError",
                              "NetworkConnectionError", "ApiConnectionError", "ApiUsageLimitError",
                              "RateLimitError", "InternalServerError", "ServiceUnavailableError",
                              "APIConnectionError", "APIError", "Timeout", "BadRequestError",
                              "UnprocessableEntityError", "UnsupportedParamsError", "APIResponseValidationError",
                              "BudgetExceededError", "AgentAuthenticationError", "ModelNotFoundError",
                              "ApiProviderResourceNotFoundError", "AuthenticationError", "PermissionDeniedError",
                              "NotFoundError"),
    # (HarborDriverError is Hermit's own: the environment's driver failed, or did not answer, outside Harbor's verifier)
    "harbor_failed": ("ServiceOperationsUnsupportedError", "VercelScopeError", "DspyImportError",
                      "MissingExtraError", "RegradeError", "HarborDriverError"),
    "unclassified": ("NonZeroAgentExitCodeError",),
    "agent_exhausted_budget": ("AgentTimeoutError", "ContextWindowExceededError", "OutputTokenExceededError",
                               "ContextLengthExceededError", "OutputLengthExceededError"),
    "agent_refused_task": ("AgentSafetyRefusalError", "ContentPolicyViolationError"),
    "verifier_failed": ("VerifierTimeoutError", "RewardFileNotFoundError", "RewardFileEmptyError",
                        "VerifierOutputParseError"),
    "cancelled": ("CancelledError",),
    # harborside's own (harborside/hermit.py): what kept the launch plan's session from starting, not the agent's
    # doing; and the deployment's context over the CLI's limit beside the image's own, the adapter's result
    "launch_failed": ("HermitLaunchError",),
    "context_over_limit": ("HermitContextLimitError",),
}.items() for name in names}
DRIVER_ERROR = "HarborDriverError"
INFRA_CAUSES = {"sandbox_failed", "model_provider_failed", "verifier_failed", "harbor_failed", "cancelled",
                "harbor_timeout", "disk", "stream_lost", "stream_order", "launch_failed",
                # read from the session's stream by the runner (runner.session_verdict), or met before the trial
                "session_failed", "tools_failed", "login_failed", "usage_limit", "model_rejected", "staging_failed",
                # this machine's own failure past the guarded steps (runner.HarborAgent.run): a file, a process
                "host_failed",
                # a task Harbor runs for none of Hermit's agents (runner.unsupported): never started
                "unsupported_task"}
# What Harbor grades as the agent's result although its cause is one of INFRA_CAUSES (its policy's retry=False,
# grade=True; its job mean counts a trial without a reward as 0): its verifier group, a verifier that ran out of its
# time (runner.verifier_timeout_verdict) or left no reward it could read (no file, an empty one, one that does not
# parse), unless Hermit's own checks of the verifier's output find the host behind it (runner.verifier_verdict); and
# litellm's 400 and 422, a request of one of Harbor's own agents that the provider refused for what it held (a prompt
# too long, an invalid request), as a Hermit CLI's first request refused for what its input made it is
# (runner.session_verdict).
GRADED = {"VerifierTimeoutError", "RewardFileNotFoundError", "RewardFileEmptyError", "VerifierOutputParseError",
          "BadRequestError", "UnprocessableEntityError"}
HALTING = {"ApiUsageLimitError", "AgentAuthenticationError"}       # nothing after this trial can succeed
# the causes of an infra rollout that run no better again (the runner's NO_RETRY): a disk gate that waited its time out,
# a request the provider refused for the run's configuration (its key, permission, model or bill: no input to the
# session changes it)
NO_RETRY = ("disk", "model_rejected")
INFRA_RETRIES = 2                                                # environment.harbor.infra_retries' default
INFRA_WAIT_S = 300.0                                             # infra_wait_s'
# An exception Harbor does not list whose message shows the host failing, not the agent: the agent's setup could not
# exec in the container (the host's /tmp full, where runc writes each exec's process file; the daemon gone), or the
# task's environment did not come up: Harbor's Docker environment raises a plain RuntimeError "Docker compose command
# failed for environment ..." when an environment command (build, up, stop, down) exits non-zero, e.g. an image whose
# dependencies no longer resolve or a pull Docker Hub refused (the agent's own commands run unchecked and never raise
# it). It is infra, so a rerun runs it again instead of keeping its zero.
HOST_FAILURE = re.compile(r"no space left on device|OCI runtime|Cannot connect to the Docker daemon|"
                          r"^Agent (?:install|setup) failed|^Docker compose command failed for environment",
                          re.IGNORECASE)


# A verifier whose own tools never arrived ran no test: TB2.0's test.sh installs uv from GitHub (`curl -LsSf
# https://astral.sh/uv/<version>/install.sh | sh`) and runs the tests with uvx; with GitHub unreachable the installer
# prints "failed to download https://github.com/astral-sh/uv/releases/download/..." and `uvx` is not found, and the
# script writes reward 0 (2026-11-08: GitHub's "increase in timeouts for some unauthenticated users"). Infra, so a
# rerun runs it again instead of keeping a zero the agent did not earn.
TOOL_DOWNLOAD_FAILED = re.compile(r"^failed to download https?://\S+", re.MULTILINE)
# ... or a server failed the download outright: a 5xx for the installer itself (astral.sh's), so the installer never
# ran and printed nothing of its own (2026-11-08, GitHub answered 504 for hours).
SERVICE_ERROR = re.compile(r"returned error: 5\d\d\b|\b5\d\d\s+(?:Bad Gateway|Service Unavailable|Gateway Time-?out|"
                           r"Internal Server Error)", re.IGNORECASE)
TOOL_MISSING = re.compile(r"\buvx?: command not found|/\.local/bin/env: No such file or directory")


def verifier_unreached(stdout: str, report_exists: bool = False) -> bool:
    """The verifier's tools never arrived (their download failed, its test runner is missing): no test ran. A test
    report (ctrf.json) means the tests ran. A setup that fails the same way every time (a mirror's 404 for a package,
    as Debian's security mirror gives qemu-startup's curl) is no failed download: every run of the task scores it the
    same, so it stays the trial's 0."""
    stdout = stdout or ""
    return (not report_exists and bool(TOOL_MISSING.search(stdout))
            and bool(TOOL_DOWNLOAD_FAILED.search(stdout) or SERVICE_ERROR.search(stdout)))


# A verifier the host's full disk stopped: the shared Docker disk filled while it ran (ENOSPC, e.g. uv could not unpack
# the Python it runs the tests with, 2026-11-08), so its tests never ran. Infra, even where the agent filled it: a
# container's disk is the host's shared Docker disk (its writable layer on Docker's data root), so an agent that fills
# its container's disk fills the host's for every trial on it, and the verifier's output cannot tell the two apart.
VERIFIER_DISK_FULL = re.compile(r"No space left on device|\(os error 28\)|\[Errno 28\]", re.IGNORECASE)


def verifier_disk_full(stdout: str, report_exists: bool = False) -> bool:
    """The host's disk was full while the verifier ran, before its tests did. A test report (ctrf.json) means the
    tests ran: the words in their output are then a test's (a program of the agent's that printed them, a task about
    a full disk), and the trial is the agent's result."""
    return not report_exists and bool(VERIFIER_DISK_FULL.search(stdout or ""))


def _verdict(status: str, cause: str | None, exc_type: str | None, message: str, reward, *, halt: bool = False,
             timed_out: bool = False) -> dict:
    return {"status": status, "failure_cause": cause, "exception_type": exc_type, "exception_message": message or "",
            "reward": reward, "halt": halt, "timed_out": timed_out}


def classify(result: dict | None, *, expired: bool = False, harbor_exit: int | None = None,
             reward_key: str = "reward") -> dict:
    """The verdict of a trial's `result.json` (a Harbor TrialResult or StepResult as a dict), or of its absence."""
    if result is None:
        if expired:
            return _verdict("infra", "harbor_timeout", None, "harbor run exceeded the wait budget and was killed", None,
                            timed_out=True)
        return _verdict("infra", "harbor_failed", None, f"harbor run exited {harbor_exit} without a trial result", None)
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    reward = rewards.get(reward_key)
    exc = result.get("exception_info") or {}
    exc_type = exc.get("exception_type")
    message = exc.get("exception_message") or ""
    if not exc_type:
        if reward is None:                                 # no exception recorded, yet nothing to score
            return _verdict("infra", "verifier_failed", None, f"no verifier reward {reward_key!r} in the result", None)
        return _verdict("ok", None, None, "", float(reward))
    if exc_type == "AgentTimeoutError":
        return _verdict("timeout", "agent_exhausted_budget", exc_type, message,
                        float(reward) if reward is not None else None, timed_out=True)
    cause = FAILURE_CAUSE.get(exc_type, "unclassified")
    if cause == "unclassified" and exc_type != "NonZeroAgentExitCodeError" and HOST_FAILURE.search(message):
        cause = "sandbox_failed"
    infra = cause in INFRA_CAUSES and exc_type not in GRADED
    return _verdict("infra" if infra else "agent_error", cause, exc_type, message,
                    float(reward) if reward is not None else None, halt=exc_type in HALTING)


def no_trial(cause: str, message: str) -> dict:
    """An infra verdict for a rollout that never reached its environment (the disk gate, an environment that did not
    start): no session ran, so it spent nothing (`ran` False: its cost is 0, never unknown)."""
    return {**_verdict("infra", cause, None, message, None), "ran": False}


def verifier_timeout_verdict(verdict: dict, verifier_timeout: str = "agent") -> dict:
    """A verifier that ran out of its time (VerifierTimeoutError) is the agent's result with `verifier_timeout:
    agent`, the default, as Harbor grades it: a timeout on the agent's solution (solution_timed_out), scored its
    reward or 0 and not run again (a solution too slow for its tests, as AlgoTune's verifier times it, or a process
    it left that hung them). With `verifier_timeout: infra`, for a host whose load makes verifiers time out, it is
    infra: run again. Either way the session did not run out of its time: `timed_out` stays false."""
    if verdict.get("exception_type") != "VerifierTimeoutError":
        return verdict
    if verifier_timeout == "infra":
        return {**verdict, "status": "infra", "failure_cause": "verifier_failed", "timed_out": False}
    return {**verdict, "status": "timeout", "failure_cause": "solution_timed_out", "timed_out": False}


def _text(path: Path) -> str:
    """A regular file's text, or "" when it is missing, unreadable or no regular file (never read through a link)."""
    try:
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() and not path.is_symlink() else ""
    except OSError:
        return ""


def verifier_verdict(verdict: dict, verifier_dir: Path | None) -> dict:
    """A rollout whose verifier ran no test is infra: run again, not kept as the agent's result, whether the verifier
    wrote 0, left no reward or ran out of its time. Either the verifier's tools never arrived, their download failed by
    a server (a 5xx, GitHub's release download) so its test runner is missing and it wrote no report
    (verifier_unreached), or the host's disk was full while it ran, before its tests wrote a report
    (verifier_disk_full: with a report, the words are a test's output). A rollout the verifier passed ran its tests;
    one whose verifier never ran (no output of it) stays as it was."""
    if verifier_dir is None or verdict["status"] == "infra" or float(verdict.get("reward") or 0) >= 1:
        return verdict
    out = Path(verifier_dir)
    stdout = _text(out / "test-stdout.txt")
    if verifier_unreached(stdout, (out / "ctrf.json").exists()):
        why = "the verifier's tools never arrived (their download failed), so its tests never ran"
    elif verifier_disk_full(stdout, (out / "ctrf.json").exists()):
        why = "the host's disk was full while the verifier ran (ENOSPC)"
    else:
        return verdict
    return {**verdict, "status": "infra", "failure_cause": "verifier_failed",
            "exception_message": f"{why}: {stdout.strip()[-300:]}"}


def start_cause(kind: str | None, message: str) -> str:
    """The infra cause of an environment that did not start (Harbor's start, its healthcheck, the driver): Harbor's
    policy's cause when it lists one among INFRA_CAUSES, else sandbox_failed (a trial whose environment failed before
    its agent started is infra, whatever raised)."""
    cause = FAILURE_CAUSE.get(kind or "", "unclassified")
    if kind == "TimeoutError":
        return "harbor_timeout"
    if kind == "DriverGone":
        return "harbor_failed"
    return cause if cause in INFRA_CAUSES else "sandbox_failed"


# What an acting agent raises that is not its result (the runner read the same of a trial's session): an environment
# that failed under it (its driver gone, the image's context file unreadable), a session that never started, a provider
# that could not serve it after the agent's own runs again (the Codex agent's `retries`: the runner's busy_retries,
# one allowance over all of a rollout's runs, so it is not run again here), a model endpoint that failed (the answer
# agent's: a 408, 409, 429 or 5xx, or no answer, after the chat client's own retries), and a refusal of the run's
# configuration (a CLI's, read by the Codex agent's stream reader; the answer agent's endpoint's 401-404), which no
# run again changes. (cause, whether the rollout runs again).
PIPELINE_INFRA = ((re.compile(r"\bmodel_rejected\b|chat/completions HTTP 40[1-4]\b"), "model_rejected", False),
                  (re.compile(r"could not be served after"), "model_provider_failed", False),
                  (re.compile(r"before starting a session"), "session_failed", True),
                  (re.compile(r"chat/completions HTTP (?:408|409|429|5\d\d)\b|chat/completions failed: "),
                   "model_provider_failed", True),
                  (re.compile(r"\bEnvironmentUnavailable\b|the Harbor driver|the environment is not started|"
                              r"the image's own context file could not be read"), "harbor_failed", True))


def pipeline_cause(message: str) -> tuple[str, bool] | None:
    """(the infra cause of an acting agent's failure, whether it runs again) by PIPELINE_INFRA, or None: the
    pipeline's own failure, a failed run as any."""
    return next(((cause, again) for pattern, cause, again in PIPELINE_INFRA if pattern.search(message or "")), None)
