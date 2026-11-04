# Farmhand production ↔ local FS25 lab access

Last verified: 2026-11-04. See `.mcp.json` for the current remote MCP URL.

## What exists

A private MCP gateway is running on the owner's Windows/WSL2 PC, independently of Codex's working tree. It exposes **read-only** FS25 toolkit research plus bounded operations against the **existing local lab runner**, which is attached to FS25 **Save 1**. No new runner or game was started.

Gateway services (PC only):
- `systemctl --user status farmhand-mcp.service farmhand-tunnel.service`
- Implementation: `~/.local/share/farmhand-cloud-bridge/bridge.py`
- Credential: `~/.local/share/farmhand-cloud-bridge/auth_token` — NEVER commit it.
- HTTPS endpoint: `https://catalogs-donor-sand-shine.trycloudflare.com/mcp`
- Temporary Cloudflare Quick Tunnel. Hostname **changes when recreated**; replace the URL and network-secret allowed host together.

## Connect the existing Claude Cloud session — one account-side authorization

1. The user should open their existing Claude cloud session, select the caret after its title, then **Edit cloud environment → Network secrets → Add secret**.
2. Name: **Farmhand private lab**. Allowed website: **catalogs-donor-sand-shine.trycloudflare.com**.
3. Use **Authorization** header, **Bearer** prefix, and paste the secret value privately from the PC file `~/.local/share/farmhand-cloud-bridge/auth_token`. On Windows PowerShell, `wsl cat ~/.local/share/farmhand-cloud-bridge/auth_token` displays it. Do not paste it into chat, a GitHub commit, or Claude's source tree.
4. Save the network secret. Anthropic's proxy can attach it to outgoing requests for the allowed hostname without letting the model read it. The approved hostname is reachable even under a Trusted cloud-network policy.
5. In the existing cloud session, bring in **only the new configuration/doc files** from the remote `cloud/farmhand-production-20261104` branch, preserving all local changes. Reconnect the `farmhand-live-lab` MCP server or enable its custom connector if the session had already started before the project .mcp.json was added. **Do not change the model or reread the whole repo.**
6. **PROVE access**: call `toolkit_list("notes")`, `toolkit_read("skills/fs25-master-skill/SKILL.md",1,15)`, and `live_status()`. Do not claim Claude Cloud is connected before these return.

No token, installed GIANTS corpus, game executable, or private toolkit files are stored in the public Farmhand GitHub repository.

## Available remote MCP tools

- `toolkit_list(directory)` — browse allowed reference directories.
- `toolkit_read(path,start_line,max_lines)` — read one bounded source excerpt.
- `toolkit_search(term,max_matches)` — search the actual locally installed FS25 reference corpus.
- `live_status()` — compact status, readiness, savegame, installed build and active task (no GUI focus).
- `live_collect(test_id)` — retrieve lab test evidence.
- `live_start_request(text)` — start one bounded natural-language test **only if local Save 1 is READY with no active job**; returns an ID.
- `live_job_status(job_id)` — check remote test completion.
- `live_stop(job_id)` — may stop **only a cloud-originated active test**.
- `live_hot_reload()` — perform Codex's **existing** background Lua hot reload of **already-installed local code**; refuses unless game linked and all jobs idle. It never presses keys, clicks menus, switches windows, launches or restarts FS25.

## Hot reload, with zero PC focus theft

Codex implemented a filesystem-triggered reload in `mod/FarmhandBridge.lua`: every ~1 second Farmhand checks the exact local game-profile directory
`modSettings/FS25_Farmhand/.fhReload`. When no command is running, the existing `FarmhandProbe:consoleReload()` reloads the five already-installed Lua modules in dependency order. It preserves listener/state identity and refreshes incompatible observation caches. The marker is consumed; `log.txt` records `[Farmhand] fhReload: reloaded ...`.

The gateway's `live_hot_reload()` verifies game and companion readiness, recent live frame, no active job, and no pending marker. It then creates only that marker and verifies consumption and game-log confirmation. **No foreground change or savegame reload.** Local live verification completed November 4: all five modules reloaded and the marker was consumed.

**Limitation:** Hot reload is **not deployment**. It reloads code ALREADY installed in the local lab. It cannot deploy cloud edits to the PC. New cloud production code must first be reviewed, tested, and deliberately promoted into a separate local staging workspace; do not overwrite Codex's dirty lab or silently install production patches over a running player's save.

GUI `click`/`key`, menus, `restart-fs25`, game closure and unrestricted host commands are intentionally **not** exposed through the MCP gateway. Existing Codex background screenshot path uses `.fhScreen`; do not bring FS25 to foreground just to inspect it.

## Engineering boundaries and product priorities

- Codex laboratory: discover/prove the smallest reusable primitives in actual FS25.
- Claude cloud: implement production Farmhand; use research and existing lab evidence but distinguish **lab proof** from **proof of cloud source**.
- Promote only evidence-backed improvements; no blind merge, no silent ghosting/teleport/restart, no user-focus theft.
- Preserve autonomous user-observer operation and physical verification. Live test movement must be confirmed by observed equipment/field state, not helper acceptance or planner logs.
- Build player-facing low-cost settings for optional behavior (ghosting defaults OFF), plain-language commands, STOP, and recovery with bounded side effects.
- Never copy GIANTS proprietary or other restricted third-party implementations into public product code. Use source for API facts and original implementations.

This gateway is a **temporary development channel**, not a production dependency of the released Farmhand mod. The shipping mod must function without the user's cloud account, MCP gateway or tunnel.
