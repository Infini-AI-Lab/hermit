#!/usr/bin/env python3
"""Bounded, read-only discovery of local alternate representations."""

import argparse
import pathlib
import zipfile


def main():
    ap = argparse.ArgumentParser(description="Find local files or archive members matching terms")
    ap.add_argument("terms", nargs="+", help="distinctive case-insensitive terms")
    ap.add_argument("--root", default=".", help="local corpus root")
    ap.add_argument("--extract", metavar="DIR", help="extract matching archive members here")
    args = ap.parse_args()
    terms = [t.lower() for t in args.terms]
    root = pathlib.Path(args.root)
    extract = pathlib.Path(args.extract) if args.extract else None
    seen = 0
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        if all(t in str(path.relative_to(root)).lower() for t in terms):
            print(f"FILE {path}")
            seen += 1
        if not zipfile.is_zipfile(path):
            continue
        try:
            with zipfile.ZipFile(path) as archive:
                for member in archive.infolist():
                    if all(t in member.filename.lower() for t in terms):
                        print(f"ARCHIVE {path}!{member.filename}")
                        seen += 1
                        if extract and not member.is_dir():
                            target = extract / pathlib.PurePosixPath(member.filename).name
                            extract.mkdir(parents=True, exist_ok=True)
                            with archive.open(member) as src, target.open("wb") as dst:
                                dst.write(src.read())
                            print(f"EXTRACTED {target}")
        except (OSError, zipfile.BadZipFile) as exc:
            print(f"SKIP {path}: {exc}")
    if not seen:
        print("NO_MATCH")


if __name__ == "__main__":
    main()
