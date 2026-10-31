# Inventory questions

Each task asks one question about `inventory.csv`, a small table of items, warehouses and unit counts that is copied
into the agent's working directory. The agent answers by running bash commands, mini-swe-agent's only action.

The judge accepts exactly `{"answer": <integer>}`; after a wrong or malformed answer its feedback gives the format
rule and the reference answer. Learn the output format from training feedback. Test tasks are held out from the
learner.
