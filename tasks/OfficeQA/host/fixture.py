"""A synthetic OfficeQA-shaped dataset for offline smokes: a few documents,
80 questions whose answers are stated in them, prepared the way the official
data is. Nothing here is benchmark data; manifest.fixture_only says so."""
import argparse
import csv
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data  # noqa: E402

FACTS = [("Zephyr reserve", "25%"), ("Aurora reserve", "50%"), ("Boreal holdings total", "1,250 million"),
         ("Cirrus yield", "3.4%"), ("Delta issuance", "780 million"), ("Echo outlay", "42 million")]


def build(root: Path):
    root = root.resolve()
    source = root / "source"
    corpus = source / "corpus"
    corpus.mkdir(parents=True)
    for i, (name, value) in enumerate(FACTS, start=1):
        lines = [f"Synthetic Treasury Bulletin {2000 + i}", "", f"Table {i}. {name}.", f"The {name} for fiscal year {2000 + i} was {value}.",
                 "", "Other synthetic lines follow.", *[f"Line {k}: filler text about item {k}." for k in range(1, 40)]]
        (corpus / f"treasury_bulletin_{2000 + i}_01.txt").write_text("\n".join(lines) + "\n")
    rows = []
    for i in range(80):
        name, value = FACTS[i % len(FACTS)]
        rows.append({"uid": f"toy-{i:03}", "question": f"According to the synthetic Treasury Bulletin, what was the {name} for fiscal year {2000 + i % len(FACTS) + 1}? (variant {i})",
                     "answer": value, "source_docs": "https://hidden.invalid/oracle", "source_files": "gold-only-hint", "difficulty": "hard"})
    csv_path = source / "fixture.csv"
    with csv_path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    manifest = data._prepare_from_sources(root, csv_path=csv_path, corpus=corpus,
                                          expected_questions=80, expected_documents=len(FACTS))
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    m = build(parser.parse_args().root)
    print({"prepared": True, "questions": m["sample_count"], "documents": m["corpus_document_count"], "fixture_only": m["fixture_only"]})
