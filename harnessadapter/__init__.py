"""MetaHarnessAdapter: train harness adapters (template, Evolver, Trainer, evolving task) and deploy them into hosts."""
__version__ = "0.2.0"


def __getattr__(name):
    if name == "Session":       # `from harnessadapter import Session`, without importing the adapter package eagerly
        from harnessadapter.adapter.session import Session
        return Session
    raise AttributeError(name)
