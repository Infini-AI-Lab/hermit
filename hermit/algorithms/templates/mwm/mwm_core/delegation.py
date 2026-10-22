"""Original MWM native-delegation text; venue routing belongs outside the core."""

MODES = ("managed", "native", "none")

# The "This environment" sentence per mode (native mwm/delegation.py); the
# deployment chooses the mode, the contract renderer appends the sentence.
SENTENCES = {
    "managed": ("A sub-agent here acts in the host's seat: the host's tools "
                "except the one that submits, the host's budgets; its turns "
                "are marked by: <spec> in the record and its trajectory is "
                "under /evidence/delegates/."),
    "native": ("Hosts here start a sub-agent through their own harness, "
               "reading the spec's text themselves; what it can do and where "
               "its record lands are the harness's."),
    "none": "Hosts here cannot start a sub-agent; specs are not served.",
}


def sentence(mode: str) -> str:
    import textwrap
    return textwrap.fill(SENTENCES[mode], width=72)

NATIVE_DOC = (
    "Sub-agent specs are prompts under /world-model/agents/<name>/SPEC.md "
    "(read one with wm read). To run one, start a sub-agent the way your "
    "harness allows and give it the spec's text with your sub-task."
)

NATIVE_CALL_NOTICE = (
    "delegate: not a wm call here — start a sub-agent through your own "
    "harness; the spec's text is at /world-model/agents/{spec}/SPEC.md"
)
