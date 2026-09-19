# Synthetic MWM integration evidence

These fabricated records test software integration, not benchmark performance or hidden-policy discovery. Observations explicitly state policy limits. The initial world model has no learned knowledge.

- Logical record address q1, file /evidence/q1.json: earlier policy at tick 1.
- Logical record address q2, file /evidence/q2.json: revised return window and fee at tick 2.

Use q1 and q2 in derived_from and worked-example names (examples/q1.md, examples/q2.md). Hosts follow an address with wm(name="recall", args={"address":"q1"}) or wm(name="trace", args={"address":"q1","from":0}). A physical evidence filename is distinct from its logical record address.

Ticks order observations of the same fictional shop and operation. No live environment tools are available. Preserve historical values with their dates. These records do not establish boundary inclusivity, rounding behavior, or validity after tick 2.
