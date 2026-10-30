# Recurrent harness

Your input may contain ranked reflections from previous instances of this stream, retrieved for the current
instance, before the current task and observations. They are supplied separately as:

```
[RANKED REFLECTIONS]
[RANK 1 OF K REFLECTION]
{reflection}
...
```

Each reflection separates operational evidence (what worked, what failed, any recovery) from interpretations and
reusable strategies, and may carry use-case guidance that says when it applies. Use what matches the current task
to maximize reward.
