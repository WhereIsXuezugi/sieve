"""The documentation must describe the code that exists.

These exist because it once did not: the install guide sent people to a config
path the loader never read, the README named the wrong port, and five documented
defaults were simply wrong. Each test here turns one of those into a failure.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from sieve.app import create_app
from sieve.config import Config
from tests.support import offline_config

ROOT = Path(__file__).resolve().parent.parent
DOCS = sorted([*ROOT.glob("*.md"), *(ROOT / "docs").glob("*.md"), *(ROOT / ".github").rglob("*.md")])


@pytest.fixture(scope="module")
def schema(tmp_path_factory):
    app = create_app(offline_config(tmp_path_factory.mktemp("docs")), start_worker=False)
    return app.openapi()


def _api_operations(schema) -> set[tuple[str, str]]:
    return {(m.upper(), p) for p, ops in schema["paths"].items() if p.startswith("/api")
            for m in ops}


# -- the API reference -------------------------------------------------------


def test_api_reference_lists_every_endpoint(schema):
    text = (ROOT / "docs" / "api.md").read_text()
    documented = set(re.findall(r"\| `(GET|POST|PUT|DELETE)` \| `(/api/[^`]+)`", text))
    missing = sorted(_api_operations(schema) - documented)
    assert not missing, f"docs/api.md does not mention {missing}"


def test_api_reference_names_no_imaginary_endpoints(schema):
    text = (ROOT / "docs" / "api.md").read_text()
    documented = set(re.findall(r"\| `(GET|POST|PUT|DELETE)` \| `(/api/[^`]+)`", text))
    imaginary = sorted(documented - _api_operations(schema))
    assert not imaginary, f"docs/api.md documents endpoints that do not exist: {imaginary}"


def test_every_endpoint_has_a_human_summary(schema):
    """Auto-generated summaries are the function name in Title Case."""
    lazy = [f"{m.upper()} {p}: {op.get('summary')}"
            for p, ops in schema["paths"].items() if p.startswith("/api")
            for m, op in ops.items()
            if not op.get("summary") or all(w[:1].isupper() for w in op["summary"].split())]
    assert not lazy, f"give these a real summary: {lazy}"


# -- configuration ---------------------------------------------------------


def _documented_defaults() -> dict[str, str]:
    text = (ROOT / "docs" / "configuration.md").read_text()
    section = text.split("## Deployment config", 1)[1].split("## Where the config file lives", 1)[0]
    return dict(re.findall(r"^\| `([a-z_]+)` \| `([^`]*)`", section, re.M))


def test_documented_defaults_match_the_code():
    """Regression: the docs claimed port 8080, a 12s timeout, a 900s cache and
    more, none of which were true."""
    defaults = Config()
    wrong = []
    for key, documented in _documented_defaults().items():
        assert hasattr(defaults, key), f"configuration.md documents `{key}`, which Config does not have"
        actual = getattr(defaults, key)
        if isinstance(actual, (bool, int, float, str)):
            expected = str(actual).lower() if isinstance(actual, bool) else str(actual)
            if documented.strip('"') != expected and documented != f'"{expected}"':
                wrong.append(f"{key}: documented {documented!r}, actually {actual!r}")
    assert not wrong, "configuration.md disagrees with Config:\n" + "\n".join(wrong)


def test_example_config_values_are_real_keys():
    text = (ROOT / "config.example.toml").read_text()
    keys = re.findall(r"^#?\s*([a-z_]+)\s*=", text, re.M)
    unknown = sorted({k for k in keys if not hasattr(Config(), k)})
    assert not unknown, f"config.example.toml sets keys Config ignores: {unknown}"


def test_example_config_port_matches_default():
    text = (ROOT / "config.example.toml").read_text()
    port = re.search(r"^port\s*=\s*(\d+)", text, re.M)
    assert port and int(port.group(1)) == Config().port


def test_no_document_names_a_stale_port():
    port = str(Config().port)
    offenders = []
    for path in [*DOCS, ROOT / "Dockerfile", ROOT / "docker-compose.yml", ROOT / "config.example.toml"]:
        for number in re.findall(r"127\.0\.0\.1:(\d{4})\b", path.read_text()):
            if number not in {port, "3000"}:  # 3000 is Invidious
                offenders.append(f"{path.relative_to(ROOT)}: {number}")
    assert not offenders, f"these point at a port Sieve does not use: {offenders}"


# -- the command line ------------------------------------------------------


def _cli_commands() -> set[str]:
    from sieve import cli

    parser_holder: dict[str, argparse.ArgumentParser] = {}
    original = argparse.ArgumentParser.parse_args

    def capture(self, *args, **kwargs):
        parser_holder["p"] = self
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = capture
    try:
        try:
            cli.main(["stats"])
        except SystemExit:
            pass
    finally:
        argparse.ArgumentParser.parse_args = original
    parser = parser_holder["p"]
    subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return set(subparsers.choices)


def test_every_documented_command_exists():
    commands = _cli_commands()
    mentioned = set()
    for path in DOCS:
        for block in re.findall(r"```(?:bash|cron|sh)?\n(.*?)```", path.read_text(), re.S):
            # Lookahead so overlapping matches count: in `docker exec sieve sieve
            # doctor` the first "sieve" is a container name, the second the CLI.
            mentioned.update(re.findall(r"(?=(?:^|\s|/)sieve ([a-z]+))", block))
    mentioned.discard("sieve")
    unknown = sorted(mentioned - commands)
    assert not unknown, f"documented but not a command: {unknown}"


# -- links -----------------------------------------------------------------


@pytest.mark.parametrize("path", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_internal_links_resolve(path):
    text = path.read_text()
    broken = []
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        file_part = target.split("#", 1)[0]
        if file_part and not (path.parent / file_part).resolve().exists():
            broken.append(target)
    for target in re.findall(r'(?:srcset|src)="([^"]+)"', text):
        if not target.startswith("http") and not (path.parent / target).resolve().exists():
            broken.append(target)
    assert not broken, f"broken links in {path.relative_to(ROOT)}: {broken}"


def test_operation_ids_are_unique(schema):
    """Regression: one route serving GET and HEAD gave both the same operation
    id, which makes the published schema invalid for client generators."""
    ids = [op.get("operationId") for ops in schema["paths"].values() for op in ops.values()]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, f"duplicate operation ids: {duplicates}"
