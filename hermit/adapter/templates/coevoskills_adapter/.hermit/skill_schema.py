"""Strict, dependency-free schema validation for evolved Agent Skills.

The evolution containers do not consistently include PyYAML, so this module
accepts the small, deterministic YAML subset that the evolution prompt asks an
Agent to produce.  Accepted manifests are a strict subset of the Anthropic
Agent Skills format: a byte-zero frontmatter block with non-empty ``name`` and
``description`` scalar strings, followed by a non-empty Markdown body.

The file is also copied into task containers and executed as a standalone
validator.  Keep imports limited to the Python standard library.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


RESULT_MARKER = "__SKILL_SCHEMA_RESULT__"
ALLOWED_FRONTMATTER_KEYS = {
    "allowed-tools",
    "compatibility",
    "description",
    "license",
    "metadata",
    "name",
}
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TOP_LEVEL_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:[ \t]*(.*))?$")
_YAML_NON_STRING_SCALARS = {
    "false",
    "no",
    "null",
    "off",
    "on",
    "true",
    "yes",
    "~",
}


@dataclass(frozen=True)
class SkillSchemaIssue:
    path: str
    code: str
    message: str


def _issue(path: str, code: str, message: str) -> SkillSchemaIssue:
    return SkillSchemaIssue(path=path, code=code, message=message)


def _parse_required_string(
    raw: str,
    *,
    field: str,
    path: str,
) -> tuple[str | None, list[SkillSchemaIssue]]:
    value = raw.strip()
    if not value:
        return None, [_issue(path, f"empty_{field}", f"`{field}` must be a non-empty string")]
    if value in {"|", ">", "|-", ">-", "|+", ">+"}:
        return None, [
            _issue(
                path,
                f"multiline_{field}",
                f"`{field}` must be a single-line scalar; quote it when it contains punctuation",
            )
        ]

    if value[:1] in {"\"", "'"}:
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return None, [
                _issue(
                    path,
                    f"invalid_{field}_quoting",
                    f"`{field}` has invalid scalar quoting",
                )
            ]
        if not isinstance(parsed, str):
            return None, [
                _issue(path, f"non_string_{field}", f"`{field}` must be a string")
            ]
        value = parsed.strip()
    else:
        if ": " in value or value[:1] in "[{&*!" or value[-1:] in "]}":
            return None, [
                _issue(
                    path,
                    f"ambiguous_{field}_scalar",
                    f"`{field}` must be quoted because its value is ambiguous YAML",
                )
            ]
        if value.lower() in _YAML_NON_STRING_SCALARS:
            return None, [
                _issue(path, f"non_string_{field}", f"`{field}` must be a string")
            ]

    if not value:
        return None, [_issue(path, f"empty_{field}", f"`{field}` must be a non-empty string")]
    return value, []


def validate_skill_markdown(
    content: str,
    *,
    directory_name: str,
    display_path: str,
) -> list[SkillSchemaIssue]:
    """Validate one SKILL.md and return precise, answer-free schema issues."""

    issues: list[SkillSchemaIssue] = []
    if content.startswith("\ufeff"):
        return [
            _issue(
                display_path,
                "byte_order_mark",
                "SKILL.md must start with `---` at byte zero, without a UTF-8 BOM",
            )
        ]
    if not content.startswith("---\n"):
        return [
            _issue(
                display_path,
                "frontmatter_not_at_byte_zero",
                "SKILL.md must start with `---` at byte zero; remove leading blanks or headings",
            )
        ]

    lines = content.splitlines()
    try:
        closing = next(index for index, line in enumerate(lines[1:], 1) if line == "---")
    except StopIteration:
        return [
            _issue(
                display_path,
                "frontmatter_not_closed",
                "YAML frontmatter must end with a standalone `---` line",
            )
        ]

    raw_fields: dict[str, str] = {}
    block_values: dict[str, list[str]] = {}
    active_key: str | None = None
    for line_number, line in enumerate(lines[1:closing], 2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1].isspace():
            if active_key and raw_fields.get(active_key) in {
                "|",
                ">",
                "|-",
                ">-",
                "|+",
                ">+",
            }:
                block_values.setdefault(active_key, []).append(line.strip())
            elif active_key != "metadata":
                issues.append(
                    _issue(
                        display_path,
                        "unsupported_nested_frontmatter",
                        f"line {line_number}: nested YAML is supported only under `metadata`",
                    )
                )
            continue

        match = _TOP_LEVEL_RE.fullmatch(line)
        if not match:
            issues.append(
                _issue(
                    display_path,
                    "invalid_frontmatter_line",
                    f"line {line_number}: expected a top-level `key: value` entry",
                )
            )
            active_key = None
            continue
        key, raw = match.groups()
        active_key = key
        if key in raw_fields:
            issues.append(
                _issue(
                    display_path,
                    "duplicate_frontmatter_key",
                    f"line {line_number}: duplicate `{key}` field",
                )
            )
            continue
        raw_fields[key] = raw or ""
        if key not in ALLOWED_FRONTMATTER_KEYS:
            issues.append(
                _issue(
                    display_path,
                    "unexpected_frontmatter_key",
                    f"line {line_number}: `{key}` is not an allowed Agent Skill frontmatter field",
                )
            )

    for key, values in block_values.items():
        if key == "description":
            # Normalize a valid YAML block scalar to a quoted string for the
            # common scalar checks below. Folded and literal styles differ in
            # whitespace only, which is irrelevant to schema validation.
            raw_fields[key] = repr("\n".join(values))

    for required in ("name", "description"):
        if required not in raw_fields:
            issues.append(
                _issue(
                    display_path,
                    f"missing_{required}",
                    f"frontmatter is missing required `{required}` field",
                )
            )

    name: str | None = None
    if "name" in raw_fields:
        name, name_issues = _parse_required_string(
            raw_fields["name"], field="name", path=display_path
        )
        issues.extend(name_issues)
    if name is not None:
        if len(name) > 64:
            issues.append(
                _issue(
                    display_path,
                    "name_too_long",
                    f"`name` is {len(name)} characters; maximum is 64",
                )
            )
        if not _NAME_RE.fullmatch(name):
            issues.append(
                _issue(
                    display_path,
                    "invalid_name",
                    "`name` must use lowercase letters, digits, and single hyphens only",
                )
            )
        if "anthropic" in name or "claude" in name:
            issues.append(
                _issue(
                    display_path,
                    "reserved_name",
                    "`name` must not contain the reserved words `anthropic` or `claude`",
                )
            )
        if name != directory_name:
            issues.append(
                _issue(
                    display_path,
                    "directory_name_mismatch",
                    f"frontmatter `name` must equal its Skill directory `{directory_name}`",
                )
            )

    if "description" in raw_fields:
        description, description_issues = _parse_required_string(
            raw_fields["description"], field="description", path=display_path
        )
        issues.extend(description_issues)
        if description is not None:
            if len(description) > 1024:
                issues.append(
                    _issue(
                        display_path,
                        "description_too_long",
                        f"`description` is {len(description)} characters; maximum is 1024",
                    )
                )
            if "<" in description or ">" in description:
                issues.append(
                    _issue(
                        display_path,
                        "description_xml",
                        "`description` must not contain XML angle brackets",
                    )
                )

    if not "\n".join(lines[closing + 1 :]).strip():
        issues.append(
            _issue(
                display_path,
                "empty_skill_body",
                "SKILL.md must contain non-empty Markdown instructions after frontmatter",
            )
        )
    return issues


def validate_skill_path(path: Path) -> list[SkillSchemaIssue]:
    display_path = str(path)
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [_issue(display_path, "missing_skill_md", "SKILL.md does not exist")]
    except UnicodeError:
        return [_issue(display_path, "invalid_utf8", "SKILL.md must be valid UTF-8")]
    except OSError as exc:
        return [_issue(display_path, "read_error", f"could not read SKILL.md: {exc}")]
    return validate_skill_markdown(
        content,
        directory_name=path.parent.name,
        display_path=display_path,
    )


def validate_skill_directory(path: Path) -> list[SkillSchemaIssue]:
    return validate_skill_path(path / "SKILL.md")


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: skill_schema.py <path/to/SKILL.md> [...]", file=sys.stderr)
        return 2
    for raw_path in argv:
        for issue in validate_skill_path(Path(raw_path)):
            print(RESULT_MARKER + json.dumps(asdict(issue), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
