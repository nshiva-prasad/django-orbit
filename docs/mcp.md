# MCP Server — AI Assistant Integration

Django Orbit exposes your app's telemetry as an [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server. Connect Claude, Cursor, Windsurf, or any MCP-compatible AI assistant and ask questions directly against your app's live observability data.

!!! tip "New in v0.7.0"
    The MCP server is available starting from `django-orbit[mcp]`.

## Installation

```bash
pip install django-orbit[mcp]
```

The `[mcp]` extra installs the `mcp>=1.0` package. The core `django-orbit` package has no dependency on it.

## Setup

Add the MCP server configuration to your AI assistant:

=== "Claude Desktop"

    Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

    ```json
    {
      "mcpServers": {
        "django-orbit": {
          "command": "python",
          "args": ["manage.py", "orbit_mcp"],
          "cwd": "/path/to/your/django/project"
        }
      }
    }
    ```

=== "Cursor"

    Edit `.cursor/mcp.json` in your project:

    ```json
    {
      "mcpServers": {
        "django-orbit": {
          "command": "python",
          "args": ["manage.py", "orbit_mcp"],
          "cwd": "/path/to/your/django/project"
        }
      }
    }
    ```

=== "Windsurf"

    Edit `~/.windsurfrc`:

    ```json
    {
      "mcpServers": {
        "django-orbit": {
          "command": "python",
          "args": ["manage.py", "orbit_mcp"],
          "cwd": "/path/to/your/django/project"
        }
      }
    }
    ```

The MCP server launches on-demand using stdio transport — no extra process to keep running.

## Available Tools

The MCP server exposes raw telemetry tools plus higher-level agentic investigation tools to your AI assistant:

| Tool | What it returns |
|------|----------------|
| `get_recent_requests` | Last N HTTP requests with status, path, duration |
| `get_slow_queries` | SQL queries above threshold, sorted by duration |
| `get_exceptions` | Exceptions in a time window with full traceback |
| `get_n1_patterns` | Requests where N+1 duplicate queries were detected |
| `get_request_detail` | Every event for one request via `family_hash` |
| `search_entries` | Keyword search across all event types |
| `get_stats_summary` | Error rate, avg response time, cache hit rate |
| `audit_mcp_exposure` | Effective MCP safety policy: payload inclusion, masking and limits |
| `investigate_request` | Diagnosis for one `family_hash`: timeline, signals, queries, hypotheses and next actions |
| `investigate_exception_group` | Blast-radius summary for one exception fingerprint |
| `create_incident_bundle` | On-demand JSON handoff bundle from a request, fingerprint or ticket text |
| `build_debug_brief` | Match natural-language ticket/error text to recent Orbit evidence |
| `propose_fix_hypotheses` | Ranked fix directions from captured evidence; does not edit code |
| `propose_test_plan` | Suggested regression/performance tests for the observed issue |


## Agentic Debugging Workflow

Use the high-level tools when you want the assistant to move from symptom to evidence instead of browsing raw rows.

```text
build_debug_brief("checkout returns 500 payment token rejected")
create_incident_bundle("fingerprint", "<fingerprint-from-brief>", format="markdown")
propose_fix_hypotheses("fingerprint", "<fingerprint-from-brief>")
propose_test_plan("family_hash", "<family_hash>")
investigate_exception_group("<fingerprint>")
investigate_request("<family_hash>")
```

Incident bundles are generated on demand from current `OrbitEntry` data. They are not persisted. Each bundle includes primary evidence, a safety report, recommended next actions and tool suggestions for deeper investigation.

## Agent Safety

All MCP entry output goes through Orbit's agent-safe serializer. It masks sensitive keys using `MASK_KEYS`, can omit payloads entirely, and replaces oversized payloads with deterministic truncation metadata. Use `audit_mcp_exposure` to verify the effective policy before sharing an MCP session with an assistant.

## Example Prompts

Once connected, ask your AI assistant questions like:

- *"Why is `/api/orders/` slow? Check the recent requests."*
- *"What exceptions occurred in the last hour?"*
- *"Find all N+1 query patterns in the app"*
- *"Show me everything that happened during request abc123"*
- *"What's the current error rate and avg response time?"*

The assistant will call the appropriate Orbit tools and reason over the live data from your running Django app.

## Configuration

```python
ORBIT_CONFIG = {
    'MCP_ENABLED': True,  # default
}
```

Setting `MCP_ENABLED: False` does not disable the server itself (it's a management command), but disables data exposure if you want to prevent access programmatically.

## Running Manually

You can also run the MCP server directly for debugging:

```bash
python manage.py orbit_mcp
```

This starts the server in stdio mode. It reads JSON-RPC messages from stdin and writes responses to stdout — the standard MCP transport.

## How It Works

The `orbit_mcp` management command calls `create_mcp_server()` from `orbit/mcp_server.py`, which builds a `FastMCP` instance with the raw telemetry and agentic investigation tools. Each tool queries `OrbitEntry` directly from your Django database.

The server is stateless and read-only — it never modifies your data.

## Next Steps

- [Configuration Reference](configuration.md)
- [Storage Backends](storage-backends.md) — persist data across restarts for richer AI context
