"""The evidence the MWM learner reads: how a venue's episodes become records in the evidence store.

The method decides the shape (one record per episode under blockNNN/, one entry per episode for the block's
listing, a README.md at the store's root, the observation cap); each venue module turns that venue's raw turns into
it. The acting side writes the records as episodes finish (the host's own `trace` and `recall` read them within a
block), the Evolver's session reads them at /evidence.

    readme            the store's README.md, from the venue's env_evidence text
    record            the observation cap shared by every venue
    clbench, poker    CL-Bench's database and poker venues (turn records from the host, in the official-trace shape)
    servelearnbench   ServeLearnBench episodes (the benchmark's trace)
"""
