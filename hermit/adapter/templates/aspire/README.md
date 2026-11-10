# ASPIRE skill library

The adapter of ASPIRE (Agentic Skills Discovery for Robotics, arXiv:2607.00272): a library of Markdown skill
entries, each a repair a verifier reproduced on an earlier robot task. The acting agent receives ADAPTER.md and,
below it, the whole library (`skills/README.md`, then each skill file). The `aspire` Evolver is the coordinator
that writes it: it distills the actors' findings into proposals, has a fresh actor reproduce each one from the
proposal alone, and admits only the reproduced ones. See docs/methods/aspire.md.
