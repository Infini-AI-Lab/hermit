"""A complete optimizer Evolver: greedy reflective rewriting.

Each round runs a train minibatch with the current text, shows the failures
and the grader's feedback to the model, asks for a rewritten text, and keeps
it only if the val score improves. GEPA and SkillOpt are stronger versions of
this loop; the framework supplies everything around it.
"""
from harnessadapter.evolution.optimizer import Outcome


def val_score(context, candidate, label):
    rollouts = context.evaluate(candidate, context.val, label=label)
    return sum(r.score for r in rollouts) / len(rollouts)


def run(context):
    name = context.components[0]                  # the adapter file being optimized (ADAPTER.md)
    best = context.seed
    best_score = seed_score = val_score(context, best, "seed/val")
    size = int(context.settings["minibatch"])
    for i in range(int(context.settings["rounds"])):
        batch = [context.train[(i * size + k) % len(context.train)] for k in range(size)]
        failures = [r for r in context.evaluate(best, batch, label=f"round-{i}/train") if r.score < 1 and not r.infra]
        if not failures:
            continue
        examples = "\n\n".join(f"Answer: {r.output}\nFeedback: {r.feedback}" for r in failures)
        reply = context.model("I provided an assistant with the following instructions to perform a task for me:\n"
                              f"```\n{best[name]}\n```\n\nThese runs failed:\n\n{examples}\n\n"
                              "Write improved instructions inside one ``` block.")
        if reply.count("```") < 2:
            continue
        body = reply.split("```")[1]
        candidate = {**best, name: body.split("\n", 1)[1] if "\n" in body else body}   # drop a language tag
        score = val_score(context, candidate, f"round-{i}/val")
        context.log(f"round {i}: val {score:.2f} (best {best_score:.2f})")
        if score > best_score:
            best, best_score = candidate, score
    return Outcome(best, {"seed_val_score": seed_score, "selected_val_score": best_score})
