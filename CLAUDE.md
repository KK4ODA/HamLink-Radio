# HamLink Radio — Project Memory

## What This Is

HamLink Radio is a family emergency communications app for licensed amateur radio operators (US FCC Part 97). A single monolithic Python/Flask file (`monitor.py`, ~5500 lines) with embedded HTML/CSS/JavaScript frontend. Monitors VarAC, APRS, and Winlink for check-in messages from a traveling family member and lets the home station operator send replies, post sitreps, and receive phone notifications.

**Repository:** https://github.com/KK4ODA/HamLink-Radio.git
**Branch:** master
**Main file:** `monitor.py` — contains everything (Flask backend, HTML, CSS, JS)
**Config:** `config.json` (gitignored — contains API keys, callsigns)
**Launcher:** `start_hamlink.bat`

## Architecture

- **Single file app** — intentionally monolithic for easy deployment. A dev said it "makes his eyes bleed" but the user prefers it this way for emergency reliability
- **Flask** serves the web UI and all API endpoints
- **Background threads:** APRS-IS listener, KISS/Soundmodem listener, Pat/Winlink poller, beacon loop, main DB poll loop
- **External processes launched:** VarAC, Soundmodem, VARA FM, Pat — all managed with subprocess.Popen, terminated on shutdown
- **No build tools** — no npm, webpack, etc. Leaflet.js served from `static/` folder

## Key Technical Decisions Made in This Session

### FCC Part 97 Compliance
- **97.115(c):** Third-party communications under automatic control require data emissions
- **97.221:** Automatically controlled digital stations must not exceed 500 Hz bandwidth
- **VARA HF 500 Hz:** Only compliant mode under automatic control
- **APRS Soundmodem & VARA FM:** Exceed 500 Hz — blocked by default with licensed operator / emergency override modal
- **VarAC replies:** NOT RF transmissions — they're SQLite database inserts to the outbox. Only sitrep broadcasts are actual RF (via VARA HF 500 Hz UI automation)
- **APRS bulletins:** Removed entirely (non-compliant under automatic control)
- US-only disclaimer added to manual and first-launch notice

### Pat/Winlink Integration (CRITICAL — hard-won knowledge)
- **Never spawn `pat connect telnet` as a subprocess** while `pat http` is running — causes file lock conflicts on Windows. Use Pat's HTTP API instead: `POST http://localhost:8080/api/connect` with form param `url=telnet` or `url=varafm:///GATEWAY`
- **Pat's HTTP API endpoints:** See `pat-interface-notes.md` for complete list
- **`pat compose`** CLI is safe alongside `pat http` (local file write only)
- **Separate RF poll interval** for Winlink RF gateway (default 3 hours) vs internet telnet (user-configurable, typically 180s)
- **Winlink position reports:** Queried from CMS RSS feed at `https://cms.winlink.org:444/rss/rsspositionreports.aspx?callsign=CALLSIGN` — coordinates are in the `<title>` tag (non-standard format), NOT in georss:point tags

### APRS
- **APRS-IS filter:** Must use `b/KK4ODA*` with wildcard asterisk to match all SSIDs. Without `*`, it only matches the exact callsign
- **APRS ACKs:** Handled via `packet["response"] == "ack"` (aprslib parsed) OR `msg_text.startswith("ack")` (raw). ACKs don't have `message_text` field in aprslib
- **Third-party packets:** iGate-relayed packets have `}` prefix in the info field. Must strip and re-parse the inner packet
- **Own-callsign filter:** KISS listener skips packets from exact home SSID (e.g., KK4ODA-1) only — NOT all SSIDs, since the traveler uses KK4ODA-7/9
- **APRS position:** Saved to `.last_position.json` to persist across restarts
- **MAIL bot store-and-forward:** Sent via APRS-IS when internet up, via RF Soundmodem when internet down

### Alarm/Dismiss System (Three Layers)
1. **PC speaker** (`_speaker_alarm_active`) — system beep, independent of browser
2. **Browser audio** (`alarmInt`, `speechSynthesis`) — Web Audio API or voice alerts
3. **Server state** (`pending_alerts`, `acknowledged_ids`)

