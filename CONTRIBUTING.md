# Contributing to mcp-diet

Thank you for your interest in contributing to `mcp-diet`!

## Philosophy

- **Zero required runtime dependencies**: Core functionality must work using the Python 3.10+ standard library. Heavy or optional integrations (such as `tiktoken`) belong under `[project.optional-dependencies]`.
- **Actionable diagnostics**: Every lint rule (`DIET001`–`DIET007`) must include a concrete token-savings estimate or actionable remediation guidance.
- **Strong typing & deterministic tests**: All new modules should use `from __future__ import annotations`, full type annotations, and offline-deterministic unit/integration tests.

## Development Setup

```bash
git clone https://github.com/264288425a-wq/mcp-diet.git
cd mcp-diet
python -m venv .venv
# Activate .venv, then:
pip install -e ".[dev]"
```

## Running Checks Locally

```bash
# Unit & integration tests (zero-dependency stdlib runner)
PYTHONPATH=src python -m unittest discover -s tests -v

# Or with pytest / ruff / mypy (when dev extras are installed)
pytest
ruff check src tests
mypy src
```

## Pull Request Guidelines

1. Open an issue first for substantial features or new lint rules.
2. Keep PRs focused and small; include tests for both happy paths and edge cases.
3. Update `CHANGELOG.md` under `## [Unreleased]` when adding user-facing features or fixes.
4. Ensure `PYTHONPATH=src python -m mcp_diet demo` and all test suites pass.
