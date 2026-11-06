"""What a Harbor trial's `result.json` (or a step's result) says about the rollout: status, failure cause, exception,
reward, and whether the run must halt.

Harbor 0.25.0's error policy (harbor/models/trial/error_policy.py) maps exception types to failure causes. It is copied
here so that Hermit's side never imports Harbor: Harbor lives in its own venv (Python 3.13). An unlisted exception
type is `unclassified`.

    status       condition                                              score  tokens
    ok           no exception, a verifier reward                        reward n_input_tokens + n_output_tokens
    timeout      AgentTimeoutError (the verifier may still have passed)  0      None
    infra        sandbox / model provider / verifier / harbor failure,   0      None
                 harbor killed by the wait budget, trial dir missing
    agent_error  unclassified (an agent exit with an unmatched message)  0      None
"""
from __future__ import annotations

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
}.items() for name in names}
INFRA_CAUSES = {"sandbox_failed", "model_provider_failed", "verifier_failed", "harbor_failed", "cancelled",
                "harbor_timeout", "halted", "disk", "stream_lost", "stream_order"}
HALTING = {"ApiUsageLimitError", "AgentAuthenticationError"}       # nothing after this trial can succeed


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
    return _verdict("infra" if cause in INFRA_CAUSES else "agent_error", cause, exc_type, message,
                    float(reward) if reward is not None else None, halt=exc_type in HALTING)


def no_trial(cause: str, message: str, *, halt: bool = False) -> dict:
    """An infra verdict for a rollout that never reached a container (the halt sentinel, the disk gate, a lost
    stream)."""
    return _verdict("infra", cause, None, message, None, halt=halt)
