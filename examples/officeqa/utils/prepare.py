#!/usr/bin/env python3
"""Write the SkillOpt paper's OfficeQA split (arXiv 2605.23904: 50 train, 24 validation, 172 test) for this example.

    python prepare.py --csv officeqa_full.csv --docs treasury_bulletins_parsed --split-dir SkillOpt/data/officeqa_id_split

--csv        databricks/officeqa officeqa_full.csv (questions and answers)
--docs       databricks/officeqa treasury_bulletins_parsed/ with transformed/ (text) and jsons/ (page-level parse)
--split-dir  SkillOpt's data/officeqa_id_split (ids, source files and pages, at the paper's dataset revision);
             without it the files are fetched from github.com/microsoft/SkillOpt at the pinned commit.

Writes tasks.jsonl (questions only), splits/, data.json, seed_adapter/ (SkillOpt's initial OfficeQA skill as a
prompt-only adapter) and documents (a link to --docs, what the agent's sandbox is granted) here, and the answers to
.private/answers.jsonl.
"""
import argparse
import csv
import json
from pathlib import Path
import shutil
import urllib.request

HERE = Path(__file__).resolve().parent
SKILLOPT_COMMIT = "79124b37e9a6371e13b753f8bcd7adb1e493ade1"


def split_items(split_dir: str | None, name: str) -> list[dict]:
    if split_dir:
        return json.loads((Path(split_dir) / name / "items.json").read_text())
    url = (f"https://raw.githubusercontent.com/microsoft/SkillOpt/{SKILLOPT_COMMIT}/data/officeqa_id_split/"
           f"{name}/items.json")
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.loads(response.read())


def as_list(value) -> list[str]:
    from skillopt.envs.officeqa.dataloader import _parse_list_field     # SkillOpt's own parsing of these fields
    return _parse_list_field(value)


parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--csv", required=True, type=Path)
parser.add_argument("--docs", required=True, type=Path)
parser.add_argument("--split-dir")
args = parser.parse_args()

docs = args.docs.resolve()
for sub in ("transformed", "jsons"):
    if not (docs / sub).is_dir():
        raise SystemExit(f"{docs / sub} is missing: --docs is treasury_bulletins_parsed/ with transformed/ and jsons/")
# The documents, linked beside the task: the configs grant the agent's sandbox this link (and cover .private/).
link = HERE / "documents"
if link.is_symlink() or link.exists():
    link.unlink()
link.symlink_to(docs, target_is_directory=True)
rows = {r["uid"]: r for r in csv.DictReader(args.csv.open(encoding="utf-8", newline=""))}
private = HERE / ".private"
private.mkdir(mode=0o700, exist_ok=True)
(HERE / "splits").mkdir(exist_ok=True)
tasks, answers, splits, drifted = [], [], {}, []
for name in ("train", "val", "test"):
    items = split_items(args.split_dir, name)
    splits[name] = [item["uid"] for item in items]
    for item in items:
        row = rows[item["uid"]]
        # Every field comes from the CSV, so question, answer and pages agree. Items whose sources differ from the
        # split's (the paper's dataset revision) are listed in data.json as drifted.
        source_files, source_docs = as_list(row["source_files"]), as_list(row["source_docs"])
        if (source_files, source_docs) != (as_list(item["source_files"]), as_list(item["source_docs"])):
            drifted.append(item["uid"])
        tasks.append({"id": item["uid"], "question": row["question"].strip(), "category": item["category"],
                      "source_files": source_files, "source_docs": source_docs})
        answers.append({"id": item["uid"], "answer": row["answer"].strip()})
with open(HERE / "tasks.jsonl", "w") as f:
    f.writelines(json.dumps(t, ensure_ascii=False) + "\n" for t in tasks)
with open(private / "answers.jsonl", "w") as f:
    f.writelines(json.dumps(a, ensure_ascii=False) + "\n" for a in answers)
(private / "answers.jsonl").chmod(0o600)
for name, ids in splits.items():
    (HERE / "splits" / f"{name}.txt").write_text("\n".join(ids) + "\n")
(HERE / "splits" / "smoke_train.txt").write_text("\n".join(splits["train"][:4]) + "\n")
(HERE / "splits" / "smoke_val.txt").write_text("\n".join(splits["val"][:2]) + "\n")
(HERE / "splits" / "smoke_test.txt").write_text("\n".join(splits["test"][:4]) + "\n")
(HERE / "data.json").write_text(json.dumps({"docs": str(docs), "answers": str(private / "answers.jsonl"),
                                             "csv": str(args.csv.resolve()), "drifted_from_split": drifted}, indent=1))

# SkillOpt's starting skill for OfficeQA, as a prompt-only adapter (start_from in the configs).
import hermit
import skillopt
seed = HERE / "seed_adapter"
if seed.exists():
    shutil.rmtree(seed)
shutil.copytree(Path(hermit.__file__).parent / "algorithms" / "templates" / "prompt_only", seed)
(seed / "ADAPTER.md").write_text((Path(skillopt.__file__).parent / "envs" / "officeqa" / "skills" / "initial.md")
                                 .read_text())
print({name: len(ids) for name, ids in splits.items()}, "sources differing from the split:", drifted)
