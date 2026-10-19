"""The generic toolset's prompt texts (experiment 2). COMMON, IMAGES_INTRO, BRIDGE and ADAPTED are verbatim copies of
pipeline.py's constants so that the old arms and the generic arms share them; LOOP_GENERIC replaces pipeline.LOOP for the
generic toolset. prepare.py --toolset generic quotes ACTING_SYSTEM_PROMPT (COMMON + LOOP_GENERIC) verbatim in the learner's
README, so the learner sees exactly what the agent is told. The acting prompt never names node, Playwright, Chromium, the
GPU or a screenshot procedure: the agent is blind until an adapter tells it otherwise."""

COMMON = '''You are creating an original polished offline interactive visual scene. Follow the full task below. Author your own HTML, CSS and JavaScript. The supplied local Three.js 0.180.0 module and core are available at /vendor/three.module.min.js and /vendor/three.core.min.js. Plain Canvas or WebGL is also allowed. No external URLs, fonts, assets, packages, source retrieval or network. You have no access to other runs. Produce a real animated scene, not screenshots of a scene. Do not claim physical validation or aesthetic superiority. Never use evaluator-only behavior.'''

LOOP_GENERIC = '''This is the tool-harness condition. Create index.html using write_file; read_file and replace_text are available for revisions, and run_command runs one shell command inside your workspace. Tools: write_file(path, content) writes a file in your workspace (index.html is the only file that is submitted); read_file(path, start_line, max_lines) returns numbered lines of a file; replace_text(path, old, new) replaces exactly one occurrence; run_command(command, timeout_s) runs `bash -c command` in your workspace with a timeout of at most 300 seconds. Commands have no network access; they can write only inside the workspace and a private /tmp that is discarded after each command. PNG files that a command creates or modifies inside the workspace are returned to you as images in the message that follows (at most five per command, forty per run). You have at most 1800 seconds, 12 model requests, 24 total tool calls and 65,536 cumulative completion tokens. Only your current index.html is submitted; a final text reply does not save code. The brief's inspection paragraph describes the host's final capture after your run; this condition provides no inspection tool. Finish with a short factual summary of actual checks and unresolved limitations.'''

ACTING_SYSTEM_PROMPT = COMMON + "\n\n" + LOOP_GENERIC

IMAGES_INTRO = 'Image files written in your workspace by your latest command or tool call, shown as images. Assess them along with the tool output, then continue your current task within the same budget. No external reviewer has edited this feedback.'

BRIDGE = '''A frozen MetaHarnessAdapter knowledge package learned by GLM 5.3 Flash from its previous harness-assisted attempts follows. Its original context mentions MCP; this host exposes the same read-only wm contract as a Fireworks function tool backed by the native Adapter.call API. This is a transport bridge, not a new memory implementation. Earlier episodes are not accessible as full source files or raw evidence. Only this frozen context and its native read-only knowledge operations are available. Tools, delegates and model weights are not evolved in this experiment. Knowledge can be useful or mistaken; use actual current output to verify it.'''

ADAPTED = '''Instructions learned from earlier attempts at this kind of task follow. They can be useful or mistaken; use actual current output to verify them.'''

ADAPTER_TOOLS_NOTE = 'Tools beyond write_file, read_file, replace_text and run_command come from the learned adapter; their descriptions are the adapter\'s own, and their results can include images.'

WM_SUFFIX = ' This experiment admits only read-only knowledge operations: list, memory, recall, search, read, trace.'

PRIMITIVES = ("write_file", "read_file", "replace_text", "run_command")
