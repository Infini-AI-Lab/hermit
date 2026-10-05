"""A fixed tool used to demonstrate the adapter's deployment interface."""


def count_words(text: str) -> dict:
    return {"count": len(text.split())}
