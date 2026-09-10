# HamLink Radio — Project Memory

## What This Is

HamLink Radio is a family emergency communications app for licensed amateur radio operators (US FCC Part 97). A single monolithic Python/Flask file (`monitor.py`, ~6000 lines) with embedded HTML/CSS/JavaScript frontend. Monitors VarAC, APRS, and Winlink for check-in messages from a traveling family member and lets the home station operator send replies, post sitreps, and receive phone notifications.

**Repository:** https://github.com/KK4ODA/HamLink-Radio.git
**Branch:** master
**Main file:** `monitor.py` — contains everything (Flask backend, HTML, CSS, JS)
**Config:** `config.json` (gitignored — contains API keys, callsigns)
**Launcher:** `start_hamlink.bat`
**Version:** `__version__` constant near the top of `monitor.py` — bump it to match the release tag (CI warns if they differ).

## Architecture

- **Single file app** — intentionally monolithic for easy deployment. The user prefers it this way for emergency reliability. Do not split it into modules.
- **Flask** serves the web UI and all API endpoints (`threaded=True`)
- **Background threads:** APRS-IS listener, KISS/Soundmodem listener, Pat/Winlink poller, beacon loop, relay retrieval loop, main DB poll loop
- **External processes launched:** VarAC, Soundmodem, VARA FM, Pat — all managed with subprocess.Popen, terminated on shutdown
- **No build tools** — no npm, webpack, etc. Leaflet.js served from `static/` folder
- **Paths:** `APP_DIR` (config, logs, tiles — next to the script or the frozen .exe) vs `RES_DIR` (bundled `static/`, = `sys._MEIPASS` when frozen). Never use `os.path.dirname(__file__)` directly for user data.
- **All stdlib imports are at the top of the file.** Only optional deps (`aprslib`, `win32gui`, `comtypes`, `winsound`) are imported lazily inside functions.

### Frontend (embedded `HTML_PAGE`)
- Served with a plain `Response`, NOT `render_template_string` — the page has no template variables and Jinja would choke on `{#`/`{{` in JS.
- No external fonts/CDNs — system font stack so it renders offline.
- Light/dark theme via CSS variables: `:root` (light), `html[data-theme=dark]`, and `prefers-color-scheme` fallback. Toggle persists in `localStorage.hamlink_theme`.
- Message-card buttons use `data-act`/`data-id` attributes with ONE delegated click listener (no inline onclick with interpolated ids).
- `render()` only touches the DOM when the generated HTML changes (avoids flicker on the 3s poll).
- Settings drawer uses native `<details>` sections; `fillForm()` sets the On/Off badge in each summary.
- The compliance gate and start splash are unchanged in behaviour (localStorage `hamlink_compliance_accepted`; start() unlocks AudioContext).

### Demo mode
- **Demo seeds `alarm_silenced=True`** so nothing rings; browser-pane tests on this machine play through the user's speakers. Always close the test tab (`tabs_close`) and stop the server when done — an orphaned tab used to keep chiming (fixed: 3 failed polls stop the browser alarm).
- `HAMLINK_DEMO=1` env var → `_seed_demo_state()` fills state with sample messages/position, marks channels connected, enables relay tracking. Config changes are in-memory only, `poll_once()`/`init_hwm()` are skipped, no external programs launch, nothing transmits. Used for screenshots and UI work. `/api/version` and `/api/status` expose `demo: true`.

## Key Technical Decisions

### FCC Part 97 Compliance
- **97.115(c):** Third-party communications under automatic control require data emissions
- **97.221:** Automatically controlled digital stations must not exceed 500 Hz bandwidth
- **VARA HF 500 Hz:** Only compliant mode under automatic control
- **APRS Soundmodem & VARA FM:** Exceed 500 Hz — blocked by default with licensed operator / emergency override modal
- **VarAC replies:** NOT RF transmissions — they're SQLite database inserts to the outbox. Only sitrep broadcasts are actual RF (via VARA HF 500 Hz UI automation)
- **APRS bulletins:** Removed entirely (non-compliant under automatic control)
- US-only disclaimer added to manual and first-launch notice

