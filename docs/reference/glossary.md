# Glossary

| Term | Meaning |
|---|---|
| Acting agent / actor | The system attempting tasks; implemented by your pipeline |
| Adapter | A directory of context, skills, roles, or tools, with a manifest defining its training contract |
| Template / seed | The starting adapter structure and its fixed/learnable rules; a seed can already contain useful behavior |
| Host | An agent application or custom loop consuming an adapter |
| Pipeline | Your `run(task, adapter)` function that executes one task |
| Judge | Your scorer producing a numeric score and learner-visible feedback |
| Evolver / learner | The method that uses training evidence to revise an adapter |
| Trainer | The outer execution loop, checks, accounting, history, and recovery |
| Learning material | Recorded task inputs, outputs, judge results, and available trajectories supplied to an Evolver |
| Trajectory | Messages, tool interactions, or logs from an acting attempt, when the pipeline supplies them |
| Step / batch | One main rollout batch followed by one Evolver update |
| Epoch | One pass through the configured training split |
| Specification check | Validation against the template's file rules and optional checker; not a performance test |
| Candidate | A proposed adapter version; a method may test multiple candidates inside one step |
| Adapter tree | Git history of versions belonging to a template |
| Checkpoint | A tagged version saved during training |
| Final | The adapter selected at finalization; not universally the highest-scoring checkpoint |
| Learner state | Method-owned files persisted across steps and saved for recovery |
| Session resume | Continuing a learner host session; separate from crash recovery |
| Deployment | Writing adapter exports into a host workspace |
| Conversion | Emitting a native plugin package for a target host |
| MWM block | An ordered group of environment episodes between learner reviews |
| Evidence store | Task-owned episode records consumed by MWM under its native protocol |
