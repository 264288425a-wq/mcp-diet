# Changelog

All notable changes to `mcp-diet` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-23

### Added
- Zero-dependency MCP context token analyzer (`mcp-diet inspect`, `mcp-diet check`, `mcp-diet demo`) supporting live `stdio` servers, Streamable HTTP endpoints, local JSON manifests, and client config files (`mcp.json`, `claude_desktop_config.json`, Codex `config.toml`).
- Seven actionable context-bloat lint rules (`DIET001`–`DIET007`) covering oversized tools, verbose descriptions, deep nesting, large enums, duplicate sub-schemas, server menu-tax budgets, and excessive parameters.
- Snapshot & PR diff gate (`mcp-diet snapshot`, `mcp-diet diff`) with Markdown PR reports and configurable token increase thresholds.
- Runtime payload slimming (`trim_payload`, `@trim_response` decorator for sync/async Python and FastMCP tools, `compact_schema`).
- Transparent MCP `stdio` slimming proxy (`mcp-diet proxy`) capable of compacting `tools/list` schemas and enforcing `tools/call` response token limits.
- Multi-format reporters: terminal table with bar charts, GitHub Markdown, JSON, and SARIF 2.1.0 for GitHub Code Scanning.
- Composite GitHub Action (`action.yml`) and `.pre-commit-hooks.yaml`.
