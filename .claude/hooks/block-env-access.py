#!/usr/bin/env python3
"""PreToolUse hook: block any tool call referencing .env files.

Blocks: .env, .env.local, .env.production, .env.dev, etc.
Allows: .env.example (template — no secrets).

Inspects all string-valued tool_input fields. Matches standalone .env tokens
only — won't false-positive on .environment, my.env.dev, pyproject.toml, etc.

Output protocol: PreToolUse hooks emit a hookSpecificOutput with
permissionDecision = "deny" + a reason. Exit code stays 0 — the JSON does
the blocking.
"""

from __future__ import annotations

import json
import re
import sys

# Match a standalone .env or .env.<suffix> reference, but NOT .env.example.
#   (?<![A-Za-z0-9_])  - left boundary: not preceded by a word char
#   \.env              - literal .env
#   (?:\.(?!example(?![A-Za-z0-9_]))[A-Za-z0-9_-]+)?
#                      - optional suffix, but the suffix is not 'example'
#   (?![A-Za-z0-9_])   - right boundary: not followed by a word char
FORBIDDEN = re.compile(
    r"(?<![A-Za-z0-9_])\.env(?:\.(?!example(?![A-Za-z0-9_]))[A-Za-z0-9_-]+)?(?![A-Za-z0-9_])"
)

# Keys in tool_input that may carry paths/commands worth scanning.
SCAN_KEYS = (
    "file_path",
    "path",
    "command",
    "pattern",
    "glob",
    "old_string",
    "new_string",
    "content",
    "url",
    "prompt",
)


def _gather_strings(tool_input: dict) -> list[str]:
    out: list[str] = []
    for key in SCAN_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list):
            out.extend(s for s in value if isinstance(s, str))
    return out


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        # Malformed input — don't block; let the harness surface the error.
        return 0

    tool_input = payload.get("tool_input") or {}
    haystacks = _gather_strings(tool_input)
    text = "\n".join(haystacks)

    if FORBIDDEN.search(text):
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "Access to .env files is blocked by project policy "
                        "(.claude/hooks/block-env-access.py). "
                        "Use .env.example for templates. If you genuinely need "
                        "to inspect .env contents, ask the user to do it themselves."
                    ),
                }
            },
            sys.stdout,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
