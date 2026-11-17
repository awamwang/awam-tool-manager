# awam-tool-manager

[![Publish preflight](https://github.com/awamwang/awam-tool-manager/actions/workflows/publish-check.yml/badge.svg)](https://github.com/awamwang/awam-tool-manager/actions/workflows/publish-check.yml)

> Language: [中文](README.md) · [English](README_EN.md)

Unified tool manager — maintains an inventory of tools installed on your machine and provides a single entry point for registration, search, launching, and capability-routed invocation.

## Quick start

### Install dependencies

```bash
pip install -r requirements.txt
```

> `requirements.txt` pins `mcp>=1.29.0,<2.0.0`.
> mcp 2.x removed the low-level decorator API this project relies on (`@app.list_tools()`, etc.); upgrading will prevent startup.

### Run the MCP server

```bash
cd src
python server.py                  # stdio by default
python server.py --transport http # single long-running HTTP instance (127.0.0.1:8766/mcp)
```

HTTP mode listens on the loopback address only by default and requires a Bearer token:

```bash
# token source precedence: file read by --print-token > AWAM_HTTP_TOKEN env var
python server.py --transport http --print-token
# default token file: %LOCALAPPDATA%\.awam-tool-manager\http-token
```

- Single instance: a second instance on the same port fails to start with a clear error (the port is pre-bound, not guarded by a lock file).
- Non-loopback bind addresses require explicit opt-in via `AWAM_HTTP_ALLOW_REMOTE=1`; this service can launch local programs, so do not expose it to a LAN casually.
- Exposure mode is process-wide: one instance serves a single `EXPOSURE_MODE`.
- Other settings: `AWAM_HTTP_HOST`, `AWAM_HTTP_PORT`, `AWAM_HTTP_TOKEN`; `/health` is unauthenticated for liveness probes.

### Configure in an MCP client

stdio:

```json
{
  "mcpServers": {
    "awam-tool-manager": {
      "command": "python",
      "args": ["<project-root>/src/server.py"]
    }
  }
}
```

(Replace `<project-root>` with the actual path after you clone; `args` needs an absolute path.)

Streamable HTTP (field names vary by client):

```json
{
  "mcpServers": {
    "awam-tool-manager": {
      "url": "http://127.0.0.1:8766/mcp",
      "headers": {
        "Authorization": "Bearer <http-token>"
      }
    }
  }
}
```

## Registered tools

| tool_id | kind | Description |
|---|---|---|
| `everything` | app | Everything file search (returns a launch command) |
| `everything-search-mcp` | mcp | Everything's MCP interface; can route calls such as `search_files` |
| `winget-mcp` | mcp | Official WinGet MCP: query/install/upgrade packages (installs are behind a destructive gate) |
| `quicker` | launcher | Quicker launcher: list/search/run actions |
| `utools` | launcher | uTools launcher: parses plugin asars to list features, wakes them via the `utools://` protocol (fire-and-forget) |

More can be added later: Steam, npm, Scoop, WeGame, PowerToys, etc. Just add a JSON file following [Adding a tool](SKILL.md#新增工具步骤).

## Directory layout

```
data/                  # only hand-maintained sources of truth live in the repo (since A14)
└── tools/             # one JSON per tool entry
src/
├── server.py          # MCP server entry point
├── http_transport.py  # optional Streamable HTTP transport (stdio is still the default)
├── store.py           # JSON storage layer (tools in the repo, caches in the runtime dir)
├── registry.py        # tool registry
├── backend_client.py  # MCP runtime client and connection pool
├── capabilities.py    # Provider / Capability / Runtime routing layer
├── launcher_client.py # launcher adapters (Quicker / uTools)
└── runtime_paths.py   # runtime directory resolution + legacy path migration
```

> Runtime data (MCP capability caches, launcher action lists, runtime launch intents) does not live in the repo;
> it is stored under `%LOCALAPPDATA%\.awam-tool-manager/` (override with `AWAM_RUNTIME_DIR`).
>
> `data/launchers/` and `src/adapters/` are leftovers from an early version, removed during refactoring; they do not exist in the code.

## Documentation

- [Data format spec](docs/format.md)
- [Architecture](docs/architecture.md)
- [MCP protocol conformance comparison](docs/mcp-conformance.md)
- [Code review report](docs/code-review.md)