### Security
- CSRF: per-process token exposed in `/api/status`; accepted as `X-CSRF-Token` header, `_csrf` JSON field, or `_csrf` form field. The phone quick-reply confirmation page embeds the token as a hidden form field — without it the POST is rejected with 403.
- `/api/status` returns a **deep copy of the whole config** (`copy.deepcopy(config)`). Do not go back to hand-listing keys: any key missing from the snapshot gets blanked the next time the user hits Save (this is how the aprs.fi key was being wiped in v0.1.0).
- HTML rendered from request data must go through `html.escape` (see `api_relay_approve_from_phone`).

### Pat/Winlink Integration (CRITICAL — hard-won knowledge)
- **Never spawn `pat connect telnet` as a subprocess** while `pat http` is running — causes file lock conflicts on Windows. Use Pat's HTTP API instead: `POST http://localhost:8080/api/connect` with form param `url=telnet` or `url=varafm:///GATEWAY`
- **Pat's HTTP API endpoints:** See `pat-interface-notes.md` for complete list
- **`pat compose`** CLI is safe alongside `pat http` (local file write only)
- **Separate RF poll interval** for Winlink RF gateway (default 3 hours) vs internet telnet (user-configurable, typically 180s)
- **Winlink position reports:** Queried from CMS RSS feed at `https://cms.winlink.org:444/rss/rsspositionreports.aspx?callsign=CALLSIGN` — coordinates are in the `<title>` tag (non-standard format), NOT in georss:point tags
- `.pat_seen_ids` is written once per inbox poll (not once per message)

### APRS
- **APRS-IS filter:** Must use `b/CALL*` with wildcard asterisk to match all SSIDs. Without `*`, it only matches the exact callsign
- **APRS ACKs:** Handled via `packet["response"] == "ack"` (aprslib parsed) OR `msg_text.startswith("ack")` (raw). ACKs don't have `message_text` field in aprslib
- **Outgoing ACKs use the cached `_internet_up()`** (state refreshed by the poll loop) rather than a blocking TCP probe, so the APRS-IS consumer callback is never stalled. User-initiated sends still probe live with `_check_internet()`.
- **Third-party packets:** iGate-relayed packets have `}` prefix in the info field. Must strip and re-parse the inner packet
- **Own-callsign filter:** KISS listener skips packets from exact home SSID (e.g., CALL-1) only — NOT all SSIDs, since the traveler uses CALL-7/9
- **APRS position:** Saved to `.last_position.json` to persist across restarts
- **MAIL bot store-and-forward:** Sent via APRS-IS when internet up, via RF Soundmodem when internet down

### Alarm/Dismiss System (Three Layers)
1. **PC speaker** (`_speaker_alarm_active`) — system beep, independent of browser
2. **Browser audio** (`alarmInt`, `speechSynthesis`) — Web Audio API or voice alerts
3. **Server state** (`pending_alerts`, `acknowledged_ids`)

**Dismiss levels:**
- **Dismiss alert** — soft: silences alarm, keeps messages in "New Messages" for reply
- **Close** — hard: acknowledges server-side, moves to "Previous Messages"
- **Phone dismiss** — silences alarm only, sets `alarm_silenced` flag

**`speechSynthesis.cancel()`** must run outside `if(alarmInt)` to avoid race conditions. Prefer `localService` voices for offline operation.

### Internet Detection
- `_check_internet(timeout=2)` — TCP connect to `8.8.8.8:53`
- Checked every poll cycle in the main poll loop, stored in `state["internet_up"]`; `_internet_up()` returns the cached value
- All UI indicators use `d.internet_up` (not APRS-IS TCP state which can be stale)
- APRS-IS reconnect loop checks internet every second during backoff sleep for fast recovery
- Pushover and Winlink position checks skipped when internet is down (log "skipped" instead of errors)

