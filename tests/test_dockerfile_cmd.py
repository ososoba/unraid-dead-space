"""Regression: the Dockerfile CMD must be a valid `dms.cli.serve` argv.

This was caught the hard way — the security pass renamed `--no-access-log`
to `--access-log` (default off), but the Dockerfile CMD still passed the
old flag. argparse rejected it, the container crashed in a restart loop,
and Unraid's Logs/Console buttons couldn't attach to the never-alive
process. This test parses the Dockerfile, extracts CMD, and feeds the
args through serve.main's argparser to make sure they're accepted.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"

_CMD_RX = re.compile(r"^\s*CMD\s+(\[.+?\])\s*$", re.MULTILINE | re.DOTALL)


def _extract_cmd_argv() -> list[str]:
    text = DOCKERFILE.read_text(encoding="utf-8")
    matches = _CMD_RX.findall(text)
    assert matches, "Dockerfile has no CMD line in JSON-array form"
    # Multiple CMDs would mean only the last takes effect; assert just one.
    assert len(matches) == 1, f"expected 1 CMD line, got {len(matches)}"
    return json.loads(matches[0])


def test_dockerfile_cmd_parses_with_serve_argparser() -> None:
    argv = _extract_cmd_argv()
    # First three slots are the python invocation (`python -m dms.cli.serve`).
    assert argv[:3] == ["python", "-m", "dms.cli.serve"], argv[:3]
    serve_args = argv[3:]

    # Reconstruct the parser from serve.main without actually running uvicorn.
    # We can't import-and-call serve.main(serve_args) because uvicorn.run would
    # block, but the parser is a thin wrapper we can rebuild from the source.
    from dms.cli import serve as serve_module

    # Pull the parser out by intercepting argparse.ArgumentParser.parse_args.
    captured: dict = {}

    real_parse = argparse.ArgumentParser.parse_args

    def _spy(self: argparse.ArgumentParser, args=None, namespace=None):
        captured["args"] = real_parse(self, args, namespace)
        # Raise to short-circuit before uvicorn.run.
        raise _StopError()

    class _StopError(Exception):
        pass

    argparse.ArgumentParser.parse_args = _spy
    try:
        with pytest.raises(_StopError):
            serve_module.main(serve_args)
    finally:
        argparse.ArgumentParser.parse_args = real_parse

    ns = captured["args"]
    # Sanity-check the bound values match what the Dockerfile intends.
    assert ns.host == "0.0.0.0"
    assert ns.port == 8765
