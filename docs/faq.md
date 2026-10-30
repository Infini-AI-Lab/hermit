# FAQ

<details class="ha-faq" id="how-is-a-harness-adapter-different-from-a-plugin">
<summary>How Is a Harness Adapter Different from a Plugin?</summary>

A plugin packages capabilities for an agent to use. A harness adapter adds a training contract: what the learner may change, what stays fixed, and how candidate updates are checked. Hermit uses that contract to train and version the adapter from task feedback.

The two can overlap: a compatible harness adapter can be distributed as a native plugin. The training contract belongs to the adapter and framework; installing the plugin does not start training in the host. See the [adapter concept guide](framework/what-is-a-harness-adapter.md).

</details>

<details class="ha-faq" id="what-can-a-harness-adapter-contain">
<summary>What Can a Harness Adapter Contain?</summary>

A harness adapter can contain instructions, skills, memory, tools, executable code, and agent coordination logic. It can be an entire repository implementing an external harness.

Its specification defines how the agent uses it, which parts can be trained, and how updates are checked—while keeping the original model and harness unchanged.

</details>

<details class="ha-faq" id="why-not-evolve-the-original-harness-directly">
<summary>Why Not Evolve the Original Harness Directly?</summary>

Many harnesses are closed-source. Even open-source harnesses have carefully designed component interactions that direct edits can disrupt. Harness adapters keep learned changes in an external layer while leaving the original harness and model fixed.

This requires an extension point through which the agent can consume the adapter. Changes to inaccessible host internals cannot automatically be expressed as an adapter. See [using and training adapters](framework/adapter-lifecycle.md).

</details>

<details class="ha-faq" id="when-should-i-use-hermit">
<summary>When Should I Use Hermit?</summary>

There are three starting points:

- **Use a trained adapter:** [deploy it](tutorials/deploy-an-adapter.md) to a compatible agent without running training.
- **Adapt your agent to your tasks:** bring an acting agent, tasks, and a way to score outputs; start with an existing adapter template and [learning method](methods/index.md).
- **Develop an evolution method:** implement an [Evolver](framework/custom-evolver.md) and reuse the training loop, task pipeline, checks, and run records.

Start with the [offline Quick Start](get-started/quickstart.md) to try the complete loop without model access.

</details>

<details class="ha-faq" id="what-changes-during-training">
<summary>What Changes During Training?</summary>

The Evolver revises files declared learnable in the adapter's specification. Depending on the template, these can include instructions, skills, roles, memory, or tool implementations. The Trainer checks the result against the specification and records the accepted adapter version.

The acting agent's model weights and original harness stay fixed. The **acting agent** uses the adapter to attempt tasks; the **Evolver** is the learner that updates it. Codex or Claude Code can serve as a learner, while other Evolvers implement their own optimization logic. See [adapter specifications](framework/adapter.md) and the [Evolver interface](framework/evolver.md).

</details>

<details class="ha-faq" id="do-i-have-to-train-an-adapter-before-using-it">
<summary>Do I Have to Train an Adapter Before Using It?</summary>

No. You can use a manually written adapter or an existing trained one. `hermit deploy` generates a supported host workspace; `hermit convert` creates a native plugin package. Neither command launches training or starts the acting agent.

Follow [Deploy an adapter](tutorials/deploy-an-adapter.md) for the commands and required dependencies.

</details>

<details class="ha-faq" id="can-i-reuse-an-adapter-across-models-and-harnesses">
<summary>Can I Reuse an Adapter Across Models and Harnesses?</summary>

Yes, when the receiving agent supports the exports the adapter needs. Hermit currently generates workspaces for Codex, Claude Code, and Pi, and converts native plugin packages for Claude Code, Codex, Pi, and Hermes. Each route has different capabilities; a target may refuse unsupported exports.

Compatibility does not guarantee the same performance on a different model, harness, or task. Evaluate the adapter in its new setting. See the [host capability matrix](framework/commands.md#what-each-hosts-plugin-carries) and [compatibility limits](reference/compatibility.md).

</details>

<details class="ha-faq" id="which-harness-evolution-methods-can-i-use">
<summary>Which Harness Evolution Methods Can I Use?</summary>

Hermit includes GEPA, SkillOpt, MetaWorld Model (MWM), Harness², Continual Harness, ReHarness, and Meta-Harness, along with Codex/Claude coding-agent learners and a prompt-only baseline. They share the Trainer interface, but have different data requirements and compatible adapter templates.

Use the [method selection guide](methods/index.md) to choose a method and the [recipe catalog](recipes/index.md) for runnable benchmark setups. Methods labeled **Coming Soon** in the README are planned integrations.

</details>

<details class="ha-faq" id="how-do-i-know-whether-training-helped">
<summary>How Do I Know Whether Training Helped?</summary>

Compare the trained adapter against both the starting adapter and no adapter on held-out tasks, using the same acting model, harness, and scoring protocol. Include learning and evaluation costs when comparing methods.

The Trainer's acceptance checks establish structural validity; they do not guarantee a score improvement. Candidate selection depends on the Evolver and experiment protocol. See [evaluation](guides/evaluation.md) and [run records](guides/runs.md) for scores, changes, and recorded usage.

</details>