### Graceful Shutdown
- `_cleanup()` registered via `atexit` and signal handlers (SIGINT/SIGTERM)
- `_kill_proc()` for tracked processes (terminate → wait 5s → kill)
- `_kill_by_name("VARA.exe")` and `_kill_by_name("soundmodem.exe")` via taskkill as backup
- `/api/shutdown` endpoint for UI button — delays 1s for HTTP response, then `os._exit(0)`
- Batch file checks exit code: 0 = clean (closes window), non-zero = shows error + pause

### Soundmodem Gotcha
- **Do NOT start Soundmodem minimized** — .NET WinForms `SplitterDistance` crash. Launch normally, not with `SW_SHOWMINIMIZED` or `start /min`

### VarAC Message Filter
- Only skip messages FROM the exact home callsign, NOT portable variants (`CALL/P`)
- `startswith()` was too broad — `CALL/P` starts with `CALL` but is the traveler

### VarAC Vmail Deletion
- **VarAC's "delete" is soft-only** — it just sets `vmail.is_deleted = 1`. The row stays in `VarAC.db` and remains visible in VarAC's own inbox view.
- **`/api/delete_vmail`** does a HARD `DELETE FROM vmail WHERE id=?` (plus `DELETE FROM vmail_attachment WHERE vmail_guid=?` — the join column is `vmail_guid`). Advances `vmail_hwm` past the deleted id.
- Always back up `VarAC.db` before hard-deleting by hand.

### Relay Automation (see `relay` config block)
- `varac_retrieve_relay()` automates VarAC's UI via `pywin32` + `UIAutomationCore` to click the RELAY status bar label and double-click the callsign in the DataGridView.
- State: `state["relay_tracking"]`, `relay_retrieval_queue`, `relay_pending_confirm`, `relay_last_attempt` (cooldown), `relay_paths` (for reply routing).
- Reply routing: if a vmail arrived via a relay, replies are auto-routed back through the same relay (`relay.route_replies_via_relay`).

### aprs.fi Backfill
- On startup, `_aprs_fi_backfill()` queries `https://api.aprs.fi/api/get` for all traveler SSIDs and updates the saved position if newer. Requires `config.aprs.aprs_fi_api_key`.

### Self-update (added v0.3.0)
- `check_for_update()` hits `https://api.github.com/repos/KK4ODA/HamLink-Radio/releases/latest` (unauthenticated, 60 req/h is plenty at one check per `updates.interval_hours`). Result lives in `state["update"]` and is included in `/api/status`; endpoints `/api/update/status`, `/api/update/check` (POST), `/api/update/apply` (POST).
- `_install_kind()`: `exe` (frozen), `git` (APP_DIR has `.git` → refuse, say `git pull`), else `source`.
- exe path: downloads the `*-win64.zip` asset, renames the running exe to `*.old.exe` (allowed on Windows), moves the new one in; `_remove_old_exe()` cleans up at next start.
- source path: downloads `HamLink-vX.Y.Z.zip` (git archive), writes every file over APP_DIR except `.git*`/`.github`/`.claude`; `monitor.py` → backup `.bak`; `*.bat` written as `*.bat.new` because cmd.exe reads a running batch file incrementally.
- `_restart_app()` writes a temp `restart.bat` (wait 3s → move `*.bat.new` → `start start_hamlink.bat --no-browser` or the exe), runs it with CREATE_NO_WINDOW, calls `_cleanup()`, exits 0 (so an old launcher closes quietly). The launcher accepts `--no-browser`.
- UI: banner under the header (`showUpdateBanner`), "Later" stores the skipped tag in `localStorage.hamlink_update_skipped`, modal polls `/api/update/status` then `/api/version` until the server is back and reloads.
- `HAMLINK_VERSION_OVERRIDE=0.1.0` makes a build pretend to be older — the way to test the update flow against a real release. Demo mode fakes an available `v9.9.9` and a no-op install.