**Dismiss levels:**
- **Dismiss/Dismiss All** — soft: silences alarm, keeps messages in "New Messages" for reply
- **Close Message** — hard: acknowledges server-side, moves to "Previous Messages"
- **Phone dismiss** — silences alarm only, sets `alarm_silenced` flag

**`speechSynthesis.cancel()`** must run outside `if(alarmInt)` to avoid race conditions. Prefer `localService` voices for offline operation.

### Internet Detection
- `_check_internet(timeout=2)` — TCP connect to `8.8.8.8:53`
- Checked every 15 seconds in the main poll loop, stored in `state["internet_up"]`
- All UI indicators use `d.internet_up` (not APRS-IS TCP state which can be stale)
- APRS-IS reconnect loop checks internet every second during backoff sleep for fast recovery
- Pushover and Winlink position checks skipped when internet is down (log "skipped" instead of errors)

### UI Details
- **Tab bar:** Dashboard + Offline Map (Leaflet.js with MBTiles)
- **Connection indicators:** VarAC database, APRS-IS (internet), APRS RF (Soundmodem), Winlink (internet/RF fallback)
- **Position card:** Pulses green with "NEW" badge on position/timestamp change. "✓ Seen" button to acknowledge
- **Sent messages:** Show "You → Recipient" with delivery status ("✓ Delivered", "📬 Stored in APRS mailbox")
- **Received messages:** Show "📨 Name → You" with channel tag
- **RF warning banner:** Shows for APRS RF and Winlink RF, includes note that VarAC is safe for unlicensed users
- **Compose box:** `onComposeInput()` only auto-manages APRS checkbox when opened with multi-channel or APRS. Single-channel reply (varac/winlink) doesn't touch APRS checkbox
- **Stop HamLink button:** Bottom of main page, graceful shutdown

### Graceful Shutdown
- `_cleanup()` registered via `atexit` and signal handlers (SIGINT/SIGTERM)
- `_kill_proc()` for tracked processes (terminate → wait 5s → kill)
- `_kill_by_name("VARA.exe")` and `_kill_by_name("soundmodem.exe")` via taskkill as backup
- `/api/shutdown` endpoint for UI button — delays 1s for HTTP response, then `os._exit(0)`
- Batch file checks exit code: 0 = clean (closes window), non-zero = shows error + pause

### Soundmodem Gotcha
- **Do NOT start Soundmodem minimized** — .NET WinForms `SplitterDistance` crash. Launch normally, not with `SW_SHOWMINIMIZED` or `start /min`
- The `HamLink.bat` wrapper was removed for this reason

### VarAC Message Filter
- Only skip messages FROM the exact home callsign (`KK4ODA`), NOT portable variants (`KK4ODA/P`)
- `startswith()` was too broad — `KK4ODA/P` starts with `KK4ODA` but is the traveler

### VarAC Vmail Deletion (added 2026-05-27)
- **VarAC's "delete" is soft-only** — it just sets `vmail.is_deleted = 1`. The row stays in `VarAC.db` and remains visible in VarAC's own inbox view, so users perceive "deleted" messages as coming back.
- HamLink's poll query filters `is_deleted = 0`, so soft-deleted rows do NOT alert in HamLink — but they still show in VarAC.
- **`/api/delete_vmail`** endpoint does a HARD `DELETE FROM vmail WHERE id=?` (plus `DELETE FROM vmail_attachment WHERE vmail_guid=?` for any attachments — note the join column is `vmail_guid`, not `vmail_id`). Advances `vmail_hwm` past the deleted id so it can't be re-fetched.
- **🗑 Delete button** on every vmail card in the UI (red, next to ✕ Close). Confirmation prompt before purge.
- When fixing user reports of "deleted vmails keep coming back in VarAC," the fix is a hard DELETE while VarAC is closed. Always back up `VarAC.db` first.

