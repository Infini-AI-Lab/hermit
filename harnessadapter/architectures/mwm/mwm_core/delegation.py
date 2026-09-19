"""Original MWM native-delegation text; venue routing belongs outside the core."""

MODES = ("managed", "native", "none")

NATIVE_DOC = (
    "Sub-agent specs are prompts under /world-model/agents/<name>/SPEC.md "
    "(read one with wm read). To run one, start a sub-agent the way your "
    "harness allows and give it the spec's text with your sub-task."
)

NATIVE_CALL_NOTICE = (
    "delegate: not a wm call here — start a sub-agent through your own "
    "harness; the spec's text is at /world-model/agents/{spec}/SPEC.md"
)