### Settings tests, validation, save confirmation (added v0.4.0)
- `POST /api/test {what: ...}` — `varac_exe`, `bbs_dir`, `aprs_is` (real login, reads `logresp ... verified/unverified`), `aprs_fi`, `kiss`, `varafm`, `pat`, `exe`, `github`, `beacon` (packet preview, nothing sent), `validate`. UI helper `runTest(what, trId, fields)`.
- `validate_config(cfg)` returns `[{level, field, message}]`; run on every save (returned by `/api/config` with `path` and `saved_at`) and by the **Check settings** button. Add new sanity checks there.
- Save shows a modal with the config path, time, and warnings; `/api/status` carries `config_path`, `app_dir`, `tiles_dir`, `config_saved_at`; `/api/open_folder` opens APP_DIR.
- `initTooltips()` gives every settings input/toggle a `title` built from its label + hint; dashboard buttons carry explicit titles.

### Offline map downloader (added v0.4.0)
- Tiles come from USGS The National Map (`MAP_SOURCES`: `usgs_topo`, `usgs_imagery`), public domain, bulk download allowed. Do not switch to tile.openstreetmap.org — its policy forbids bulk downloads.
- `start_map_download(name, lat, lon, radius_km, zmin, zmax, source)` → thread writes `tiles/<name>.mbtiles.part` (6 workers, TMS y-flip, metadata table) then renames and sets `config.map_file`. Progress in `_map_dl`; endpoints `/api/map/{list,estimate,download,status,cancel,select,delete}`.
- `config.map_file` (file name in tiles/) replaces the legacy `map_state` (US state name → `<state>.mbtiles`), which is still honoured by `_active_map_path()`.
- `serve_tile` sniffs PNG vs JPEG (USGS tiles are JPEG) and searches **every** .mbtiles in tiles/ (primary first, `_all_map_paths()`, connections cached in `_mbtiles_conns`) so several downloaded areas combine on one layer. `/api/status` carries `map_active` (primary file + bounds) and `map_coverage` (bounds of all files); the map tab's outside-coverage check uses the union.
- Map tab has a 'Download a map around the last position' card (`mapDownloadHere()`); Settings has 'Use last position'.

### Misc robustness
- `save_config()` writes to `config.json.tmp` then `os.replace` (atomic).
- `log_reply()` ids are `sent-{channel}-{call}-{uuid8}` — a multi-channel send creates several entries in the same second.
- MBTiles connection is shared across Flask threads behind `_mbtiles_lock`.
- CSV log writes are serialised with `_log_file_lock`.

## Releases

