"""What a Harbor trial's `result.json` (or a step's result) says about the rollout: status, failure cause, exception,
reward, and whether the run must halt.

Harbor 0.25.0's error policy (harbor/models/trial/error_policy.py) maps exception types to failure causes. It is copied
here so that Hermit's side never imports Harbor: Harbor lives in its own venv (Python 3.13). An unlisted exception
type is `unclassified`.

    status       condition
    ok           no exception, a verifier reward
    timeout      AgentTimeoutError (the verifier may still have run); a verifier that ran out of its time on the
                 agent's solution (solution_timed_out, with verifier_timeout: agent: runner.verifier_timeout_verdict)
    agent_error  unclassified (an agent exit with an unmatched message: a CLI that exited non-zero, as one does whose
                 first request the provider refused for what the input made it, a prompt too long), a session the
                 model's safeguards ended (agent_refused_task: runner.session_verdict), the adapter's context over
                 the CLI's limit beside the image's own (context_over_limit)
    infra        a sandbox, model provider, verifier or Harbor failure; harbor killed by the wait budget; no trial
                 directory; an unlisted exception whose message is the host's (HOST_FAILURE); harborside's launch of
                 the session failing for a reason not the agent's (launch_failed); a verifier a server kept from its
                 tests (verifier_failed); and what the runner reads from the session or meets before the
                 trial: the plan's usage limit (usage_limit), a lost sign-in (login_failed), a provider that refused
                 the run's configuration before it served any request (model_rejected: a key it does not take, a
                 permission it does not give, a model it does not have, an account it does not bill; no halt), a
                 session that never started or whose tools never came up (session_failed), the adapter's tools not
                 served (tools_failed)

The judge (judge.py) scores the verifier's reward whenever there is one (ok, timeout, agent_error), else 0. An infra
rollout is not the agent's result: the runner marks it `unscored`, and the Trainer keeps it out of every mean. Tokens
and cost are Hermit's reading of the CLI's stream (runner.session); Harbor's own for its built-in agents.
"""
from __future__ import annotations

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
    "harbor_failed": ("ServiceOperationsUnsupportedError", "VercelScopeError", "DspyImportError",
                      "MissingExtraError", "RegradeError"),
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
INFRA_CAUSES = {"sandbox_failed", "model_provider_failed", "verifier_failed", "harbor_failed", "cancelled",
                "harbor_timeout", "halted", "disk", "stream_lost", "stream_order", "launch_failed",
                # read from the session's stream by the runner (runner.session_verdict), or met before the trial
                "session_failed", "tools_failed", "login_failed", "usage_limit", "model_rejected"}
HALTING = {"ApiUsageLimitError", "AgentAuthenticationError"}       # nothing after this trial can succeed
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
# the Python it runs the tests with, 2026-11-08), so its tests did not all run. Infra.
VERIFIER_DISK_FULL = re.compile(r"No space left on device|\(os error 28\)|\[Errno 28\]", re.IGNORECASE)


def verifier_disk_full(stdout: str) -> bool:
    """The host's disk was full while the verifier ran."""
    return bool(VERIFIER_DISK_FULL.search(stdout))


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
    return _verdict("infra" if cause in INFRA_CAUSES else "agent_error", cause, exc_type, message,
                    float(reward) if reward is not None else None, halt=exc_type in HALTING)


def no_trial(cause: str, message: str, *, halt: bool = False) -> dict:
    """An infra verdict for a rollout that never reached a container (the halt sentinel, the disk gate, a lost
    stream)."""
    return _verdict("infra", cause, None, message, None, halt=halt)