### Relay Automation (added in monitor.py — see `relay` config block)
- `varac_retrieve_relay()` automates VarAC's UI to retrieve vmails held by a relay station — uses `pywin32` + `UIAutomationCore` to click the RELAY status bar label and double-click the callsign in the DataGridView.
- State: `state["relay_tracking"]`, `relay_retrieval_queue`, `relay_pending_confirm`, `relay_last_attempt` (cooldown), `relay_paths` (for reply routing).
- Config knobs: `relay{enabled, auto_retrieve, auto_retrieve_delay_seconds, confirm_before_connect, max_retries, retry_delay_seconds, cooldown_seconds, route_replies_via_relay, ignore_stations, min_snr}`.
- Reply routing: if a vmail arrived via a relay, replies are auto-routed back through the same relay (set `relay.route_replies_via_relay` to disable).

### aprs.fi Backfill (added)
- On startup, `_aprs_fi_backfill()` queries `https://api.aprs.fi/api/get` for all traveler SSIDs.
- If aprs.fi has a newer position than what's persisted in `.last_position.json`, the saved position is updated.
- Covers the gap when HamLink was offline while traveler moved — APRS-IS doesn't replay history on reconnect.
- Requires `config.aprs.aprs_fi_api_key`. Silently skips if no key or no internet.

## Releases (added 2026-05-27)

- **Workflow:** `.github/workflows/release.yml` — triggered on `push: tags: ['v*']`.
- **Process:** `git tag -a v0.2.0 -m "v0.2.0" && git push origin v0.2.0` → CI builds `HamLink-v0.2.0.zip` via `git archive` (tracked files only, no config.json or logs leak) and publishes a GitHub release with an auto-generated changelog grouped by conventional-commit prefix (`feat:` → Features, `fix:` → Bug fixes, `docs:`, `chore|refactor|ci|build|perf|test|style` → Chores, else → Misc).
- **Current release:** `v0.1.0` (initial release after rebrand from HomeLink). The old `HomeLink Radio v1.0.0` release was deleted.
- **Tip:** Use `feat:` / `fix:` prefixes in commit messages going forward so the changelog auto-organizes nicely.

## File Structure

```
monitor.py          — The entire application (~5500 lines)
MANUAL.md           — User manual (~700 lines)
README.md           — Brief overview
start_hamlink.bat   — Windows launcher with dependency checks
pat-interface-notes.md — Pat HTTP API documentation (for other projects)
config.json         — User config (gitignored)
message_log.csv     — Message history CSV (gitignored)
.last_position.json — Persisted APRS position (gitignored)
.pat_seen_ids       — Winlink message dedup (gitignored)
static/             — Leaflet.js, CSS, marker icons (for offline maps)
tiles/              — MBTiles files for offline maps (gitignored)
tiles/README.txt    — Instructions for downloading tiles
.github/workflows/release.yml — Release automation (tag v* → GitHub release)
.github/ISSUE_TEMPLATE/        — Bug report & feature request templates
```

## Config Structure (config.json)

Key fields: `home_callsign`, `watch_callsigns[]`, `operator_name`, `varac_db_path`, `varac_exe_path`, `map_state`

Nested:
- `aprs{enabled, home_ssid, traveler_ssids, rf_fallback, use_mailbox, aprs_fi_api_key}`
- `soundmodem{enabled, exe_path, kiss_port}`
- `pat{enabled, exe_path, poll_interval, rf_fallback, rf_poll_interval, rf_gateway, position_reports, home_tactical, traveler_tactical}`
- `beacon{enabled, lat, lon, via_aprsis, via_rf}`
- `pushover{enabled, user_key, api_token, quick_replies}`
- `relay{enabled, auto_retrieve, auto_retrieve_delay_seconds, confirm_before_connect, max_retries, retry_delay_seconds, cooldown_seconds, route_replies_via_relay, ignore_stations, min_snr}`

## Testing Notes

- **VarAC messages:** Insert directly into VarAC.db Inbox (folder_id=1) with `vmail_from='KK4ODA/P'` to simulate traveler
- **APRS messages:** Send from phone via aprs.fi app using traveler SSID (KK4ODA-7)
- **APRS position:** Beacon from phone app — received via APRS-IS (with `b/KK4ODA*` filter) or via KISS (third-party packets from iGates)
- **Winlink:** Pat syncs via HTTP API — test by sending from Winlink web interface
- **RF fallback:** Disconnect internet, verify Pat uses VARA FM gateway, APRS uses Soundmodem
- **Self-echo on APRS-IS:** Can't receive your own callsign's packets on the same connection — use different device/IP for testing