- **Workflow:** `.github/workflows/release.yml` — triggered on `push: tags: ['v*']`.
- **Job 1 (ubuntu):** `git archive` source zip + grouped changelog → creates the GitHub release. Warns if `__version__` ≠ tag.
- **Job 2 (windows):** PyInstaller `--onefile --add-data static;static --collect-all comtypes` → `HamLink-Radio-vX.Y.Z-win64.zip` (exe + README + MANUAL + tiles/README) uploaded to the same release.
- **Process:** bump `__version__`, commit, then `git tag -a v0.3.0 -m "v0.3.0" && git push origin v0.3.0`.
- **Tip:** Use `feat:` / `fix:` / `docs:` prefixes in commit messages so the changelog auto-organises.
- **History:** v0.1.0 (initial after HomeLink rebrand), v0.2.0 (UI redesign, bug fixes, demo mode, exe in CI), v0.3.0 (self-update), v0.3.1 (fix exe relaunch after update: strip _MEIPASS2/_PYI_* env before spawning the restart helper), v0.3.2 (exe opens the browser on start; --no-browser suppresses), v0.4.0 (settings tests/validation/save confirmation, built-in USGS map downloader, tooltips), v0.4.1 (callsign guidance + live address preview in Settings), v0.4.2 (Silence alarm / Mark all as read buttons in the hero, silence button on the start splash, alarm_timeout_minutes auto-stop), v0.4.3 (Offline Map tab: uses `map_active` from /api/status, fits to the downloaded bounds, explains missing file / position outside coverage), v0.4.4 (header bell silences everywhere + reports when nothing is ringing; native MessageBox popup on PC alarm start; Esc silences; `speaker_alarm_active` in status; `stop_speaker_alarm(silence=True)`), v0.4.5 (download map around traveler's last position; all .mbtiles files served together), v0.4.6 (browser alarm stops after 3 failed polls; demo starts silenced).

## File Structure

```
monitor.py              — The entire application
MANUAL.md               — User manual
README.md               — Overview with banner + screenshot (docs/)
requirements.txt        — flask, aprslib, pywin32/comtypes (win32 only)
start_hamlink.bat       — Windows launcher (installs requirements.txt)
build_hamlink_exe.bat   — PyInstaller build
pat-interface-notes.md  — Pat HTTP API documentation
docs/                   — banner.svg, icon.ico/png, screenshot-dashboard.png (rendered from demo mode with headless Chrome)
config.json             — User config (gitignored)
message_log.csv         — Message history CSV (gitignored)
.last_position.json     — Persisted APRS position (gitignored)
.pat_seen_ids           — Winlink message dedup (gitignored)
static/                 — Leaflet.js, CSS, marker icons
tiles/                  — MBTiles files (gitignored) + README.txt
.github/workflows/release.yml — Release automation
.github/ISSUE_TEMPLATE/       — Bug report & feature request templates
```

## Config Structure (config.json)

Key fields: `home_callsign`, `watch_callsigns[]`, `operator_name`, `varac_db_path`, `varac_exe_path`, `map_file` (legacy `map_state`)

Nested:
- `aprs{enabled, home_ssid, traveler_ssids, passcode, server, port, rf_fallback, use_mailbox, aprs_fi_api_key}`
- `soundmodem{enabled, exe_path, kiss_host, kiss_port, auto_launch}`
- `pat{enabled, exe_path, http_addr, auto_launch, poll_interval, rf_fallback, rf_poll_interval, rf_gateway, varafm_addr, varafm_exe_path, position_reports, home_tactical, traveler_tactical}`
- `beacon{enabled, lat, lon, symbol_table, symbol_code, comment, interval_minutes, via_aprsis, via_rf}`
- `pushover{enabled, user_key, api_token, priority, retry, expire, sound, quick_replies}`
- `updates{auto_check, interval_hours, github_token}`
- `relay{enabled, auto_retrieve, auto_retrieve_delay_seconds, confirm_before_connect, max_retries, retry_delay_seconds, cooldown_seconds, route_replies_via_relay, ignore_stations, min_snr}`

`load_config()` merges saved values over `DEFAULT_CONFIG` one level deep, so adding a key to `DEFAULT_CONFIG` is enough to introduce a new setting.

## Testing Notes

- **UI without hardware:** `HAMLINK_DEMO=1 python monitor.py`
- **VarAC messages:** Insert directly into VarAC.db Inbox (folder_id=1) with `vmail_from='CALL/P'` to simulate traveler
- **APRS messages:** Send from phone via aprs.fi app using traveler SSID (CALL-7)
- **APRS position:** Beacon from phone app — received via APRS-IS (with `b/CALL*` filter) or via KISS (third-party packets from iGates)
- **Winlink:** Pat syncs via HTTP API — test by sending from Winlink web interface
- **RF fallback:** Disconnect internet, verify Pat uses VARA FM gateway, APRS uses Soundmodem
- **Self-echo on APRS-IS:** Can't receive your own callsign's packets on the same connection — use different device/IP for testing
- **CSRF check:** `curl -X POST -d "channel=varac&message=" /api/pushover_reply_send` → 403; with `_csrf=<token>` → 400 "No message"
