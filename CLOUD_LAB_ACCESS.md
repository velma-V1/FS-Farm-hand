# Farmhand live laboratory access — 2026-11-04

**Status:** A separate authenticated remote MCP gateway runs on the owner's PC. The `.mcp.json` entry provides the current HTTPS endpoint, but **cloud authentication must be activated in the Claude cloud environment before the tool works**.

## Cloud authorization (secure, no repo secret)

1. Open the existing Farmhand Claude Code cloud session; use the caret beside the session title -> **Edit cloud environment**.
2. Under **Network secrets**, add a **Bearer** credential for the exact host `catalogs-donor-sand-shine.trycloudflare.com`; use the token held exclusively on the PC at `~/.local/share/farmhand-cloud-bridge/auth_token`. The value is **never committed or pasted into this repository**.
3. Reconnect the project-scoped `farmhand-live-lab` MCP server in `/mcp`. Existing sessions may need to reload their MCP configuration after pulling this repository change. Do **not** change the Claude model to accomplish this. For new sessions, .mcp.json loads on startup.
4. The tunnel is currently an **ephemeral Cloudflare Quick Tunnel**. Its hostname can change if the service is restarted. Update the `.mcp.json` URL and the network-secret host in that case. A named tunnel is preferable for long-term use.

The local gateway is controlled by two user systemd services: `farmhand-mcp.service` and `farmhand-tunnel.service`. Both run from `~/.local/share/farmhand-cloud-bridge`, **not** from this source checkout.

## Tools

- `toolkit_list(directory)`: list permitted toolkit paths (private FS25 source).
- `toolkit_read(path,start_line,max_lines)`: read a limited excerpt.
- `toolkit_search(term,max_matches)`: search source references.
- `live_status()`: inspect running local FS25 test environment.
- `live_collect(test_id)`: retrieve available historical test evidence.
- `live_start_request(text)` and `live_job_status(job_id)`: run and monitor one bounded live request against the **local lab**, asynchronously.
- `live_request(text)`: synchronous live request; prefer start/poll for long runs.
- `live_stop()`: STOP the local lab task.

## Strict separation

This connects to the **existing local lab** checkout `/home/xxthatguyxx/projects/FS-Farm-hand` and its trusted host runner and FS25 save 1, **not** to the cloud production branch. A successful lab test does not verify an uninstalled cloud patch. The gateway does not expose arbitrary shell execution, file writes, install/restart, UI clicking, or host lifecycle controls. Do not change/reload the FS25 game or local runner through unrelated tools. Never treat third-party or GIANTS references as redistributable software.

Use the established toolkit's exact installed game data for API/physics hypotheses; minimize tool outputs and make targeted searches instead of reading the entire corpus.

## User-local gateway check

`systemctl --user status farmhand-mcp farmhand-tunnel` and `journalctl --user -u farmhand-tunnel -n 40 --no-pager` show connection health and the current hostname.
