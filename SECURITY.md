# Security Policy

## Supported Versions

We actively maintain and release security fixes for the latest minor release of `mcp-diet`.

| Version | Supported |
| ------- | --------- |
| 0.1.x   | Yes       |

## Threat Model & Security Boundaries

Because `mcp-diet` can inspect live MCP servers (`stdio` / HTTP) and act as a transparent `stdio` proxy:

1. **Subprocess execution (`--cmd` / `proxy`)**: `mcp-diet` executes the server command explicitly provided by the user. Never inspect untrusted MCP server commands outside an isolated sandbox or container.
2. **No telemetry / no outbound calls by default**: Offline analysis (`--file`, `diff`, `snapshot`, `demo`) and `stdio` operations make zero network requests. Network access occurs only when you explicitly pass `--url`.
3. **Secret hygiene**: Snapshots and reports serialize tool schemas and metadata, not runtime credentials. Never place API keys inside tool descriptions or default schema values.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for suspected security vulnerabilities.

Use GitHub's [private vulnerability reporting page](https://github.com/264288425a-wq/mcp-diet/security/advisories/new) to contact the maintainers.

We aim to acknowledge reports within **48 hours** and provide a remediation timeline within **7 days**.
