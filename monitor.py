#!/usr/bin/env python3
"""
HamLink Radio — Family Edition
-------------------------------------
A friendly web app for family members to see check-in messages
from a traveling ham operator, and reply back via VarAC VMail.

No ham radio knowledge required to use.
All configuration in the browser Settings panel.
"""

import json, os, sys, sqlite3, time, threading, logging, uuid, csv, atexit, signal
import socket, subprocess, re, copy, html, string, configparser
import urllib.request, urllib.parse, urllib.error, zipfile, tempfile, shutil, webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request, Response

__version__ = "0.4.2"
UPDATE_REPO = "KK4ODA/HamLink-Radio"   # GitHub repo checked for new releases

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# APP_DIR holds user data (config.json, logs) and must sit next to the script
# or the frozen .exe. RES_DIR holds bundled resources (static/) — inside the
# PyInstaller extraction dir when frozen, otherwise the same as APP_DIR.
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RES_DIR = getattr(sys, "_MEIPASS", APP_DIR)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    RES_DIR = APP_DIR

DEMO_MODE = os.environ.get("HAMLINK_DEMO", "").strip() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Local PC speaker alarm (works without browser)
# ---------------------------------------------------------------------------
_speaker_alarm_active = False
_speaker_alarm_thread = None

def _beep_system():
    """Produce a loud beep using OS-native methods — no browser needed."""
    try:
        if sys.platform == "win32":
            import winsound
            # Three urgent beeps: 1000Hz and 1500Hz alternating
            for _ in range(3):
                winsound.Beep(1000, 300)
                time.sleep(0.05)
                winsound.Beep(1500, 300)
                time.sleep(0.05)
        elif sys.platform == "darwin":
            # macOS: use system sound via osascript
            os.system('osascript -e \'display notification "New message!" with title "HamLink Radio" sound name "Submarine"\'')
            os.system("afplay /System/Library/Sounds/Ping.aiff &")
        else:
            # Linux: try multiple approaches
            if os.system("command -v paplay >/dev/null 2>&1") == 0:
                os.system("paplay /usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga 2>/dev/null &")
            elif os.system("command -v beep >/dev/null 2>&1") == 0:
                os.system("beep -f 1000 -l 300 -D 50 -f 1500 -l 300 2>/dev/null &")
            else:
                # ASCII bell as last resort
                sys.stdout.write('\a')
                sys.stdout.flush()
    except Exception as e:
        log.warning("System beep failed: %s", e)

_speaker_alarm_started = 0.0

def _alarm_timeout_seconds():
    """0 = ring until dismissed; otherwise stop by itself after N minutes."""
    with cfglock:
        m = config.get("alarm_timeout_minutes", 15)
    try:
        return max(0, float(m)) * 60
    except (TypeError, ValueError):
        return 15 * 60

def _speaker_alarm_loop():
    """Background loop that beeps every 8 seconds while alarm is active."""
    global _speaker_alarm_active
    while _speaker_alarm_active:
        limit = _alarm_timeout_seconds()
        if limit and time.time() - _speaker_alarm_started > limit:
            log.info("Local speaker alarm auto-stopped after %d min (alarm_timeout_minutes)", int(limit // 60))
            _speaker_alarm_active = False
            with slock:
                state["alarm_silenced"] = True
            return
        _beep_system()
        for _ in range(80):  # 8 seconds in 0.1s increments, check flag
            if not _speaker_alarm_active:
                return
            time.sleep(0.1)

def start_speaker_alarm():
    global _speaker_alarm_active, _speaker_alarm_thread, _speaker_alarm_started
    # Reset silenced flag — new alert means alarm should sound again
    with slock:
        state["alarm_silenced"] = False
    _speaker_alarm_started = time.time()
    if _speaker_alarm_active:
        return
    _speaker_alarm_active = True
    _speaker_alarm_thread = threading.Thread(target=_speaker_alarm_loop, daemon=True)
    _speaker_alarm_thread.start()
    log.info("Local speaker alarm STARTED")

def stop_speaker_alarm():
    global _speaker_alarm_active
    if _speaker_alarm_active:
        _speaker_alarm_active = False
        log.info("Local speaker alarm STOPPED")

# ---------------------------------------------------------------------------
# Persistent message log (survives restarts)
# ---------------------------------------------------------------------------
LOG_FILE = os.path.join(APP_DIR, "message_log.csv")
_log_file_lock = threading.Lock()

def _init_log_file():
    """Create the CSV log file with headers if it doesn't exist."""
    if not os.path.isfile(LOG_FILE):
        with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "type", "from_callsign", "from_name",
                        "to_callsign", "subject", "message", "urgent",
                        "band", "snr", "direction"])

def _append_log_row(row):
    """Append one row to the CSV log (thread-safe; several pollers write here)."""
    try:
        with _log_file_lock:
            _init_log_file()
            with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(row)
    except Exception as e:
        log.warning("Failed to write message log: %s", e)

def log_message(alert_dict, direction="incoming"):
    """Append a received message to the persistent CSV log."""
    _append_log_row([
        alert_dict.get("time", ""),
        alert_dict.get("type", ""),
        alert_dict.get("from_call", ""),
        alert_dict.get("from_name", ""),
        alert_dict.get("to", ""),
        alert_dict.get("subject", ""),
        alert_dict.get("message", ""),
        alert_dict.get("urgent", False),
        alert_dict.get("band", ""),
        alert_dict.get("snr", ""),
        direction,
    ])

def log_reply(to_call, message, channel=""):
    """Log an outgoing reply to the CSV and add to dashboard history."""
    now_utc = datetime.now(timezone.utc).isoformat()
    _append_log_row([now_utc, "reply", "", "", to_call,
                     "Reply", message, False, "", "", "outgoing"])
    # Add to dashboard history so sent messages appear in Previous Messages.
    # A multi-channel send creates several entries in the same second, so the
    # id carries the channel plus a short random suffix to stay unique.
    reply_id = f"sent-{channel or 'msg'}-{to_call}-{uuid.uuid4().hex[:8]}"
    with slock:
        state["history"].append({
            "id": reply_id, "type": "sent",
            "time": now_utc,
            "from_call": "", "from_name": "You",
            "to_callsign": to_call,
            "subject": "",
            "message": message,
            "channel": channel,
            "urgent": False,
            "friendly_time": "",
        })

# ---------------------------------------------------------------------------
# Persistent position storage
# ---------------------------------------------------------------------------
_POSITION_FILE = os.path.join(APP_DIR, ".last_position.json")

def _save_position(pos):
    """Save APRS position to disk so it survives restarts."""
    try:
        with open(_POSITION_FILE, "w", encoding="utf-8") as f:
            json.dump(pos, f)
    except Exception:
        pass

def _load_saved_position():
    """Load last APRS position from disk."""
    try:
        if os.path.isfile(_POSITION_FILE):
            with open(_POSITION_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
DEFAULT_CONFIG = {
    "varac_db_path": "",
    "varac_exe_path": "",
    "varac_profile": "",
    "bbs_directory": "",
    "poll_interval_seconds": 15,
    "watch_callsigns": [],
    "operator_name": "",
    "home_callsign": "",
    "pushover": {"enabled": False, "user_key": "", "api_token": "",
                 "priority": 1, "retry": 60, "expire": 3600, "sound": "pushover",
                 "quick_replies": False},
    "aprs": {"enabled": False, "home_ssid": "-5", "traveler_ssids": "-7",
             "passcode": "", "server": "rotate.aprs2.net", "port": 14580,
             "use_mailbox": False, "rf_fallback": False, "aprs_fi_api_key": ""},
    "soundmodem": {"enabled": False, "exe_path": "", "kiss_host": "127.0.0.1",
                   "kiss_port": 8100, "auto_launch": True},
    "pat": {"enabled": False, "exe_path": "", "http_addr": "localhost:8080",
            "auto_launch": True, "poll_interval": 30,
            "home_tactical": "", "traveler_tactical": "",
            "rf_fallback": False, "rf_gateway": "", "rf_poll_interval": 10800,
            "varafm_addr": "localhost:8300",
            "varafm_exe_path": "", "position_reports": False},
    "beacon": {"enabled": False, "lat": 0.0, "lon": 0.0,
               "symbol_table": "/", "symbol_code": "-",
               "comment": "HamLink Radio", "interval_minutes": 30,
               "via_aprsis": True, "via_rf": False},
    "relay": {"enabled": False, "auto_retrieve": False,
              "auto_retrieve_delay_seconds": 10, "confirm_before_connect": True,
              "max_retries": 2, "retry_delay_seconds": 120,
              "cooldown_seconds": 300, "route_replies_via_relay": True,
              "ignore_stations": [], "min_snr": None},
    "map_state": "",      # legacy: US state name -> tiles/<state>.mbtiles
    "map_file": "",       # preferred: file name inside tiles/
    "web_port": 5000,
    "updates": {"auto_check": True, "interval_hours": 6, "github_token": ""},
    "alert_sound": "gentle",
    "alert_volume": 0.3,
    "alarm_timeout_minutes": 15,   # PC speaker + browser alarm stop by themselves after this (0 = never)
    "quick_replies": [
        "Got your message, all is well here!",
        "Please check in again soon.",
        "Call home when you can.",
        "We miss you, stay safe!",
    ],
}

def _merge_defaults(defaults, saved):
    """Overlay a saved config on the defaults, one level deep for nested sections."""
    merged = copy.deepcopy(defaults)
    for k, v in (saved or {}).items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    return merged

def load_config():
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            m = _merge_defaults(DEFAULT_CONFIG, saved)
            # Ensure watch_callsigns is a list
            if isinstance(m.get("watch_callsigns"), str):
                m["watch_callsigns"] = [c.strip() for c in m["watch_callsigns"].split(",") if c.strip()]
            print(f"[CONFIG] Loaded from {CONFIG_PATH}")
            return m
        except Exception as e:
            print(f"[CONFIG] Error loading {CONFIG_PATH}: {e} — using defaults")
    else:
        print(f"[CONFIG] No config found at {CONFIG_PATH} — using defaults")
    return copy.deepcopy(DEFAULT_CONFIG)

def save_config(c):
    """Write config atomically so a crash mid-write can't leave a truncated file."""
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=2)
    os.replace(tmp, CONFIG_PATH)

config = load_config()
# Save defaults if config.json doesn't exist yet (first run)
if not os.path.isfile(CONFIG_PATH):
    save_config(config)
    print(f"[CONFIG] Created default config at {CONFIG_PATH}")
cfglock = threading.Lock()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("hamlink-radio")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
state = {
    "vmail_hwm": 0, "relay_hwm": 0,
    "pending_alerts": [], "acknowledged_ids": set(),
    "history": [], "last_poll": None,
    "db_connected": False, "error": None,
    "last_checkin_time": None, "last_checkin_from": None,
    "reply_status": None,
    "aprs_connected": False, "aprs_error": None,
    "kiss_connected": False, "kiss_error": None,
    "pat_connected": False, "pat_error": None, "pat_last_check": None, "pat_next_sync": None, "pat_using_rf": False,
    "aprs_last_ack": None,
    "internet_up": True,
    "aprs_last_position": _load_saved_position(),
    "winlink_last_position": None,
    "alarm_silenced": False,
    # Relay automation state
    "relay_tracking": {},           # {relay_callsign: tracking entry dict}
    "relay_retrieval_queue": [],    # Pending retrieval tasks
    "relay_retrieval_active": False,# True while UI automation is running
    "relay_last_attempt": {},       # {relay_callsign: ISO timestamp} for cooldown
    "relay_pending_confirm": None,  # Relay callsign awaiting user confirmation (or None)
    "relay_paths": {},              # {from_callsign: last_relay_station} for reply routing
    # Self-update state (see "Updates" section)
    "update": {"available": False, "current": __version__, "latest": None, "url": None,
               "notes": "", "published": None, "asset_url": None, "asset_api_url": None, "asset_name": None,
               "checked_at": None, "error": None, "install_kind": None,
               "stage": "idle", "progress": ""},
}
slock = threading.Lock()

# ---------------------------------------------------------------------------
# APRS-IS passcode generator
# ---------------------------------------------------------------------------
def aprs_passcode(callsign):
    """Generate APRS-IS passcode from callsign (standard algorithm)."""
    call = callsign.upper().split("-")[0].strip()
    code = 0x73e2
    for i in range(0, len(call), 2):
        code ^= ord(call[i]) << 8
        if i + 1 < len(call):
            code ^= ord(call[i + 1])
    return code & 0x7FFF

# ---------------------------------------------------------------------------
# APRS-IS engine
# ---------------------------------------------------------------------------
_aprs_thread = None
_aprs_running = False
_aprs_conn = None          # shared APRS-IS connection object
_aprs_conn_lock = threading.Lock()
_aprs_seen_msgs = set()    # dedup: set of "CALL:MSGNO" strings already processed

def _aprs_callsign():
    """Return the home APRS callsign with SSID (e.g., W1AW-5)."""
    with cfglock:
        base = config.get("home_callsign", "").strip().upper()
        ssid = config.get("aprs", {}).get("home_ssid", "-5")
    if not base:
        return ""
    return base + ssid if ssid and not base.endswith(ssid) else base

def _aprs_traveler_calls():
    """Return list of all traveler APRS callsigns with SSIDs (e.g., ['W1AW-7', 'W1AW-9'])."""
    with cfglock:
        watch = config.get("watch_callsigns", [])
        ssids_str = config.get("aprs", {}).get("traveler_ssids", "") or config.get("aprs", {}).get("traveler_ssid", "-7")
        base = config.get("home_callsign", "").strip().upper()
    if watch:
        call = watch[0].upper().split("-")[0].split("/")[0].strip()
    elif base:
        call = base.split("-")[0].split("/")[0].strip()
    else:
        return []
    ssids = [s.strip() for s in ssids_str.split(",") if s.strip()]
    if not ssids:
        ssids = ["-7"]
    return [call + s if s.startswith("-") else call + "-" + s for s in ssids]

def _aprs_traveler_call():
    """Return the first (default) traveler callsign for replies."""
    calls = _aprs_traveler_calls()
    return calls[0] if calls else ""

def _check_internet(timeout=3):
    """Quick check if internet is available by connecting to a DNS server."""
    try:
        with socket.create_connection(("8.8.8.8", 53), timeout=timeout):
            return True
    except OSError:
        return False

def _internet_up():
    """Cached internet status (refreshed by the poll loop). Use this on hot
    paths such as ACKs, where a blocking TCP probe would stall a listener."""
    with slock:
        return bool(state.get("internet_up", True))

def _port_open(host, port, timeout=2):
    """True if something is listening on host:port (used to detect running helpers)."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False

def _aprs_send_raw(packet_str):
    """Send a raw APRS packet via the shared listener connection, or open a new one."""
    global _aprs_conn
    # Try shared connection first
    with _aprs_conn_lock:
        if _aprs_conn:
            try:
                _aprs_conn.sendall(packet_str)
                log.info("APRS TX (shared): %s", packet_str[:80])
                return True
            except Exception as e:
                log.warning("APRS shared conn send failed: %s", e)
    # Fallback: temporary connection
    try:
        import aprslib
        with cfglock:
            ap = dict(config.get("aprs", {}))
            base = config.get("home_callsign", "").strip().upper()
        home = _aprs_callsign()
        pc = ap.get("passcode", "") or str(aprs_passcode(base))
        ais = aprslib.IS(home, passwd=pc,
                         host=ap.get("server", "rotate.aprs2.net"),
                         port=int(ap.get("port", 14580)))
        ais.connect()
        try:
            ais.sendall(packet_str)
        finally:
            ais.close()
        log.info("APRS TX (temp): %s", packet_str[:80])
        return True
    except Exception as e:
        log.error("APRS send failed: %s", e)
        return False

def _aprs_send_via_kiss(aprs_str):
    """Send an APRS packet via Soundmodem KISS (RF) if available."""
    ax25 = _aprs_string_to_ax25(aprs_str)
    if ax25:
        return _kiss_send_frame(ax25)
    return False

def _aprs_send_ack(to_call, msgno):
    """Send an APRS message ACK via APRS-IS. Only sends via RF if RF fallback
    is enabled and internet is down (same logic as aprs_send_message)."""
    home = _aprs_callsign()
    padded = to_call.ljust(9)

    with cfglock:
        rf_fallback = config.get("aprs", {}).get("rf_fallback", False)
    use_rf = rf_fallback and not _internet_up()

    if use_rf:
        rf_pkt = f"{home}>APRS,WIDE1-1::{padded}:ack{msgno}"
        log.info("APRS ACK (RF) -> %s for msg# %s", to_call, msgno)
        _aprs_send_via_kiss(rf_pkt)
    else:
        ack_pkt = f"{home}>APRS,TCPIP*::{padded}:ack{msgno}"
        log.info("APRS ACK (APRS-IS) -> %s for msg# %s", to_call, msgno)
        _aprs_send_raw(ack_pkt)

def _process_aprs_packet(packet):
    """Process a parsed APRS packet."""
    try:
        ptype = packet.get("format", "")
        from_call = packet.get("from", "")
        log.debug("APRS-IS packet: %s from %s (format: %s)", packet.get("raw", "")[:80], from_call, ptype)

        # Snapshot ALL config values we need in one lock acquisition
        # NEVER call _aprs_callsign() or _aprs_traveler_calls() while holding cfglock
        with cfglock:
            opname = config.get("operator_name", "") or from_call
            home_base = config.get("home_callsign", "").strip().upper()
            home_ssid = config.get("aprs", {}).get("home_ssid", "-5")
            watch_raw = list(config.get("watch_callsigns", []))
            traveler_ssids_str = config.get("aprs", {}).get("traveler_ssids", "") or config.get("aprs", {}).get("traveler_ssid", "-7")

        # Derive callsigns from snapshot (no lock needed)
        if home_base:
            home_full = (home_base + home_ssid if home_ssid and not home_base.endswith(home_ssid)
                         else home_base).upper()
        else:
            home_full = ""
        home_base_only = home_base.split("-")[0].split("/")[0]
        watch_bases = [c.upper().split("-")[0].split("/")[0] for c in watch_raw if c.strip()]
        # Build traveler callsigns
        trav_base = watch_raw[0].upper().split("-")[0].split("/")[0].strip() if watch_raw else home_base_only
        trav_ssids = [s.strip() for s in traveler_ssids_str.split(",") if s.strip()] or ["-7"]
        traveler_calls = [trav_base + s if s.startswith("-") else trav_base + "-" + s for s in trav_ssids] if trav_base else []

        # --- Position packets ---
        lat = packet.get("latitude")
        lon = packet.get("longitude")
        if lat is not None and lon is not None:
            base_from = from_call.split("-")[0].upper()
            # Skip our own home station position (our beacon)
            if from_call.upper() == home_full:
                pass  # Don't track our own position
            elif not watch_bases or base_from in watch_bases or base_from == home_base_only:
                now_utc = datetime.now(timezone.utc).isoformat()
                new_pos = {
                    "callsign": from_call, "lat": lat, "lon": lon,
                    "time": now_utc,
                    "altitude": packet.get("altitude"),
                    "speed": packet.get("speed"),
                    "comment": packet.get("comment", ""),
                }
                with slock:
                    state["aprs_last_position"] = new_pos
                _save_position(new_pos)
                log.info("APRS position from %s: %.4f, %.4f", from_call, lat, lon)

        # --- Message packets ---
        if ptype == "message" or "message_text" in packet:
            msg_text = packet.get("message_text", "")
            addresse = packet.get("addresse", "").strip()
            msgno = packet.get("msgNo", "") or packet.get("msgno", "")

            # Handle ACKs — track delivery confirmation
            response = packet.get("response", "")
            if response == "ack" or (msg_text and msg_text.lower().startswith("ack")):
                acked_msgno = msgno or (msg_text[3:].strip() if msg_text else "")
                if acked_msgno:
                    log.info("APRS ACK received from %s for msg# %s", from_call, acked_msgno)
                    now_utc = datetime.now(timezone.utc).isoformat()
                    with slock:
                        state["aprs_last_ack"] = {
                            "from": from_call,
                            "msgno": acked_msgno,
                            "time": now_utc,
                        }
                        # Tag the most recent sent APRS message as delivered
                        for h in reversed(state["history"]):
                            if h.get("type") == "sent" and h.get("channel") == "aprs":
                                h["delivered"] = True
                                h["delivered_by"] = from_call
                                h["delivered_time"] = now_utc
                                break
                return
            # Skip empty, REJs
            if response == "rej" or not msg_text or msg_text.lower().startswith("rej"):
                return

            # Skip messages from APRS system bots (automated confirmations, not human messages)
            _aprs_system_calls = {"MAIL", "EMAIL", "SMSGTE", "BLN", "NWS", "CQSRVR",
                                  "ANSRVR", "WLNK-1", "PSAT", "ARISS", "ISS"}
            from_base = from_call.upper().split("-")[0]
            if from_base in _aprs_system_calls:
                log.info("APRS system: %s says: %s", from_call, msg_text[:60])
                # Send ACK so the bot stops retrying
                if msgno:
                    _aprs_send_ack(from_call, msgno)
                # Update the most recent sent APRS message with bot confirmation
                now_utc = datetime.now(timezone.utc).isoformat()
                with slock:
                    for h in reversed(state["history"]):
                        if h.get("type") == "sent" and h.get("channel") == "aprs":
                            h["mail_bot_confirmed"] = True
                            h["mail_bot_msg"] = msg_text
                            h["mail_bot_time"] = now_utc
                            break
                return

            # Only messages addressed to us (home SSID or any traveler SSID)
            accepted_calls = {home_full.upper()} | {c.upper() for c in traveler_calls}
            if addresse.upper() not in accepted_calls:
                return

            # --- DEDUP by sender + msgno ---
            dedup_key = f"{from_call}:{msgno}" if msgno else ""
            if dedup_key and dedup_key in _aprs_seen_msgs:
                log.info("APRS dedup: %s (re-sending ACK)", dedup_key)
                if msgno:
                    _aprs_send_ack(from_call, msgno)
                return
            if dedup_key:
                _aprs_seen_msgs.add(dedup_key)

            # --- ACK FIRST (stops sender retries) ---
            if msgno:
                log.info("APRS sending ACK for msgNo=%s from %s", msgno, from_call)
                _aprs_send_ack(from_call, msgno)
            else:
                log.warning("APRS message has no msgNo — cannot send ACK")

            # --- Create alert ---
            now_utc = datetime.now(timezone.utc).isoformat()
            alert_id = f"aprs-{from_call}-{msgno}" if msgno else f"aprs-{from_call}-{int(time.time())}"

            with slock:
                existing = {a["id"] for a in state["pending_alerts"]} | {a["id"] for a in state["history"]} | state["acknowledged_ids"]
                if alert_id in existing:
                    return
                alert = {
                    "id": alert_id, "type": "aprs", "time": now_utc,
                    "from_call": from_call, "from_name": opname,
                    "to": addresse, "subject": "", "message": msg_text,
                    "urgent": False, "friendly_time": _friendly_time(now_utc),
                }
                state["pending_alerts"].append(alert)
                state["history"].append(alert)
                state["last_checkin_time"] = now_utc
                state["last_checkin_from"] = opname

            log.info("APRS MSG from %s: %s", from_call, msg_text[:60])
            log_message(alert, "incoming")
            start_speaker_alarm()
            send_pushover(f"APRS from {opname}", msg_text, reply_channel="aprs")

    except Exception as e:
        log.warning("APRS packet error: %s", e)

def aprs_send_message(to_call, message):
    """Send an APRS message via APRS-IS or RF (Soundmodem) with auto-fallback."""
    home = _aprs_callsign()
    if not home:
        return False, "Home callsign not configured"
    padded = to_call.ljust(9)
    msg = message[:67]
    msgno = str(int(time.time()) % 1000)

    # Check if we should use RF fallback
    with cfglock:
        rf_fallback = config.get("aprs", {}).get("rf_fallback", False)
    use_rf = False
    if rf_fallback and not _check_internet(timeout=2):
        log.info("No internet detected — sending APRS via RF (Soundmodem)")
        use_rf = True

    if use_rf:
        # Send via Soundmodem KISS (RF path, use WIDE1-1 for digipeating)
        rf_pkt = f"{home}>APRS,WIDE1-1,WIDE2-1::{padded}:{msg}{{{msgno}"
        ax25 = _aprs_string_to_ax25(rf_pkt)
        if ax25:
            ok = _kiss_send_frame(ax25)
            if ok:
                log.info("APRS message sent via RF to %s", to_call)
            else:
                log.warning("APRS RF send failed — Soundmodem not connected?")
                return False, "RF send failed — Soundmodem not connected"
        else:
            return False, "Failed to encode AX.25 frame"
    else:
        pkt = f"{home}>APRS,TCPIP*::{padded}:{msg}{{{msgno}"
        ok = _aprs_send_raw(pkt)
    if ok:
        log_reply(to_call, f"[APRS] {msg}", channel="aprs")
        # Also send to MAIL bot for store-and-forward if enabled
        with cfglock:
            use_mailbox = config.get("aprs", {}).get("use_mailbox", False)
        log.info("APRS mailbox store-and-forward: %s", "enabled" if use_mailbox else "disabled")
        if use_mailbox:
            mail_padded = "MAIL".ljust(9)
            # Format: @CALLSIGN message
            mail_dest = to_call.strip()
            mail_msg = f"@{mail_dest} {msg}"[:67]
            mail_msgno = str((int(time.time()) + 1) % 1000)
            if use_rf:
                # Send MAIL bot message via RF so iGates can relay it
                rf_mail_pkt = f"{home}>APRS,WIDE1-1,WIDE2-1::{mail_padded}:{mail_msg}{{{mail_msgno}"
                _aprs_send_via_kiss(rf_mail_pkt)
            else:
                mail_pkt = f"{home}>APRS,TCPIP*::{mail_padded}:{mail_msg}{{{mail_msgno}"
                _aprs_send_raw(mail_pkt)
            log.info("APRS mailbox copy sent to MAIL for %s", mail_dest)
        return True, ""
    return False, "Failed to send"


def _aprs_fi_backfill():
    """Query aprs.fi for all traveler SSIDs at startup and update the saved
    position if aprs.fi has something newer than what we persisted. Covers the
    case where HamLink was off while the traveler moved — APRS-IS doesn't
    replay history on reconnect, so we ask aprs.fi instead."""
    with cfglock:
        ap = config.get("aprs", {})
        api_key = (ap.get("aprs_fi_api_key", "") or "").strip()
    if not api_key:
        return
    if not _check_internet(timeout=3):
        log.info("aprs.fi backfill: no internet, skipping")
        return
    traveler_calls = _aprs_traveler_calls()
    if not traveler_calls:
        return
    names = ",".join(traveler_calls[:20])  # aprs.fi caps names per request
    url = "https://api.aprs.fi/api/get?" + urllib.parse.urlencode({
        "name": names, "what": "loc", "apikey": api_key, "format": "json",
    })
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "HamLink-Radio"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning("aprs.fi backfill failed: %s", e)
        return
    if data.get("result") != "ok":
        log.warning("aprs.fi backfill: %s", data.get("description", data.get("result")))
        return
    entries = data.get("entries", []) or []
    if not entries:
        log.info("aprs.fi backfill: no entries for %s", names)
        return

    def _etime(e):
        try:
            return int(e.get("lasttime") or e.get("time") or 0)
        except (TypeError, ValueError):
            return 0

    newest = max(entries, key=_etime)
    newest_ts = _etime(newest)
    if newest_ts <= 0:
        return

    with slock:
        saved = state.get("aprs_last_position")
    saved_ts = 0
    if saved and saved.get("time"):
        try:
            saved_ts = int(datetime.fromisoformat(
                saved["time"].replace("Z", "+00:00")).timestamp())
        except Exception:
            saved_ts = 0
    if newest_ts <= saved_ts:
        log.info("aprs.fi backfill: saved position is current")
        return
    try:
        lat = float(newest.get("lat"))
        lon = float(newest.get("lng"))
    except (TypeError, ValueError):
        return

    def _fnum(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    iso = datetime.fromtimestamp(newest_ts, tz=timezone.utc).isoformat()
    new_pos = {
        "callsign": newest.get("name", ""),
        "lat": lat, "lon": lon, "time": iso,
        "altitude": _fnum(newest.get("altitude")),
        "speed": _fnum(newest.get("speed")),
        "comment": newest.get("comment", "") or "",
        "source": "aprs.fi",
    }
    with slock:
        state["aprs_last_position"] = new_pos
    _save_position(new_pos)
    log.info("aprs.fi backfill: updated position from %s at %s (%.4f, %.4f)",
             new_pos["callsign"], iso, lat, lon)


def _aprs_listener_loop():
    """Background thread: connect to APRS-IS, listen, send ACKs via same connection."""
    global _aprs_running, _aprs_conn
    backoff = 15  # Start with 15 seconds, increase on repeated failures
    while _aprs_running:
        with cfglock:
            ap = config.get("aprs", {})
            enabled = ap.get("enabled", False)
            base = config.get("home_callsign", "").strip().upper()
        if not enabled or not base:
            with slock:
                state["aprs_connected"] = False
                state["aprs_error"] = None if not enabled else "Home callsign required"
            time.sleep(5)
            backoff = 15
            continue
        try:
            import aprslib
            # Suppress aprslib's internal error spam (it logs every failed recv() individually)
            logging.getLogger("aprslib").setLevel(logging.CRITICAL)
            home = _aprs_callsign()
            pc = ap.get("passcode", "") or str(aprs_passcode(base))
            # Extract base callsigns (strip -SSID and /P /M /QRP suffixes)
            def clean_call(c):
                return c.upper().split("-")[0].split("/")[0].strip()
            base_only = clean_call(base)
            watch_bases = {base_only}
            with cfglock:
                watch = config.get("watch_callsigns", [])
            for c in watch:
                cleaned = clean_call(c)
                if cleaned:
                    watch_bases.add(cleaned)
            # Wildcard (*) is required to match all SSIDs (e.g., KK4ODA* matches -7, -9, etc.)
            filt = "b/" + "/".join(c + "*" for c in sorted(watch_bases))

            log.info("APRS-IS connecting as %s filter: %s", home, filt)
            ais = aprslib.IS(home, passwd=pc,
                             host=ap.get("server", "rotate.aprs2.net"),
                             port=int(ap.get("port", 14580)))
            ais.set_filter(filt)
            ais.connect()
            with _aprs_conn_lock:
                _aprs_conn = ais
            with slock:
                state["aprs_connected"] = True
                state["aprs_error"] = None
            log.info("APRS-IS connected successfully")
            backoff = 15  # Reset backoff on successful connection

            # Wrapper to log all incoming packets before processing
            def _aprs_callback(packet):
                from_c = packet.get("from", "?")
                fmt = packet.get("format", "?")
                raw = packet.get("raw", "")[:80]
                log.info("APRS-IS RX: %s (format=%s) %s", from_c, fmt, raw)
                _process_aprs_packet(packet)

            # immortal=False — we handle reconnection with backoff
            ais.consumer(_aprs_callback, immortal=False, raw=False)
        except ImportError:
            log.error("aprslib not installed")
            with slock:
                state["aprs_connected"] = False
                state["aprs_error"] = "aprslib not installed (pip install aprslib)"
            time.sleep(30)
            continue
        except Exception as e:
            log.error("APRS-IS connection lost: %s", e)
        # Connection dropped — clean up and wait with backoff
        with _aprs_conn_lock:
            _aprs_conn = None
        with slock:
            state["aprs_connected"] = False
            state["aprs_error"] = f"Reconnecting in {backoff}s..."
        log.info("APRS-IS reconnecting in %d seconds...", backoff)
        # Sleep in short increments, checking internet so we reconnect quickly when it's back
        for _ in range(backoff):
            if not _aprs_running:
                break
            time.sleep(1)
            if _check_internet(timeout=2):
                log.info("Internet detected — reconnecting APRS-IS now")
                break
        backoff = min(backoff * 2, 300)  # Double backoff, cap at 5 minutes


# ---------------------------------------------------------------------------
# Soundmodem / KISS TCP engine
# ---------------------------------------------------------------------------
_kiss_thread = None
_kiss_running = False
_kiss_sock = None
_kiss_sock_lock = threading.Lock()
_soundmodem_proc = None

FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD

def _kiss_unescape(data):
    """Remove KISS escape sequences from frame data."""
    out = bytearray()
    i = 0
    while i < len(data):
        if data[i] == FESC:
            i += 1
            if i < len(data):
                if data[i] == TFEND:
                    out.append(FEND)
                elif data[i] == TFESC:
                    out.append(FESC)
                else:
                    out.append(data[i])
        else:
            out.append(data[i])
        i += 1
    return bytes(out)

def _kiss_escape(data):
    """Add KISS escape sequences to frame data."""
    out = bytearray()
    for b in data:
        if b == FEND:
            out.extend([FESC, TFEND])
        elif b == FESC:
            out.extend([FESC, TFESC])
        else:
            out.append(b)
    return bytes(out)

def _ax25_to_aprs_string(frame):
    """Parse raw AX.25 frame bytes into an APRS-compatible text string for aprslib.parse()."""
    try:
        if len(frame) < 16:
            return None
        # Destination: bytes 0-6, Source: bytes 7-13
        # Each callsign char is shifted left by 1
        def decode_call(b):
            call = ""
            for i in range(6):
                ch = (b[i] >> 1) & 0x7F
                if ch != 32:  # space
                    call += chr(ch)
            ssid = (b[6] >> 1) & 0x0F
            if ssid:
                call += f"-{ssid}"
            return call

        dest = decode_call(frame[0:7])
        src = decode_call(frame[7:14])

        # Digipeater path
        path = []
        idx = 14
        # Check if address extension bit is set (bit 0 of last byte)
        if not (frame[13] & 0x01):
            while idx + 7 <= len(frame):
                digi = decode_call(frame[idx:idx+7])
                has_been = (frame[idx+6] & 0x80) != 0
                if has_been:
                    digi += "*"
                path.append(digi)
                if frame[idx+6] & 0x01:
                    idx += 7
                    break
                idx += 7

        # Control + PID
        if idx + 2 > len(frame):
            return None
        # control = frame[idx]
        idx += 2  # skip control + PID bytes

        # Info field
        info = frame[idx:].decode('ascii', errors='replace')

        # Build APRS-IS style string
        path_str = ",".join(path)
        if path_str:
            return f"{src}>{dest},{path_str}:{info}"
        return f"{src}>{dest}:{info}"
    except Exception as e:
        log.warning("AX.25 decode error: %s", e)
        return None

def _kiss_send_frame(frame_bytes):
    """Send a raw AX.25 frame via KISS TCP to Soundmodem for RF transmission."""
    with _kiss_sock_lock:
        if _kiss_sock:
            try:
                kiss_frame = bytes([FEND, 0x00]) + _kiss_escape(frame_bytes) + bytes([FEND])
                _kiss_sock.sendall(kiss_frame)
                log.info("KISS TX: %d bytes", len(frame_bytes))
                return True
            except Exception as e:
                log.warning("KISS TX failed: %s", e)
    return False

def _aprs_string_to_ax25(aprs_str):
    """Convert an APRS text string to raw AX.25 UI frame bytes for KISS transmission."""
    try:
        # Parse: SRC>DEST,PATH:INFO
        header, info = aprs_str.split(":", 1)
        parts = header.split(",")
        src_dest = parts[0]
        path = parts[1:] if len(parts) > 1 else []
        src, dest = src_dest.split(">")

        def encode_call(callsign, last=False):
            # Strip * from digipeated flag
            has_been = callsign.endswith("*")
            if has_been:
                callsign = callsign[:-1]
            call = callsign.split("-")
            base = call[0].upper().ljust(6)
            ssid = int(call[1]) if len(call) > 1 else 0
            out = bytearray()
            for ch in base[:6]:
                out.append(ord(ch) << 1)
            ssid_byte = 0x60 | (ssid << 1)
            if last:
                ssid_byte |= 0x01
            if has_been:
                ssid_byte |= 0x80
            out.append(ssid_byte)
            return bytes(out)

        frame = bytearray()
        frame.extend(encode_call(dest))
        if path:
            frame.extend(encode_call(src))
            for i, p in enumerate(path):
                frame.extend(encode_call(p, last=(i == len(path)-1)))
        else:
            frame.extend(encode_call(src, last=True))

        # Control: UI frame = 0x03, PID: no layer 3 = 0xF0
        frame.extend([0x03, 0xF0])
        frame.extend(info.encode('ascii', errors='replace'))
        return bytes(frame)
    except Exception as e:
        log.warning("APRS->AX.25 encode error: %s", e)
        return None

# ---------------------------------------------------------------------------
# VarAC auto-launch
# ---------------------------------------------------------------------------
_varac_proc = None

def _is_process_running(exe_name):
    """Check if a process with the given executable name is already running (Windows)."""
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/NH"],
                capture_output=True, text=True, timeout=5
            )
            return exe_name.lower() in result.stdout.lower()
        else:
            result = subprocess.run(["pgrep", "-f", exe_name],
                                    capture_output=True, timeout=5)
            return result.returncode == 0
    except Exception:
        return False

def _read_varac_ini(ini_path):
    """Read a VarAC .ini file with encoding fallback (utf-8 -> cp1252 -> latin-1)."""
    cp = configparser.ConfigParser()
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            cp.read(ini_path, encoding=enc)
            return cp
        except (UnicodeDecodeError, UnicodeError):
            continue
    return None


def get_bbs_directory():
    """Return the BBS directory path. Priority: HamLink config override > VarAC .ini > None."""
    with cfglock:
        override = config.get("bbs_directory", "").strip()
    if override:
        return override
    ini_path = _varac_ini_path()
    if ini_path:
        try:
            cp = _read_varac_ini(ini_path)
            if cp:
                bbs_dir = cp.get("BBS", "BBSDirectory", fallback="")
                if bbs_dir:
                    return bbs_dir
        except Exception as e:
            log.warning("Failed to read BBS dir from %s: %s", ini_path, e)
    return None


def _varac_ini_path():
    """Return the full path to the active VarAC .ini file, or None."""
    with cfglock:
        varac_exe = config.get("varac_exe_path", "")
        profile = config.get("varac_profile", "")
    if varac_exe and os.path.isfile(varac_exe):
        varac_dir = os.path.dirname(varac_exe)
        ini_name = profile if profile else "VarAC.ini"
        ini_path = os.path.join(varac_dir, ini_name)
        if os.path.isfile(ini_path):
            return ini_path
    return None


def get_varac_frequency():
    """Return the current VarAC frequency in MHz (e.g. '7.105') from LastFrequency in the ini."""
    ini_path = _varac_ini_path()
    if not ini_path:
        return None
    try:
        cp = _read_varac_ini(ini_path)
        if not cp:
            return None
        raw = cp.get("RIG_CONTROL", "LastFrequency", fallback="")
        if raw:
            # VarAC stores "7.105.000" (Hz with dot separators) -> 7.105 MHz
            hz = int(raw.strip().replace(".", ""))
            return f"{hz / 1_000_000:.3f}"
    except Exception as e:
        log.warning("Failed to read VarAC frequency: %s", e)
    return None


def get_varac_next_qsy():
    """Return the next scheduled QSY as (utc_time_str, freq_mhz_str) or None."""
    ini_path = _varac_ini_path()
    if not ini_path:
        return None
    varac_dir = os.path.dirname(ini_path)
    # Check for custom schedule path first, then default
    try:
        cp = _read_varac_ini(ini_path)
        if not cp:
            return None
        custom = cp.get("RIG_CONTROL", "FrequencyScheduleCustomFilePath", fallback="").strip()
        sched_enabled = cp.get("RIG_CONTROL", "FrequencySchedule", fallback="OFF").strip().upper()
    except Exception:
        return None
    if sched_enabled != "ON":
        return None
    sched_path = custom if custom and os.path.isfile(custom) else os.path.join(varac_dir, "VarAC_frequency_schedule.conf")
    if not os.path.isfile(sched_path):
        return None
    try:
        entries = []
        with open(sched_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(",")
                if len(parts) == 2:
                    t_str, f_str = parts[0].strip(), parts[1].strip()
                    entries.append((t_str, f_str))
        if not entries:
            return None
        now = datetime.now(timezone.utc)
        now_minutes = now.hour * 60 + now.minute
        # Find next QSY after current time
        for t_str, f_str in sorted(entries, key=lambda x: x[0]):
            h, m = map(int, t_str.split(":"))
            entry_minutes = h * 60 + m
            if entry_minutes > now_minutes:
                hz = int(f_str.replace(".", ""))
                mhz = hz / 1_000_000
                return (t_str, f"{mhz:.3f}")
        # Wrap around to tomorrow's first entry
        first = sorted(entries, key=lambda x: x[0])[0]
        hz = int(first[1].replace(".", ""))
        mhz = hz / 1_000_000
        return (first[0], f"{mhz:.3f}")
    except Exception as e:
        log.warning("Failed to parse frequency schedule: %s", e)
    return None


def varac_send_broadcast(message, to="ALL"):
    """Send a VarAC broadcast by automating the VarAC UI.
    Finds VarAC window, opens broadcast dialog, fills fields, and clicks send.
    Position-independent — uses window handles, not screen coordinates.
    Returns (ok, error_string)."""
    if sys.platform != "win32":
        return False, "VarAC broadcast automation only supported on Windows"
    try:
        import win32gui
    except ImportError:
        return False, "pywin32 not installed (pip install pywin32)"

    import ctypes
    user32 = ctypes.windll.user32
    WM_SETTEXT = 0x000C
    BM_CLICK = 0x00F5

    def _send_text(hwnd, text):
        """Set text on a control using SendMessageW (works across 32/64-bit)."""
        user32.SendMessageW(hwnd, WM_SETTEXT, 0, text)

    def _find_window_by_title(pattern):
        """Find a top-level window whose title contains pattern."""
        result = []
        def callback(h, _):
            if win32gui.IsWindowVisible(h):
                title = win32gui.GetWindowText(h)
                if re.search(pattern, title, re.IGNORECASE):
                    result.append(h)
            return True
        win32gui.EnumWindows(callback, None)
        return result[0] if result else None

    def _get_children(hwnd):
        children = []
        def callback(h, _):
            children.append(h)
            return True
        try:
            win32gui.EnumChildWindows(hwnd, callback, None)
        except Exception:
            pass
        return children

    def _find_child_by_text(parent, text):
        for h in _get_children(parent):
            try:
                if win32gui.GetWindowText(h) == text and win32gui.IsWindowVisible(h):
                    return h
            except Exception:
                pass
        return None

    def _find_edit_near_label(dialog, label_text):
        """Find an Edit control that is a sibling of a label with the given text."""
        children = _get_children(dialog)
        label_hwnd = None
        for h in children:
            try:
                if win32gui.GetWindowText(h) == label_text:
                    label_hwnd = h
                    break
            except Exception:
                pass
        if not label_hwnd:
            return None
        label_rect = win32gui.GetWindowRect(label_hwnd)
        # Find the closest Edit control to the right of or below the label
        best = None
        best_dist = 99999
        for h in children:
            try:
                cls = win32gui.GetClassName(h)
                if "Edit" not in cls and "edit" not in cls.lower():
                    continue
                r = win32gui.GetWindowRect(h)
                # Edit should be to the right of or below the label
                dx = r[0] - label_rect[0]
                dy = r[1] - label_rect[1]
                dist = abs(dy) * 2 + abs(dx)  # weight vertical proximity
                if abs(dy) < 100 and dx > -50 and dist < best_dist:
                    best = h
                    best_dist = dist
            except Exception:
                pass
        return best

    # Step 1: Find VarAC main window
    varac_hwnd = _find_window_by_title(r"VarAC.*V\d+")
    if not varac_hwnd:
        return False, "VarAC window not found"
    log.info("VarAC broadcast: found main window hwnd=%d", varac_hwnd)

    # Step 2: Find and click the BROADCAST button on the main window
    broadcast_btn = _find_child_by_text(varac_hwnd, "BROADCAST")
    if not broadcast_btn:
        return False, "BROADCAST button not found in VarAC"
    win32gui.SendMessage(broadcast_btn, BM_CLICK, 0, 0)
    log.info("VarAC broadcast: clicked BROADCAST button")
    time.sleep(0.8)

    # Step 3: Find the broadcast dialog
    dialog_hwnd = _find_window_by_title("Broadcast message")
    if not dialog_hwnd:
        return False, "Broadcast dialog did not open"
    log.info("VarAC broadcast: dialog opened hwnd=%d", dialog_hwnd)

    # Step 4: Find TO field — it's a ComboBox with an inner Edit control
    to_edit = None
    for h in _get_children(dialog_hwnd):
        cls = win32gui.GetClassName(h)
        if "COMBOBOX" in cls.upper():
            # Get the inner Edit of the ComboBox
            for sh in _get_children(h):
                if "Edit" in win32gui.GetClassName(sh):
                    to_edit = sh
                    break
            if to_edit:
                break
    if not to_edit:
        log.warning("VarAC broadcast: TO ComboBox not found, trying any Edit near TO label")
        to_edit = _find_edit_near_label(dialog_hwnd, "TO:")
    if to_edit:
        _send_text(to_edit, to)
        log.info("VarAC broadcast: set TO=%s", to)
    else:
        log.warning("VarAC broadcast: could not find TO field")

    # Step 5: Find MESSAGE field — it's a WPF HwndWrapper that needs click + WM_CHAR
    msg_field = None
    children = _get_children(dialog_hwnd)
    for h in children:
        try:
            cls = win32gui.GetClassName(h)
            if "HwndWrapper" in cls:
                msg_field = h
                break
        except Exception:
            pass
    if not msg_field:
        # Fallback: find the largest control in the dialog (the message area)
        best_area = 0
        for h in children:
            try:
                r = win32gui.GetWindowRect(h)
                area = (r[2] - r[0]) * (r[3] - r[1])
                if area > best_area and area > 5000 and h != to_edit:
                    msg_field = h
                    best_area = area
            except Exception:
                pass
    if msg_field:
        # Bring dialog to foreground and click on the message field to focus it
        user32.SetForegroundWindow(dialog_hwnd)
        time.sleep(0.2)
        r = win32gui.GetWindowRect(msg_field)
        cx, cy = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
        user32.SetCursorPos(cx, cy)
        time.sleep(0.1)
        user32.mouse_event(2, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
        user32.mouse_event(4, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
        time.sleep(0.2)
        # Type message character by character via WM_CHAR
        msg_text = message[:81]
        for ch in msg_text:
            user32.SendMessageW(msg_field, 0x0102, ord(ch), 0)  # WM_CHAR
        log.info("VarAC broadcast: typed MESSAGE (%d chars)", len(msg_text))
    else:
        # Close dialog and bail
        close_btn = _find_child_by_text(dialog_hwnd, "CLOSE")
        if close_btn:
            win32gui.SendMessage(close_btn, BM_CLICK, 0, 0)
        return False, "Could not find MESSAGE field in broadcast dialog"

    # Step 6: Click BROADCAST AND CLOSE
    send_btn = _find_child_by_text(dialog_hwnd, "BROADCAST AND CLOSE")
    if not send_btn:
        send_btn = _find_child_by_text(dialog_hwnd, "BROADCAST")
    if not send_btn:
        close_btn = _find_child_by_text(dialog_hwnd, "CLOSE")
        if close_btn:
            win32gui.SendMessage(close_btn, BM_CLICK, 0, 0)
        return False, "Could not find BROADCAST button in dialog"

    win32gui.SendMessage(send_btn, BM_CLICK, 0, 0)
    log.info("VarAC broadcast sent: TO=%s MSG=%s", to, message[:60])
    return True, ""


def varac_retrieve_relay(relay_callsign):
    """Retrieve VMail from a relay station by automating VarAC's relay notification dialog.
    Uses UI Automation to click the RELAY status bar label, then finds and double-clicks
    the callsign in the DataGridView. Returns (ok, error_string)."""
    if sys.platform != "win32":
        return False, "VarAC relay automation only supported on Windows"
    try:
        import win32gui
    except ImportError:
        return False, "pywin32 not installed (pip install pywin32)"
    try:
        import comtypes, comtypes.client
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen.UIAutomationClient import CUIAutomation, IUIAutomation
    except Exception as e:
        return False, f"UI Automation not available: {e}"

    import ctypes
    user32 = ctypes.windll.user32
    BM_CLICK = 0x00F5

    def _find_window_by_title(pattern):
        result = []
        def callback(h, _):
            if win32gui.IsWindowVisible(h):
                title = win32gui.GetWindowText(h)
                if re.search(pattern, title, re.IGNORECASE):
                    result.append(h)
            return True
        win32gui.EnumWindows(callback, None)
        return result[0] if result else None

    def _get_children(hwnd):
        children = []
        def callback(h, _):
            children.append(h)
            return True
        try:
            win32gui.EnumChildWindows(hwnd, callback, None)
        except Exception:
            pass
        return children

    def _find_child_by_text(parent, text):
        for h in _get_children(parent):
            try:
                if win32gui.GetWindowText(h).strip() == text.strip() and win32gui.IsWindowVisible(h):
                    return h
            except Exception:
                pass
        return None

    relay_call_upper = relay_callsign.upper().strip()
    log.info("Relay retrieve: starting for callsign %s", relay_callsign)

    # Step 1: Find VarAC main window
    varac_hwnd = _find_window_by_title(r"VarAC.*V\d+")
    if not varac_hwnd:
        return False, "VarAC window not found"
    log.info("Relay retrieve: found VarAC main window hwnd=%d", varac_hwnd)

    # Step 2: Click the RELAY label in VarAC's status bar using UI Automation
    # The RELAY label is a .NET ToolStripStatusLabel, not a Win32 button
    try:
        uia = comtypes.CoCreateInstance(
            CUIAutomation._reg_clsid_, interface=IUIAutomation,
            clsctx=comtypes.CLSCTX_INPROC_SERVER)
        root = uia.ElementFromHandle(varac_hwnd)
        true_cond = uia.CreateTrueCondition()
        elements = root.FindAll(4, true_cond)  # TreeScope_Descendants
        relay_label = None
        for i in range(elements.Length):
            el = elements.GetElement(i)
            name = (el.CurrentName or "").strip()
            if name == "RELAY":
                rect = el.CurrentBoundingRectangle
                if rect.right > rect.left:  # Has valid bounds
                    relay_label = rect
                    break
    except Exception as e:
        return False, f"UI Automation error finding RELAY label: {e}"

    if not relay_label:
        return False, "RELAY label not found in VarAC status bar"

    # Bring VarAC to foreground and click the RELAY label
    user32.SetForegroundWindow(varac_hwnd)
    time.sleep(0.3)
    cx = (relay_label.left + relay_label.right) // 2
    cy = (relay_label.top + relay_label.bottom) // 2
    user32.SetCursorPos(cx, cy)
    time.sleep(0.15)
    user32.mouse_event(2, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
    user32.mouse_event(4, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
    log.info("Relay retrieve: clicked RELAY status bar label at (%d,%d)", cx, cy)
    time.sleep(1.2)

    # Step 3: Find the relay notification dialog
    dialog_hwnd = _find_window_by_title("VMail Relay notification")
    if not dialog_hwnd:
        dialog_hwnd = _find_window_by_title("VMail Relay")
    if not dialog_hwnd:
        return False, "Relay notification dialog did not open"
    log.info("Relay retrieve: dialog opened hwnd=%d", dialog_hwnd)

    # Step 4: Find the callsign in the DataGridView using UI Automation
    # The grid has rows with "Callsign Row N" cells containing the callsign text
    try:
        dialog_el = uia.ElementFromHandle(dialog_hwnd)
        d_elements = dialog_el.FindAll(4, true_cond)
        target_rect = None
        for i in range(d_elements.Length):
            el = d_elements.GetElement(i)
            name = (el.CurrentName or "")
            # Match "Callsign Row N" cells — the Value property contains the actual callsign
            if name.startswith("Callsign Row"):
                try:
                    # Get the Value pattern to read cell content
                    from comtypes.gen.UIAutomationClient import IUIAutomationValuePattern
                    val_pattern = el.GetCurrentPattern(10002)  # UIA_ValuePatternId
                    if val_pattern:
                        vp = val_pattern.QueryInterface(IUIAutomationValuePattern)
                        cell_value = (vp.CurrentValue or "").upper().strip()
                        if relay_call_upper in cell_value:
                            target_rect = el.CurrentBoundingRectangle
                            log.info("Relay retrieve: found callsign '%s' in %s via Value pattern",
                                     cell_value, name)
                            break
                except Exception:
                    pass
                # Fallback: try reading the cell's Name property for the callsign
                # Some DataGridView implementations include the value in accessible name
                rect = el.CurrentBoundingRectangle
                if rect.right > rect.left:
                    # We'll try double-clicking each Callsign cell row by row as last resort
                    pass

        # If Value pattern didn't work, try clicking on the Callsign column cells
        # and reading text, or just iterate all row elements looking for the callsign text
        if not target_rect:
            for i in range(d_elements.Length):
                el = d_elements.GetElement(i)
                name = (el.CurrentName or "").upper()
                if relay_call_upper in name:
                    rect = el.CurrentBoundingRectangle
                    if rect.right > rect.left:
                        target_rect = rect
                        log.info("Relay retrieve: found callsign in element name '%s'",
                                 el.CurrentName)
                        break

    except Exception as e:
        # Close dialog and report
        close_btn = _find_child_by_text(dialog_hwnd, "CLOSE")
        if close_btn:
            win32gui.SendMessage(close_btn, BM_CLICK, 0, 0)
        return False, f"UI Automation error searching dialog: {e}"

    if not target_rect:
        close_btn = _find_child_by_text(dialog_hwnd, "CLOSE")
        if close_btn:
            win32gui.SendMessage(close_btn, BM_CLICK, 0, 0)
        return False, f"Callsign {relay_callsign} not found in relay notification dialog"

    # Step 5: Double-click the callsign cell to trigger VarAC's connect+retrieve
    user32.SetForegroundWindow(dialog_hwnd)
    time.sleep(0.2)
    cx = (target_rect.left + target_rect.right) // 2
    cy = (target_rect.top + target_rect.bottom) // 2
    user32.SetCursorPos(cx, cy)
    time.sleep(0.15)
    # Double-click using mouse events
    user32.mouse_event(2, 0, 0, 0, 0)  # LEFTDOWN
    user32.mouse_event(4, 0, 0, 0, 0)  # LEFTUP
    time.sleep(0.05)
    user32.mouse_event(2, 0, 0, 0, 0)  # LEFTDOWN
    user32.mouse_event(4, 0, 0, 0, 0)  # LEFTUP
    log.info("Relay retrieve: double-clicked callsign %s at (%d,%d)", relay_callsign, cx, cy)

    # Step 6: Wait for VarAC to process (QSY + connect)
    time.sleep(2.0)

    # Check if dialog closed (VarAC may close it after starting connection)
    if not win32gui.IsWindow(dialog_hwnd) or not win32gui.IsWindowVisible(dialog_hwnd):
        log.info("Relay retrieve: dialog closed — VarAC is connecting to relay")
    else:
        log.info("Relay retrieve: dialog still open — VarAC may be processing")
        close_btn = _find_child_by_text(dialog_hwnd, "CLOSE")
        if close_btn:
            win32gui.SendMessage(close_btn, BM_CLICK, 0, 0)
            log.info("Relay retrieve: closed relay dialog")

    return True, ""


def _launch_varac():
    """Auto-launch VarAC if configured and not already running."""
    global _varac_proc
    if _varac_proc and _varac_proc.poll() is None:
        log.info("VarAC already running (PID %d)", _varac_proc.pid)
        return
    with cfglock:
        exe = config.get("varac_exe_path", "")
        profile = config.get("varac_profile", "")
    if not exe:
        return  # Not configured, silently skip
    if not os.path.isfile(exe):
        log.warning("VarAC exe not found at: %s", exe)
        return
    # Check if VarAC is already running
    exe_name = os.path.basename(exe)
    if _is_process_running(exe_name):
        log.info("VarAC already running (%s) — skipping launch", exe_name)
        return
    try:
        cmd = [exe]
        if profile:
            cmd.append(profile)
        log.info("Launching VarAC: %s", " ".join(cmd))
        _varac_proc = subprocess.Popen(cmd, cwd=os.path.dirname(exe))
        log.info("VarAC launched (PID %d)%s", _varac_proc.pid,
                 f" with profile {profile}" if profile else "")
        time.sleep(5)  # VarAC takes longer to initialize
    except Exception as e:
        log.error("Failed to launch VarAC: %s", e)

def _launch_soundmodem():
    """Auto-launch Soundmodem executable."""
    global _soundmodem_proc
    if _soundmodem_proc and _soundmodem_proc.poll() is None:
        log.info("Soundmodem already running (PID %d)", _soundmodem_proc.pid)
        return
    with cfglock:
        sm = config.get("soundmodem", {})
        exe = sm.get("exe_path", "")
        auto = sm.get("auto_launch", True)
        kiss_host = sm.get("kiss_host", "127.0.0.1")
        kiss_port = int(sm.get("kiss_port", 8100))
    if not auto:
        log.info("Soundmodem auto-launch disabled")
        return
    if not exe:
        log.info("Soundmodem exe path not set")
        return
    if not os.path.isfile(exe):
        log.warning("Soundmodem exe not found at: %s", exe)
        return
    # Check if Soundmodem is already running on the KISS port
    if _port_open(kiss_host, kiss_port):
        log.info("Soundmodem already running on %s:%d (external instance) — skipping launch", kiss_host, kiss_port)
        return
    try:
        log.info("Launching Soundmodem: %s", exe)
        _soundmodem_proc = subprocess.Popen([exe], cwd=os.path.dirname(exe))
        log.info("Soundmodem launched (PID %d)", _soundmodem_proc.pid)
        time.sleep(3)
    except Exception as e:
        log.error("Failed to launch Soundmodem: %s", e)

def _kiss_listener_loop():
    """Background thread: connect to Soundmodem KISS TCP port, read frames."""
    global _kiss_running, _kiss_sock
    while _kiss_running:
        with cfglock:
            sm = config.get("soundmodem", {})
            enabled = sm.get("enabled", False)
            host = sm.get("kiss_host", "127.0.0.1")
            port = int(sm.get("kiss_port", 8100))
        if not enabled:
            with slock:
                state["kiss_connected"] = False
                state["kiss_error"] = None
            time.sleep(5)
            continue
        _kiss_last_error = ""
        try:
            log.info("KISS connecting to %s:%d", host, port)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((host, port))
            sock.settimeout(30)  # 30s timeout so we periodically check _kiss_running
            with _kiss_sock_lock:
                _kiss_sock = sock
            with slock:
                state["kiss_connected"] = True
                state["kiss_error"] = None
            log.info("KISS TCP connected to Soundmodem")

            buf = bytearray()
            rx_count = 0
            raw_bytes = 0
            raw_recv_count = 0
            drop_count = 0
            last_heartbeat = time.time()
            while _kiss_running:
                try:
                    data = sock.recv(1024)
                    if not data:
                        log.warning("KISS: socket returned empty — connection closed by Soundmodem")
                        break
                    raw_bytes += len(data)
                    raw_recv_count += 1
                    # Log every raw receive (concise after first few)
                    if raw_recv_count <= 5:
                        log.info("KISS raw recv #%d: %d bytes (total %d), hex: %s",
                                 raw_recv_count, len(data), raw_bytes, data[:30].hex())
                    else:
                        log.debug("KISS raw recv #%d: %d bytes (total %d)",
                                  raw_recv_count, len(data), raw_bytes)
                    buf.extend(data)
                    # Extract complete KISS frames
                    while FEND in buf:
                        idx = buf.index(FEND)
                        if idx == 0:
                            buf = buf[1:]
                            continue
                        frame = bytes(buf[:idx])
                        buf = buf[idx+1:]
                        if len(frame) < 2:
                            log.debug("KISS: skipping short frame (%d bytes)", len(frame))
                            continue
                        cmd = frame[0]
                        if cmd & 0x0F != 0x00:
                            log.debug("KISS: skipping non-data frame (cmd=0x%02x)", cmd)
                            continue  # Not a data frame
                        ax25_raw = _kiss_unescape(frame[1:])
                        aprs_str = _ax25_to_aprs_string(ax25_raw)
                        if aprs_str:
                            rx_count += 1
                            # Log all received packets — messages get special prefix
                            if "::" in aprs_str:
                                log.info("KISS RX [MSG]: %s", aprs_str[:100])
                            else:
                                log.info("KISS RX [%d]: %s", rx_count, aprs_str[:80])
                            try:
                                import aprslib
                                # Skip packets from our exact home station callsign (digipeated back)
                                # Only skip our SSID (e.g., KK4ODA-1), not the traveler's (e.g., KK4ODA-7)
                                pkt_from = aprs_str.split(">")[0].strip().upper()
                                with cfglock:
                                    our_call = config.get("home_callsign", "").strip().upper()
                                    our_ssid = config.get("aprs", {}).get("home_ssid", "-5")
                                our_full = (our_call + our_ssid if our_ssid and not our_call.endswith(our_ssid)
                                            else our_call).upper() if our_call else ""
                                if our_full and pkt_from == our_full:
                                    log.debug("KISS RX: skipping own packet from %s", pkt_from)
                                    continue
                                # Check for third-party packets (iGate relays)
                                # Format: }CALL>PATH::DEST :payload
                                info = aprs_str.split(":", 1)[1] if ":" in aprs_str else ""
                                if info.startswith("}"):
                                    inner = info[1:]  # Strip the } prefix
                                    log.info("KISS RX: third-party packet, inner: %s", inner[:80])
                                    try:
                                        parsed = aprslib.parse(inner)
                                        parsed["_via_rf"] = True
                                        _process_aprs_packet(parsed)
                                    except Exception as e3p:
                                        log.info("KISS: third-party parse failed: %s (inner: %s)", e3p, inner[:60])
                                    continue
                                parsed = aprslib.parse(aprs_str)
                                parsed["_via_rf"] = True
                                _process_aprs_packet(parsed)
                            except Exception as e:
                                # Suppress known unsupported format warnings (telemetry, etc.)
                                estr = str(e)
                                if "not supported" in estr.lower() or "format is not" in estr.lower():
                                    log.debug("KISS: aprslib can't parse (unsupported format): %s", aprs_str[:60])
                                else:
                                    log.warning("KISS parse error for '%s': %s", aprs_str[:60], e)
                        else:
                            drop_count += 1
                            log.info("KISS: AX.25 decode returned None for frame (%d bytes, hex: %s)",
                                     len(ax25_raw), ax25_raw[:20].hex() if len(ax25_raw) >= 20 else ax25_raw.hex())
                except socket.timeout:
                    # Log timeout occasionally so we know the loop is alive
                    log.debug("KISS: recv timeout (normal), buf=%d bytes", len(buf))
                except Exception as e:
                    log.error("KISS read error: %s", e)
                    break
                # Periodic heartbeat so operator knows KISS is alive
                now = time.time()
                if now - last_heartbeat >= 60:  # Every 60 seconds during debug
                    log.info("KISS alive: %d decoded, %d dropped, %d raw recvs, %d bytes total, buf=%d",
                             rx_count, drop_count, raw_recv_count, raw_bytes, len(buf))
                    last_heartbeat = now

            sock.close()
            _kiss_last_error = "Disconnected"
        except Exception as e:
            log.error("KISS connection error: %s", e)
            _kiss_last_error = str(e)
        with _kiss_sock_lock:
            _kiss_sock = None
        with slock:
            state["kiss_connected"] = False
            state["kiss_error"] = _kiss_last_error
        log.info("KISS reconnecting in 10 seconds...")
        time.sleep(10)

def start_kiss():
    global _kiss_thread, _kiss_running
    if _kiss_thread and _kiss_thread.is_alive():
        return
    _kiss_running = True
    _kiss_thread = threading.Thread(target=_kiss_listener_loop, daemon=True)
    _kiss_thread.start()
    log.info("KISS listener thread started")

# ---------------------------------------------------------------------------
# Pat / Winlink engine
# ---------------------------------------------------------------------------
_pat_thread = None
_pat_running = False
_pat_proc = None
_pat_seen_ids = set()
_PAT_SEEN_FILE = os.path.join(APP_DIR, ".pat_seen_ids")

def _pat_load_seen():
    global _pat_seen_ids
    if os.path.isfile(_PAT_SEEN_FILE):
        try:
            with open(_PAT_SEEN_FILE, "r") as f:
                _pat_seen_ids = set(line.strip() for line in f if line.strip())
        except Exception:
            pass

def _pat_save_seen():
    try:
        with open(_PAT_SEEN_FILE, "w") as f:
            for mid in _pat_seen_ids:
                f.write(mid + "\n")
    except Exception:
        pass

_pat_load_seen()


def _pat_config_path():
    """Find Pat's config.json on Windows."""
    with cfglock:
        exe = config.get("pat", {}).get("exe_path", "")
    # Try standard Windows location first
    localappdata = os.environ.get("LOCALAPPDATA", "")
    candidates = []
    if localappdata:
        candidates.append(os.path.join(localappdata, "pat", "config.json"))
    # Also try ~/.wl2k/ (older Pat versions)
    home = os.path.expanduser("~")
    candidates.append(os.path.join(home, ".wl2k", "config.json"))
    # Try next to the exe
    if exe:
        candidates.append(os.path.join(os.path.dirname(exe), "config.json"))
    for p in candidates:
        if os.path.isfile(p):
            return p
    return candidates[0] if candidates else ""

def _pat_read_config():
    """Read Pat's config.json."""
    path = _pat_config_path()
    if path and os.path.isfile(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Failed to read Pat config: %s", e)
    return {}

def _pat_write_config(cfg):
    """Write Pat's config.json."""
    path = _pat_config_path()
    if not path:
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(cfg, f, indent=2)
        log.info("Pat config written to %s", path)
        return True
    except Exception as e:
        log.error("Failed to write Pat config: %s", e)
        return False

def _restart_pat():
    """Stop and restart Pat so it picks up config changes."""
    global _pat_proc
    if _pat_proc and _pat_proc.poll() is None:
        log.info("Stopping Pat (PID %d) for restart...", _pat_proc.pid)
        _pat_proc.terminate()
        try:
            _pat_proc.wait(timeout=5)
        except Exception:
            _pat_proc.kill()
        _pat_proc = None
    _launch_pat()

# ---------------------------------------------------------------------------
# VARA FM auto-launch
# ---------------------------------------------------------------------------
_varafm_proc = None

def _launch_varafm():
    """Auto-launch VARA FM modem if configured."""
    global _varafm_proc
    if _varafm_proc and _varafm_proc.poll() is None:
        log.info("VARA FM already running (PID %d)", _varafm_proc.pid)
        return
    with cfglock:
        exe = config.get("pat", {}).get("varafm_exe_path", "")
    if not exe:
        return  # Not configured, silently skip
    if not os.path.isfile(exe):
        log.warning("VARA FM exe not found at: %s", exe)
        return
    # Check if VARA FM is already running by testing its TCP port
    with cfglock:
        addr = config.get("pat", {}).get("varafm_addr", "localhost:8300")
    _host, port_str = addr.rsplit(":", 1)
    if _port_open("127.0.0.1", port_str):  # Always use 127.0.0.1 to avoid IPv6 hangs
        log.info("VARA FM already running on %s (external instance) — skipping launch", addr)
        return
    try:
        log.info("Launching VARA FM: %s", exe)
        _varafm_proc = subprocess.Popen([exe], cwd=os.path.dirname(exe),
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("VARA FM launched (PID %d)", _varafm_proc.pid)
        time.sleep(3)
        if _varafm_proc.poll() is not None:
            log.error("VARA FM exited immediately with code %d", _varafm_proc.returncode)
            _varafm_proc = None
        else:
            log.info("VARA FM is running")
    except Exception as e:
        log.error("Failed to launch VARA FM: %s", e)

def _launch_pat():
    """Auto-launch Pat Winlink client."""
    global _pat_proc
    if _pat_proc and _pat_proc.poll() is None:
        log.info("Pat already running (PID %d)", _pat_proc.pid)
        return
    with cfglock:
        pt = config.get("pat", {})
        exe = pt.get("exe_path", "")
        auto = pt.get("auto_launch", True)
        http_addr = pt.get("http_addr", "localhost:8080")
    if not auto:
        log.info("Pat auto-launch disabled in settings")
        return
    if not exe:
        log.warning("Pat exe path is empty — set it in Settings")
        return
    if not os.path.isfile(exe):
        log.warning("Pat exe not found at: %s", exe)
        return
    # Check if Pat is already running on the configured port
    host, port_str = http_addr.rsplit(":", 1)
    if _port_open("127.0.0.1", port_str):  # Always use 127.0.0.1 to avoid IPv6 hangs
        log.info("Pat already running on %s (external instance) — skipping launch", http_addr)
        return
    try:
        cmd = [exe, "http", "--addr", http_addr]
        log.info("Launching Pat: %s", " ".join(cmd))
        _pat_proc = subprocess.Popen(
            cmd,
            cwd=os.path.dirname(exe),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info("Pat launched (PID %d)", _pat_proc.pid)
        time.sleep(3)
        # Check if Pat is still running after 3 seconds
        if _pat_proc.poll() is not None:
            log.error("Pat exited immediately with code %d — check Pat configuration", _pat_proc.returncode)
            _pat_proc = None
        else:
            log.info("Pat is running on %s", http_addr)
    except Exception as e:
        log.error("Failed to launch Pat: %s", e)

def _pat_api_url():
    with cfglock:
        addr = config.get("pat", {}).get("http_addr", "localhost:8080")
    return f"http://{addr}/api"

def _pat_check_inbox():
    """Poll Pat's mailbox for new messages."""
    try:
        url = _pat_api_url() + "/mailbox/in"
        with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as resp:
            data = json.loads(resp.read().decode())

        if data:
            log.debug("Pat inbox: %d messages, first keys: %s", len(data), list(data[0].keys()) if data else "none")

        with cfglock:
            opname = config.get("operator_name", "") or ""
            home_tactical = config.get("pat", {}).get("home_tactical", "").strip().upper()
            traveler_tactical = config.get("pat", {}).get("traveler_tactical", "").strip().upper()

        seen_dirty = False
        for msg in data:
            mid = msg.get("MID", "") or msg.get("mid", "") or msg.get("Id", "") or msg.get("id", "")
            if not mid:
                # Try to generate a unique ID from subject+date
                mid = f"{msg.get('Subject', msg.get('subject', ''))}_{msg.get('Date', msg.get('date', ''))}"
            if mid in _pat_seen_ids:
                continue
            _pat_seen_ids.add(mid)
            seen_dirty = True

            # Handle different field name casing Pat might use
            subject = msg.get("Subject", "") or msg.get("subject", "")
            from_field = msg.get("From", msg.get("from", ""))
            if isinstance(from_field, dict):
                from_addr = from_field.get("Addr", "") or from_field.get("addr", "")
            elif isinstance(from_field, str):
                from_addr = from_field
            else:
                from_addr = str(from_field) if from_field else ""

            # Extract TO addresses
            to_field = msg.get("To", msg.get("to", []))
            to_addrs = []
            if isinstance(to_field, list):
                for t_entry in to_field:
                    if isinstance(t_entry, dict):
                        to_addrs.append((t_entry.get("Addr", "") or t_entry.get("addr", "")).upper())
                    elif isinstance(t_entry, str):
                        to_addrs.append(t_entry.upper())
            elif isinstance(to_field, str):
                to_addrs.append(to_field.upper())

            # Filter: only alert on messages FROM the traveler TO the home tactical address
            from_upper = from_addr.upper()
            from_match = False
            if traveler_tactical and traveler_tactical in from_upper:
                from_match = True
            # Also match if from a watched callsign
            if not from_match and _match(from_addr):
                from_match = True
            if not from_match:
                log.debug("Winlink skip: FROM=%s does not match traveler or watch list", from_addr)
                continue

            to_match = False
            if not home_tactical:
                to_match = True  # No home tactical configured — accept all
            else:
                for addr in to_addrs:
                    if home_tactical in addr:
                        to_match = True
                        break
            if not to_match:
                log.debug("Winlink skip: TO=%s does not match home tactical %s", to_addrs, home_tactical)
                continue

            body_text = msg.get("Body", "") or msg.get("body", "")
            # List endpoint doesn't include body — fetch individual message
            if not body_text and mid:
                try:
                    detail_url = _pat_api_url() + f"/mailbox/in/{mid}"
                    with urllib.request.urlopen(urllib.request.Request(detail_url), timeout=10) as detail_resp:
                        detail = json.loads(detail_resp.read().decode())
                    body_text = detail.get("Body", "") or detail.get("body", "")
                except Exception as e:
                    log.debug("Pat: could not fetch body for %s: %s", mid[:20], e)
            t = msg.get("Date", "") or msg.get("date", "") or datetime.now(timezone.utc).isoformat()
            name = opname or from_addr

            log.info("Winlink inbox msg: MID=%s From=%s To=%s Subject=%s Body=%d chars", mid[:20], from_addr, to_addrs, subject[:40], len(body_text))

            alert = {
                "id": f"winlink-{mid}",
                "type": "winlink",
                "time": t,
                "from_call": from_addr,
                "from_name": name,
                "to": ", ".join(to_addrs),
                "subject": subject,
                "message": body_text[:500] if body_text else "",
                "urgent": False,
                "friendly_time": _friendly_time(t),
            }

            with slock:
                existing = {a["id"] for a in state["pending_alerts"]} | {a["id"] for a in state["history"]} | state["acknowledged_ids"]
                if alert["id"] in existing:
                    continue
                state["pending_alerts"].append(alert)
                state["history"].append(alert)
                state["last_checkin_time"] = t
                state["last_checkin_from"] = name

            log.info("Winlink ALERT created: %s from %s", subject[:40], from_addr)
            log_message(alert, "incoming")
            start_speaker_alarm()
            send_pushover(f"Winlink from {name}", f"{subject}\n{body_text[:200]}", reply_channel="winlink")

        if seen_dirty:
            _pat_save_seen()  # one write per poll, not one per message

        with slock:
            state["pat_connected"] = True
            state["pat_error"] = None
            state["pat_last_check"] = datetime.now(timezone.utc).isoformat()

    except Exception as e:
        err_str = str(e)
        log.info("Pat inbox poll failed: %s (URL: %s)", err_str[:100], _pat_api_url() + "/mailbox/in")
        if "10061" in err_str or "Connection refused" in err_str:
            friendly = "Pat not running — start Pat or check exe path in Settings"
        elif "timed out" in err_str.lower():
            friendly = "Pat not responding"
        else:
            friendly = "Cannot reach Pat"
        log.debug("Pat inbox check: %s", err_str)
        with slock:
            state["pat_connected"] = False
            state["pat_error"] = friendly

def pat_send_message(to_addr, subject, body):
    """Send a Winlink message via Pat CLI compose command."""
    proc = None
    try:
        with cfglock:
            exe = config.get("pat", {}).get("exe_path", "")
            home_tac = config.get("pat", {}).get("home_tactical", "")
        # Fallback: read from Pat's own config if not in monitor config
        if not home_tac:
            pcfg = _pat_read_config()
            aux = pcfg.get("auxiliary_addresses", [])
            if aux:
                home_tac = aux[0] if isinstance(aux[0], str) else ""
        if not exe or not os.path.isfile(exe):
            return False, "Pat exe not configured"
        # Use tactical address as From if configured
        from_addr = home_tac.upper() if home_tac else ""
        cmd = [exe, "compose"]
        if from_addr:
            cmd.extend(["--from", from_addr])
        cmd.extend(["--subject", subject, to_addr])
        log.info("Pat compose: %s", " ".join(cmd))
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=os.path.dirname(exe)
        )
        stdout, stderr = proc.communicate(input=body.encode(), timeout=15)
        if proc.returncode != 0:
            err = stderr.decode().strip() or stdout.decode().strip() or f"Exit code {proc.returncode}"
            log.error("Pat compose failed: %s", err)
            return False, err
        log.info("Winlink message queued to %s: %s", to_addr, subject[:40])
        log_reply(to_addr, f"[Winlink] {subject}: {body[:100]}", channel="winlink")
        return True, ""
    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
        return False, "Pat compose timed out"
    except Exception as e:
        log.error("Winlink send failed: %s", e)
        return False, str(e)

def pat_connect_telnet():
    """Trigger Pat to sync via its HTTP API (POST /api/connect).
    Uses the running Pat HTTP server instead of spawning a separate process."""
    try:
        with cfglock:
            rf_fallback = config.get("pat", {}).get("rf_fallback", False)
            rf_gateway = config.get("pat", {}).get("rf_gateway", "")

        # Determine connection URL
        use_rf = False
        if rf_fallback and rf_gateway and not _check_internet(timeout=2):
            use_rf = True
            connect_url = f"varafm:///{rf_gateway}"
            log.info("No internet — Pat connecting via VARA FM to %s", rf_gateway)
        else:
            connect_url = "telnet"

        api_url = _pat_api_url() + "/connect"
        data = urllib.parse.urlencode({"url": connect_url}).encode()
        req = urllib.request.Request(api_url, data=data, method="POST")
        timeout_secs = 300 if use_rf else 120

        log.info("Pat sync via HTTP API: %s (url=%s)", api_url, connect_url)
        with urllib.request.urlopen(req, timeout=timeout_secs) as resp:
            result = json.loads(resp.read().decode())
        num_received = result.get("NumReceived", 0)
        log.info("Pat session completed (%s) — %d new messages",
                 "RF" if use_rf else "telnet", num_received)
        return True
    except Exception as e:
        err_str = str(e)
        if "500" in err_str:
            log.warning("Pat connect session failed (server error)")
        elif "timed out" in err_str.lower():
            log.warning("Pat connect timed out")
        else:
            log.warning("Pat connect failed: %s", err_str[:200])
        return False

def _winlink_check_position():
    """Query Winlink CMS RSS feed for the traveler's latest position report."""
    with cfglock:
        pos_enabled = config.get("pat", {}).get("position_reports", False)
        watch = config.get("watch_callsigns", [])
    if not pos_enabled or not watch:
        return
    # Use base callsign (no SSID)
    callsign = watch[0].upper().split("-")[0].split("/")[0].strip()
    if not callsign:
        return
    try:
        url = f"https://cms.winlink.org:444/rss/rsspositionreports.aspx?callsign={urllib.parse.quote(callsign)}"
        req = urllib.request.Request(url, headers={"User-Agent": "HamLink Radio"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
        root = ET.fromstring(raw)
        # Find the first (most recent) item in the RSS feed
        item = root.find(".//item")
        if item is None:
            log.info("Winlink position: no reports for %s", callsign)
            return
        title = (item.findtext("title") or "").strip()
        desc = (item.findtext("description") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        # Parse lat/lon from the RSS item
        lat, lon = None, None
        # Method 1: georss:point tag (standard GeoRSS)
        georss = item.find("{http://www.georss.org/georss}point")
        if georss is not None and georss.text:
            parts = georss.text.strip().split()
            if len(parts) == 2:
                lat, lon = float(parts[0]), float(parts[1])
        # Method 2: title field (Winlink CMS format: "Position report for CALL is LAT / LON")
        if lat is None and title:
            m = re.search(r'(-?[\d.]+)\s*/\s*(-?[\d.]+)', title)
            if m:
                lat, lon = float(m.group(1)), float(m.group(2))
        # Method 3: description field (e.g. "Latitude: 33.84, Longitude: -84.28")
        if lat is None and desc:
            lat_m = re.search(r'[Ll]at(?:itude)?[:\s]+(-?[\d.]+)', desc)
            lon_m = re.search(r'[Ll]on(?:gitude)?[:\s]+(-?[\d.]+)', desc)
            if lat_m and lon_m:
                lat, lon = float(lat_m.group(1)), float(lon_m.group(1))
        if lat is None or lon is None:
            log.info("Winlink position: could not parse coordinates from %s (title: %s)", callsign, title[:80])
            return
        # Parse timestamp
        pos_time = None
        if pub_date:
            try:
                from email.utils import parsedate_to_datetime
                pos_time = parsedate_to_datetime(pub_date).isoformat()
            except Exception:
                pos_time = pub_date
        with slock:
            state["winlink_last_position"] = {
                "callsign": callsign,
                "lat": lat, "lon": lon,
                "time": pos_time or datetime.now(timezone.utc).isoformat(),
                "comment": title or desc,
                "source": "winlink",
            }
        log.info("Winlink position for %s: %.4f, %.4f (%s)", callsign, lat, lon, title)
    except Exception as e:
        log.warning("Winlink position check failed: %s", e)


def _pat_poll_loop():
    """Background thread: sync with Winlink CMS and poll Pat inbox periodically.

    The poll_interval controls how often we trigger a full telnet sync.
    Between syncs, we check the local inbox every 15 seconds so messages
    arriving via Pat's own schedule or manual sync are detected quickly.
    """
    global _pat_running
    INBOX_CHECK_INTERVAL = 15  # seconds between local inbox checks
    _pat_cycle = 0
    while _pat_running:
        _pat_cycle += 1
        try:
            with cfglock:
                pt = config.get("pat", {})
                enabled = pt.get("enabled", False)
                inet_interval = max(pt.get("poll_interval", 60), 60)
                rf_interval = max(pt.get("rf_poll_interval", 10800), 600)
                rf_fallback = pt.get("rf_fallback", False)
                rf_gateway = pt.get("rf_gateway", "")
            if not enabled:
                with slock:
                    state["pat_connected"] = False
                    state["pat_error"] = None
                time.sleep(5)
                continue
            log.info("Pat poll cycle #%d starting", _pat_cycle)
            # Determine sync interval based on connectivity
            has_internet = _check_internet(timeout=2)
            use_rf = rf_fallback and rf_gateway and not has_internet
            sync_interval = rf_interval if use_rf else inet_interval
            # Full sync with CMS
            log.info("Pat: syncing with Winlink %s (next sync in %ds, internet=%s)",
                     "via RF gateway" if use_rf else "via internet", sync_interval,
                     "yes" if has_internet else "NO")
            pat_connect_telnet()
            _pat_check_inbox()
            if has_internet:
                _winlink_check_position()
            else:
                log.info("Winlink position check skipped (no internet)")
            # Set next sync time for UI countdown
            next_sync_epoch = time.time() + sync_interval
            with slock:
                state["pat_next_sync"] = datetime.fromtimestamp(next_sync_epoch, tz=timezone.utc).isoformat()
                state["pat_using_rf"] = use_rf
            # Between syncs, keep checking local inbox frequently
            elapsed = 0
            while elapsed < sync_interval and _pat_running:
                time.sleep(INBOX_CHECK_INTERVAL)
                elapsed += INBOX_CHECK_INTERVAL
                with cfglock:
                    still_enabled = config.get("pat", {}).get("enabled", False)
                if not still_enabled:
                    break
                try:
                    _pat_check_inbox()
                except Exception as e:
                    log.debug("Pat inbox check failed during wait: %s", e)
            log.info("Pat: sync interval elapsed (%ds), starting next cycle", sync_interval)
        except Exception as e:
            log.error("Pat poll loop error (will retry): %s", e)
            time.sleep(15)  # Avoid tight error loop

def start_pat():
    global _pat_thread, _pat_running
    if _pat_thread and _pat_thread.is_alive():
        return
    _pat_running = True
    _pat_thread = threading.Thread(target=_pat_poll_loop, daemon=True)
    _pat_thread.start()
    log.info("Pat/Winlink poller thread started")


def start_aprs():
    global _aprs_thread, _aprs_running
    if _aprs_thread and _aprs_thread.is_alive():
        return
    _aprs_running = True
    _aprs_thread = threading.Thread(target=_aprs_listener_loop, daemon=True)
    _aprs_thread.start()
    log.info("APRS listener thread started")
    threading.Thread(target=_aprs_fi_backfill, daemon=True).start()


# ---------------------------------------------------------------------------
# APRS Position Beacon
# ---------------------------------------------------------------------------
_beacon_thread = None
_beacon_running = False

def _lat_to_aprs(lat):
    """Convert decimal latitude to APRS format (DDMM.MMN/S)."""
    ns = "N" if lat >= 0 else "S"
    lat = abs(lat)
    deg = int(lat)
    mins = (lat - deg) * 60
    return f"{deg:02d}{mins:05.2f}{ns}"

def _lon_to_aprs(lon):
    """Convert decimal longitude to APRS format (DDDMM.MME/W)."""
    ew = "E" if lon >= 0 else "W"
    lon = abs(lon)
    deg = int(lon)
    mins = (lon - deg) * 60
    return f"{deg:03d}{mins:05.2f}{ew}"

def _build_beacon_packet():
    """Build an APRS position beacon packet string."""
    with cfglock:
        bcn = config.get("beacon", {})
        base = config.get("home_callsign", "").strip().upper()
        ssid = config.get("aprs", {}).get("home_ssid", "-5")
    if not base or not bcn.get("enabled"):
        return None, None
    lat = bcn.get("lat", 0)
    lon = bcn.get("lon", 0)
    if lat == 0 and lon == 0:
        return None, None
    home = base + ssid if ssid and not base.endswith(ssid) else base
    sym_table = bcn.get("symbol_table", "/")
    sym_code = bcn.get("symbol_code", "-")
    comment = bcn.get("comment", "HamLink Radio")
    lat_str = _lat_to_aprs(lat)
    lon_str = _lon_to_aprs(lon)
    # APRS position format: !DDMM.MMN/DDDMM.MME-comment
    position = f"!{lat_str}{sym_table}{lon_str}{sym_code}{comment}"
    # Packet for APRS-IS
    aprsis_pkt = f"{home}>APRS,TCPIP*:{position}"
    # Packet for RF (with digipeater path)
    rf_pkt = f"{home}>APRS,WIDE1-1,WIDE2-1:{position}"
    return aprsis_pkt, rf_pkt

def _beacon_loop():
    """Background thread: send position beacons at configured interval."""
    global _beacon_running
    # Initial delay to let connections establish
    time.sleep(30)
    while _beacon_running:
        with cfglock:
            bcn = config.get("beacon", {})
            enabled = bcn.get("enabled", False)
            interval = max(bcn.get("interval_minutes", 30), 5) * 60  # min 5 minutes
            via_aprsis = bcn.get("via_aprsis", True)
            via_rf = bcn.get("via_rf", True)
        if not enabled:
            time.sleep(10)
            continue
        aprsis_pkt, rf_pkt = _build_beacon_packet()
        if not aprsis_pkt:
            time.sleep(10)
            continue
        # Send via APRS-IS
        if via_aprsis:
            try:
                ok = _aprs_send_raw(aprsis_pkt)
                if ok:
                    log.info("Beacon sent via APRS-IS")
                else:
                    log.warning("Beacon APRS-IS send failed (not connected?)")
            except Exception as e:
                log.warning("Beacon APRS-IS error: %s", e)
        # Send via RF (Soundmodem KISS)
        if via_rf:
            try:
                ax25 = _aprs_string_to_ax25(rf_pkt)
                if ax25:
                    ok = _kiss_send_frame(ax25)
                    if ok:
                        log.info("Beacon sent via RF (Soundmodem)")
                    else:
                        log.warning("Beacon RF send failed (Soundmodem not connected?)")
                else:
                    log.warning("Beacon RF: failed to encode AX.25")
            except Exception as e:
                log.warning("Beacon RF error: %s", e)
        # Wait for next beacon
        for _ in range(interval):
            if not _beacon_running:
                return
            time.sleep(1)

def start_beacon():
    global _beacon_thread, _beacon_running
    if _beacon_thread and _beacon_thread.is_alive():
        return
    _beacon_running = True
    _beacon_thread = threading.Thread(target=_beacon_loop, daemon=True)
    _beacon_thread.start()
    log.info("APRS beacon thread started")


# ---------------------------------------------------------------------------
# Relay retrieval automation
# ---------------------------------------------------------------------------
_relay_thread = None
_relay_running = False

def _relay_retrieval_loop():
    """Background thread: process the relay retrieval queue, connecting to relay
    stations via VarAC UI automation to download pending VMails."""
    global _relay_running
    log.info("Relay retrieval loop started")
    while _relay_running:
        try:
            with cfglock:
                rcfg = config.get("relay", {})
            if not rcfg.get("enabled"):
                for _ in range(50):  # 5s sleep in 0.1s increments
                    if not _relay_running:
                        return
                    time.sleep(0.1)
                continue

            delay_secs = rcfg.get("auto_retrieve_delay_seconds", 10)
            max_retries = rcfg.get("max_retries", 2)
            retry_delay = rcfg.get("retry_delay_seconds", 120)
            cooldown = rcfg.get("cooldown_seconds", 300)

            # Check for pending items in the queue
            with slock:
                queue = list(state["relay_retrieval_queue"])
            if not queue:
                for _ in range(50):
                    if not _relay_running:
                        return
                    time.sleep(0.1)
                continue

            task = queue[0]
            relay_call = task["relay_station"]
            relay_upper = relay_call.upper().strip()

            # Respect the delay before first attempt
            queued_at = task.get("queued_at", "")
            if queued_at:
                try:
                    q_dt = datetime.fromisoformat(queued_at.replace("Z", "+00:00"))
                    elapsed = (datetime.now(timezone.utc) - q_dt).total_seconds()
                    if elapsed < delay_secs:
                        wait = delay_secs - elapsed
                        log.debug("Relay retrieve: waiting %.0fs before connecting to %s", wait, relay_call)
                        for _ in range(int(wait * 10)):
                            if not _relay_running:
                                return
                            time.sleep(0.1)
                        continue
                except Exception:
                    pass

            # Check cooldown
            with slock:
                last_attempt_iso = state["relay_last_attempt"].get(relay_upper)
            if last_attempt_iso:
                try:
                    la_dt = datetime.fromisoformat(last_attempt_iso.replace("Z", "+00:00"))
                    elapsed = (datetime.now(timezone.utc) - la_dt).total_seconds()
                    if elapsed < cooldown:
                        log.info("Relay %s still in cooldown (%ds remaining), skipping",
                                 relay_call, int(cooldown - elapsed))
                        with slock:
                            if state["relay_retrieval_queue"] and state["relay_retrieval_queue"][0] is task:
                                state["relay_retrieval_queue"].pop(0)
                        continue
                except Exception:
                    pass

            # Attempt retrieval
            with slock:
                state["relay_retrieval_active"] = True
                if relay_upper in state["relay_tracking"]:
                    state["relay_tracking"][relay_upper]["status"] = "retrieving"

            log.info("Relay retrieve: connecting to %s", relay_call)
            now_iso = datetime.now(timezone.utc).isoformat()
            with slock:
                state["relay_last_attempt"][relay_upper] = now_iso

            ok, err = varac_retrieve_relay(relay_call)

            with slock:
                state["relay_retrieval_active"] = False
                entry = state["relay_tracking"].get(relay_upper, {})

                if ok:
                    entry["status"] = "retrieved"
                    entry["last_attempt"] = now_iso
                    entry["error"] = None
                    log.info("Relay retrieve: success for %s", relay_call)
                else:
                    entry["attempts"] = entry.get("attempts", 0) + 1
                    entry["last_attempt"] = now_iso
                    entry["error"] = err
                    if entry["attempts"] >= max_retries:
                        entry["status"] = "failed"
                        log.warning("Relay retrieve: FAILED for %s after %d attempts: %s",
                                    relay_call, entry["attempts"], err)
                    else:
                        entry["status"] = "queued"
                        log.warning("Relay retrieve: attempt %d failed for %s: %s — will retry in %ds",
                                    entry["attempts"], relay_call, err, retry_delay)

                # Remove from queue
                if state["relay_retrieval_queue"] and state["relay_retrieval_queue"][0] is task:
                    state["relay_retrieval_queue"].pop(0)

                # Re-queue for retry if needed
                if not ok and entry.get("status") == "queued":
                    retry_at = datetime.now(timezone.utc) + timedelta(seconds=retry_delay)
                    state["relay_retrieval_queue"].append({
                        "relay_station": relay_call,
                        "frequency_mhz": task.get("frequency_mhz", ""),
                        "queued_at": retry_at.isoformat(),
                    })

        except Exception as e:
            log.error("Relay retrieval loop error: %s", e)
            with slock:
                state["relay_retrieval_active"] = False

        # Sleep 5 seconds between checks
        for _ in range(50):
            if not _relay_running:
                return
            time.sleep(0.1)

    log.info("Relay retrieval loop stopped")


def start_relay():
    global _relay_thread, _relay_running
    if _relay_thread and _relay_thread.is_alive():
        return
    _relay_running = True
    _relay_thread = threading.Thread(target=_relay_retrieval_loop, daemon=True)
    _relay_thread.start()
    log.info("Relay retrieval thread started")


# ---------------------------------------------------------------------------
# Pushover
# ---------------------------------------------------------------------------
def send_pushover(title, message, reply_channel="varac", relay_station="", relay_status=""):
    with cfglock:
        po = config.get("pushover", {})
        quick_replies = config.get("quick_replies", [])
        web_port = config.get("web_port", 5000)
    if not po.get("enabled") or not po.get("user_key") or not po.get("api_token"):
        return
    if not _check_internet(timeout=2):
        log.info("Pushover skipped (no internet)")
        return
    try:
        # Build HTML message with quick reply links
        # These link to the monitor's API on the local network
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
        except OSError:
            local_ip = "127.0.0.1"
        base_url = f"http://{local_ip}:{web_port}"
        html_body = message.replace("\n", "<br>")

        # Relay approval link for confirmed_wait status
        if relay_station and relay_status == "confirmed_wait":
            encoded_station = urllib.parse.quote(relay_station)
            approve_link = f"{base_url}/api/relay/approve_from_phone?station={encoded_station}"
            html_body += f'<br><br>→ <a href="{approve_link}"><b>Approve Auto-Retrieval</b></a>'

        po_quick = po.get("quick_replies", False)
        if po_quick and quick_replies:
            html_body += "<br><br><b>Quick Replies:</b><br>"
            for qr in quick_replies[:4]:  # Max 4 quick replies
                qr_short = qr[:67] if reply_channel == "aprs" else qr
                encoded = urllib.parse.quote(qr_short)
                link = f"{base_url}/api/pushover_reply?channel={reply_channel}&message={encoded}"
                html_body += f'→ <a href="{link}">{qr_short}</a><br>'

        # Always include a dismiss link so the user can silence HamLink from their phone
        html_body += f'<br><a href="{base_url}/api/dismiss_from_phone">Dismiss Alert</a>'
        html_body += '<br><small><i>Links require your phone to be on the same network as HamLink.</i></small>'

        p = {"token": po["api_token"], "user": po["user_key"],
             "title": title, "message": html_body, "html": "1",
             "priority": po.get("priority", 1), "sound": po.get("sound", "pushover"),
             "url": f"{base_url}", "url_title": "Open Dashboard"}
        if p["priority"] == 2:
            p["retry"] = po.get("retry", 60)
            p["expire"] = po.get("expire", 3600)
        d = urllib.parse.urlencode(p).encode()
        r = urllib.request.Request("https://api.pushover.net/1/messages.json", data=d)
        with urllib.request.urlopen(r, timeout=10):
            pass
        log.info("Pushover sent: %s", title)
    except Exception as e:
        log.warning("Pushover failed: %s", e)

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def _connect():
    with cfglock:
        p = config.get("varac_db_path", "")
    if not p:
        raise FileNotFoundError("No database path configured.")
    if not os.path.isfile(p):
        raise FileNotFoundError(f"Database not found: {p}")
    c = sqlite3.connect(p, timeout=15)
    c.row_factory = sqlite3.Row
    return c

def _match(cs):
    with cfglock:
        w = [c.upper().strip() for c in config.get("watch_callsigns", []) if c.strip()]
    return (not w) or cs.upper().strip() in w

def _opname():
    with cfglock:
        return config.get("operator_name", "") or ""

def _friendly_time(iso_str):
    """Return a human-friendly time string."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        diff = (now - dt).total_seconds()
        if diff < 60:
            return "Just now"
        elif diff < 3600:
            m = int(diff // 60)
            return f"{m} min ago"
        elif diff < 86400:
            h = int(diff // 3600)
            return f"{h} hr ago"
        else:
            return dt.strftime("%b %d, %I:%M %p")
    except Exception:
        return str(iso_str)

# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------
def init_hwm():
    try:
        c = _connect()
        cur = c.cursor()
        # Set HWM to max of non-inbox vmails only, so unread inbox vmails
        # are detected on startup (not skipped)
        cur.execute("SELECT folder_id FROM vmail_folder WHERE folder='Inbox'")
        inbox_row = cur.fetchone()
        inbox_id = inbox_row[0] if inbox_row else 1
        # Find the highest id of non-inbox vmails (sent, outbox, parking, drafts)
        cur.execute("SELECT COALESCE(MAX(id),0) FROM vmail WHERE folder_id != ?", (inbox_id,))
        non_inbox_max = cur.fetchone()[0]
        # Find the highest id of READ inbox vmails (already seen)
        cur.execute("SELECT COALESCE(MAX(id),0) FROM vmail WHERE folder_id = ? AND read_status = 1", (inbox_id,))
        read_inbox_max = cur.fetchone()[0]
        # HWM = highest of non-inbox or read-inbox, so unread inbox items are picked up
        state["vmail_hwm"] = max(non_inbox_max, read_inbox_max)
        cur.execute("SELECT COALESCE(MAX(id),0) FROM vmail_relay_notification WHERE is_deleted = 1")
        state["relay_hwm"] = cur.fetchone()[0]
        log.info("HWM initialized: vmail=%d, relay=%d", state["vmail_hwm"], state["relay_hwm"])
        c.close()
        return True
    except Exception as e:
        log.warning("HWM init failed: %s", e)
        return False

def _process_relay_notification(fr, freq_mhz, urgent, t, opname):
    """Process a relay notification with relay-specific logic (tracking, queueing, cooldown).
    Returns a dict to add to the relay_tracking entry (or None to skip)."""
    with cfglock:
        rcfg = config.get("relay", {})
    enabled = rcfg.get("enabled", False)
    auto_retrieve = rcfg.get("auto_retrieve", False) and enabled
    confirm = rcfg.get("confirm_before_connect", True)
    cooldown = rcfg.get("cooldown_seconds", 300)
    ignore_list = [s.upper().strip() for s in rcfg.get("ignore_stations", [])]

    fr_upper = fr.upper().strip()
    now_utc = datetime.now(timezone.utc).isoformat()

    # Check ignore list
    if fr_upper in ignore_list:
        log.info("Relay from %s ignored (in ignore_stations list)", fr)
        return None

    with slock:
        tracking = state["relay_tracking"]
        last_attempts = state["relay_last_attempt"]

        # Check cooldown
        last_attempt_time = last_attempts.get(fr_upper)
        if last_attempt_time:
            try:
                la_dt = datetime.fromisoformat(last_attempt_time.replace("Z", "+00:00"))
                elapsed = (datetime.now(timezone.utc) - la_dt).total_seconds()
                if elapsed < cooldown:
                    log.info("Relay from %s skipped (cooldown: %ds remaining)", fr, int(cooldown - elapsed))
                    # Still update tracking entry if it exists
                    if fr_upper in tracking:
                        tracking[fr_upper]["last_seen"] = now_utc
                        tracking[fr_upper]["notification_count"] = tracking[fr_upper].get("notification_count", 0) + 1
                    return None
            except Exception:
                pass

        # Upsert relay tracking entry
        if fr_upper in tracking:
            entry = tracking[fr_upper]
            entry["last_seen"] = now_utc
            entry["notification_count"] = entry.get("notification_count", 0) + 1
            entry["frequency_mhz"] = freq_mhz
            entry["urgent"] = urgent
            # Don't overwrite status if already queued/retrieving
            if entry.get("status") in ("retrieved", "failed"):
                entry["status"] = "pending"  # Re-arm on new notification
                entry["attempts"] = 0
                entry["error"] = None
        else:
            entry = {
                "relay_station": fr,
                "frequency_mhz": freq_mhz,
                "first_seen": now_utc,
                "last_seen": now_utc,
                "notification_count": 1,
                "urgent": urgent,
                "status": "pending",
                "last_attempt": None,
                "attempts": 0,
                "error": None,
                "retrieved_vmail_ids": [],
            }
            tracking[fr_upper] = entry

        # Queue for retrieval if auto_retrieve is enabled
        if auto_retrieve and entry["status"] == "pending":
            if confirm:
                entry["status"] = "confirmed_wait"
                state["relay_pending_confirm"] = fr_upper
                log.info("Relay from %s awaiting user confirmation", fr)
            else:
                entry["status"] = "queued"
                if fr_upper not in [q.get("relay_station", "").upper() for q in state["relay_retrieval_queue"]]:
                    state["relay_retrieval_queue"].append({
                        "relay_station": fr,
                        "frequency_mhz": freq_mhz,
                        "queued_at": now_utc,
                    })
                log.info("Relay from %s queued for auto-retrieval", fr)

    return entry


def poll_once():
    if DEMO_MODE:
        return  # demo data is seeded once at startup; nothing to poll
    with cfglock:
        dbp = config.get("varac_db_path", "")
    if not dbp:
        with slock:
            state["db_connected"] = False
            state["error"] = "No database configured. Open Settings to set it up."
        return

    try:
        conn = _connect()
        cur = conn.cursor()
        alerts = []
        opname = _opname()

        # Determine inbox folder_id
        cur.execute("SELECT folder_id FROM vmail_folder WHERE folder='Inbox'")
        inbox_row = cur.fetchone()
        inbox_id = inbox_row["folder_id"] if inbox_row else 1  # fallback to 1

        # VMails (inbox only — skip Outbox/Sent/Parking to avoid self-alerts)
        cur.execute("""SELECT id, guid, creation_time, received_time,
                              vmail_from, vmail_to, vmail_via, subject, msg,
                              folder_id, is_deleted, has_attachment, urgent,
                              delivery_band, delivery_snr
                       FROM vmail WHERE id > ? AND is_deleted = 0
                       AND folder_id = ? ORDER BY id ASC""",
                    (state["vmail_hwm"], inbox_id))
        inbox_rows = cur.fetchall()

        # Also check for non-inbox vmails to advance HWM past them
        # (so we don't re-scan sent/outbox items every cycle)
        cur.execute("SELECT COALESCE(MAX(id),?) FROM vmail WHERE id > ? AND folder_id != ?",
                    (state["vmail_hwm"], state["vmail_hwm"], inbox_id))
        non_inbox_max = cur.fetchone()[0]

        # Advance HWM to the higher of: last inbox row processed, or last non-inbox row seen
        new_hwm = state["vmail_hwm"]
        if inbox_rows:
            new_hwm = max(new_hwm, max(row["id"] for row in inbox_rows))
        if non_inbox_max:
            new_hwm = max(new_hwm, non_inbox_max)
        state["vmail_hwm"] = new_hwm

        # Get home callsign to filter out our own outgoing messages
        with cfglock:
            home_call_upper = config.get("home_callsign", "").strip().upper()

        for row in inbox_rows:
            fr = row["vmail_from"] or ""
            to = row["vmail_to"] or ""
            # Skip messages FROM our exact home callsign ONLY if they're addressed
            # to someone else (leaked outgoing copies VarAC may place in inbox).
            # If vmail_to == home_callsign, always alert — someone sent us a message
            # (even if vmail_from also matches, e.g. portable station using base call).
            fr_upper = fr.upper().strip()
            to_upper = to.upper().strip()
            is_own = (fr_upper == home_call_upper) and (to_upper != home_call_upper)
            if is_own:
                log.debug("Skipping own outgoing vmail from %s to %s", fr, to)
                continue
            if _match(fr):
                t = row["received_time"] or row["creation_time"] or datetime.now(timezone.utc).isoformat()
                name = opname or fr
                alerts.append({
                    "id": f"vmail-{row['id']}", "type": "vmail",
                    "time": t, "from_call": fr, "from_name": name,
                    "to": row["vmail_to"] or "",
                    "subject": row["subject"] or "",
                    "message": row["msg"] or "",
                    "urgent": bool(row["urgent"]),
                    "has_attachment": bool(row["has_attachment"]),
                    "via": row["vmail_via"] or "",
                    "band": row["delivery_band"] or "",
                    "snr": row["delivery_snr"] or "",
                    "friendly_time": _friendly_time(t),
                })

        # Track relay paths from incoming VMails (for reply routing)
        for row in inbox_rows:
            via = row["vmail_via"] or ""
            fr = row["vmail_from"] or ""
            if via.strip() and fr.strip():
                with slock:
                    state["relay_paths"][fr.upper().strip()] = via.strip()
                    log.debug("Relay path recorded: %s via %s", fr, via)

        # Relay notifications
        cur.execute("""SELECT id, guid, relay_notification_time, frequency,
                              from_callsign, is_deleted, urgent
                       FROM vmail_relay_notification
                       WHERE id > ? AND is_deleted = 0 ORDER BY id ASC""",
                    (state["relay_hwm"],))
        for row in cur.fetchall():
            state["relay_hwm"] = max(state["relay_hwm"], row["id"])
            fr = row["from_callsign"] or ""
            # Skip relay notifications from our own home station
            if home_call_upper and fr.upper().startswith(home_call_upper.split("-")[0]):
                log.debug("Skipping own relay notification from %s", fr)
                continue
            t = row["relay_notification_time"] or datetime.now(timezone.utc).isoformat()
            freq_mhz = (row["frequency"] or 0) / 1_000_000
            freq_str = f"{freq_mhz:.4f}" if freq_mhz else ""
            urg = bool(row["urgent"])

            # Process relay notification (tracking, queueing, cooldown)
            _process_relay_notification(fr, freq_str, urg, t, opname)

            # Always create the alert for UI display (regardless of relay automation)
            alerts.append({
                "id": f"relay-{row['id']}", "type": "relay",
                "time": t, "from_call": fr,
                "from_name": opname or fr,
                "relay_station": fr,
                "frequency_mhz": freq_str,
                "urgent": urg,
                "friendly_time": _friendly_time(t),
            })

        conn.close()

        if alerts:
            with slock:
                for a in alerts:
                    state["pending_alerts"].append(a)
                    state["history"].append(a)
                    if a["type"] == "vmail":
                        state["last_checkin_time"] = a["time"]
                        state["last_checkin_from"] = a["from_name"]

            # These run OUTSIDE the lock to avoid blocking state access
            for a in alerts:
                log.info("ALERT: [%s] from %s", a["type"], a["from_call"])
                log_message(a, direction="incoming")

            # Start speaker alarm once (not per-alert)
            start_speaker_alarm()

            # Send Pushover notifications (network I/O — must be outside lock)
            for a in alerts:
                name = a["from_name"]
                if a["type"] == "vmail":
                    subj = a["subject"]
                    msg = a["message"][:200] if a["message"] else ""
                    body = f"{subj}\n{msg}" if subj else msg
                    send_pushover(f"Message from {name}", body or "New message received")
                elif a["type"] == "relay":
                    relay_info = f"Station {a.get('relay_station', 'unknown')} is holding a message for you"
                    if a.get("frequency_mhz"):
                        relay_info += f" ({a['frequency_mhz']} MHz)"
                    # Include relay automation status in notification
                    with slock:
                        rtrack = state["relay_tracking"].get(a.get("relay_station", "").upper(), {})
                    rst = rtrack.get("status", "")
                    if rst == "queued":
                        relay_info += ". Auto-retrieval queued."
                    elif rst == "confirmed_wait":
                        relay_info += ". Awaiting your approval to auto-retrieve."
                    elif rst == "retrieving":
                        relay_info += ". Auto-retrieval in progress."
                    else:
                        relay_info += ". Open VarAC relay dialog to retrieve, or enable auto-retrieve in HamLink settings."
                    send_pushover(f"Relay alert from {name}", relay_info,
                                  relay_station=a.get("relay_station", ""),
                                  relay_status=rst)

        with slock:
            state["last_poll"] = datetime.now(timezone.utc).isoformat()
            state["db_connected"] = True
            state["error"] = None

    except Exception as e:
        log.error("Poll error: %s", e)
        with slock:
            state["db_connected"] = False
            state["error"] = str(e)

def poll_loop():
    _trim_counter = 0
    _hwm_initialized = False
    log.info("Poll loop started")
    while True:
        with cfglock:
            iv = config.get("poll_interval_seconds", 15)
            p = config.get("varac_db_path", "")
        # Initialize HWM once when DB becomes available
        if p and not DEMO_MODE and os.path.isfile(p) and not _hwm_initialized:
            if init_hwm():
                _hwm_initialized = True
                log.info("VarAC DB polling active")
        # Poll DB if available (skip gracefully if not)
        poll_once()
        # Periodic cleanup every ~60 polls (~15 minutes at default interval)
        _trim_counter += 1
        if _trim_counter >= 60:
            _trim_counter = 0
            _trim_memory()
        # Check internet status every poll cycle
        inet = _check_internet(timeout=2)
        with slock:
            state["internet_up"] = inet
        time.sleep(iv)

MAX_HISTORY = 500
MAX_ACKED = 1000
MAX_APRS_SEEN = 500

def _trim_memory():
    """Prevent unbounded memory growth in long-running sessions."""
    global _aprs_seen_msgs
    with slock:
        if len(state["history"]) > MAX_HISTORY:
            state["history"] = state["history"][-MAX_HISTORY:]
        if len(state["acknowledged_ids"]) > MAX_ACKED:
            # Keep only the most recent ones (convert to list, trim, back to set)
            acked = list(state["acknowledged_ids"])
            state["acknowledged_ids"] = set(acked[-MAX_ACKED:])
    if len(_aprs_seen_msgs) > MAX_APRS_SEEN:
        # Keep a random subset (order doesn't matter for dedup)
        trimmed = list(_aprs_seen_msgs)[-MAX_APRS_SEEN:]
        _aprs_seen_msgs = set(trimmed)

# ---------------------------------------------------------------------------
# Updates — check GitHub releases, download + install, restart
# ---------------------------------------------------------------------------
# Three install kinds are recognised:
#   exe    — running as the PyInstaller build. The new HamLink_Radio.exe is
#            downloaded from the release's -win64.zip; the running exe is
#            renamed to *.old.exe (Windows allows renaming a running binary)
#            and the new one moved into place. The .old.exe is removed on the
#            next start.
#   source — running monitor.py from an unpacked source zip. Every file in the
#            release's source archive is written over APP_DIR (monitor.py is
#            backed up to monitor.py.bak first). Batch files are written as
#            *.bat.new and swapped in by the restart helper, because cmd.exe
#            reads a running .bat incrementally and must not see it change.
#   git    — APP_DIR is a git checkout. Self-update is refused; use git pull.
_update_thread = None
_update_worker = None
_update_lock = threading.Lock()

def _parse_version(v):
    """'v1.2.3-beta' -> (1, 2, 3). Unknown strings compare as (0, 0, 0)."""
    m = re.match(r"\s*[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?", v or "")
    return tuple(int(x or 0) for x in m.groups()) if m else (0, 0, 0)

def _running_version():
    # HAMLINK_VERSION_OVERRIDE lets a developer pretend to be an older build
    # to exercise the update path without editing the code.
    return os.environ.get("HAMLINK_VERSION_OVERRIDE", "").strip() or __version__

def _install_kind():
    if getattr(sys, "frozen", False):
        return "exe"
    if os.path.isdir(os.path.join(APP_DIR, ".git")):
        return "git"
    return "source"

def _set_update(**kv):
    with slock:
        state["update"].update(kv)

def _github_token():
    with cfglock:
        return (config.get("updates", {}).get("github_token", "") or "").strip()

def _github_headers(accept):
    """Request headers for api.github.com. A token is only needed while the
    repository is private (or to lift the anonymous rate limit)."""
    h = {"User-Agent": f"HamLink-Radio/{__version__}", "Accept": accept}
    tok = _github_token()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h

def check_for_update(force=False):
    """Query the GitHub Releases API and record the result in state['update'].
    Returns the new update dict. Network errors are recorded, never raised."""
    with cfglock:
        auto = bool(config.get("updates", {}).get("auto_check", True))
    if not force and not auto:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    if DEMO_MODE:
        return
    if not _check_internet(timeout=3):
        _set_update(error="No internet connection", checked_at=now_iso)
        return
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
            headers=_github_headers("application/vnd.github+json"))
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        msg = f"Update check failed: HTTP {e.code}"
        if e.code == 404:
            msg += " — the repository or its releases are not visible (private repo? add a GitHub token in Settings → Updates)"
        elif e.code in (401, 403):
            msg += " — GitHub token rejected or rate limit hit"
        log.warning(msg)
        _set_update(error=msg, checked_at=now_iso)
        return
    except Exception as e:
        log.warning("Update check failed: %s", e)
        _set_update(error=f"Update check failed: {e}", checked_at=now_iso)
        return
    tag = (data.get("tag_name") or "").strip()
    kind = _install_kind()
    # Each asset: browser_download_url works for public repos; the API `url`
    # (with Accept: application/octet-stream + token) works for private ones.
    assets = {a.get("name", ""): a for a in data.get("assets", [])}
    asset_name = asset_url = asset_api_url = None
    if kind == "exe":
        for name, a in assets.items():
            if name.lower().endswith("-win64.zip"):
                asset_name, asset_url, asset_api_url = name, a.get("browser_download_url"), a.get("url")
                break
    else:
        asset_name = f"HamLink-{tag}.zip"
        a = assets.get(asset_name)
        if a:
            asset_url, asset_api_url = a.get("browser_download_url"), a.get("url")
        else:
            asset_url = asset_api_url = data.get("zipball_url")
    available = _parse_version(tag) > _parse_version(_running_version())
    _set_update(available=available, current=_running_version(), latest=tag,
                url=data.get("html_url"), notes=(data.get("body") or "")[:6000],
                published=data.get("published_at"), asset_url=asset_url,
                asset_api_url=asset_api_url, asset_name=asset_name,
                checked_at=now_iso, error=None, install_kind=kind)
    if available:
        log.info("Update available: %s (running %s) — %s", tag, _running_version(), data.get("html_url"))
    else:
        log.info("Update check: %s is current (latest release %s)", _running_version(), tag or "none")
    with slock:
        return dict(state["update"])

def _update_check_loop():
    """Background thread: check shortly after start, then every interval_hours."""
    time.sleep(20)
    while True:
        try:
            check_for_update()
        except Exception as e:
            log.warning("Update check loop error: %s", e)
        with cfglock:
            hours = config.get("updates", {}).get("interval_hours", 6)
        try:
            hours = max(1, float(hours))
        except (TypeError, ValueError):
            hours = 6
        deadline = time.time() + hours * 3600
        while time.time() < deadline:
            time.sleep(60)

def start_update_checker():
    global _update_thread
    if _update_thread and _update_thread.is_alive():
        return
    _update_thread = threading.Thread(target=_update_check_loop, daemon=True)
    _update_thread.start()

def _safe_zip_members(z):
    """Yield (member, relative_path) for regular files in a release archive,
    stripping the top-level folder and refusing anything that escapes it."""
    for m in z.infolist():
        if m.is_dir():
            continue
        parts = m.filename.replace("\\", "/").split("/")
        rel_parts = parts[1:] if len(parts) > 1 else []
        if not rel_parts or any(p in ("", ".", "..") for p in rel_parts):
            continue
        if rel_parts[0] in (".git", ".github", ".gitignore", ".claude"):
            continue
        yield m, "/".join(rel_parts)

def _download(info, dest, label):
    """Fetch the release asset. With a token, use the API asset URL (works for
    private repos); otherwise the public browser_download_url."""
    if _github_token() and info.get("asset_api_url"):
        req = urllib.request.Request(info["asset_api_url"], headers=_github_headers("application/octet-stream"))
    else:
        req = urllib.request.Request(info["asset_url"], headers={"User-Agent": f"HamLink-Radio/{__version__}"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                _set_update(progress=f"Downloading {label}… {done * 100 // total}%")
            else:
                _set_update(progress=f"Downloading {label}… {done // 1024} KB")

def _apply_update_worker():
    with slock:
        info = dict(state["update"])
    kind = info.get("install_kind") or _install_kind()
    try:
        if DEMO_MODE:
            for pct in (0, 25, 50, 75, 100):
                _set_update(stage="downloading", progress=f"Downloading (demo)… {pct}%")
                time.sleep(0.6)
            _set_update(stage="installing", progress="Installing (demo)…")
            time.sleep(1)
            _set_update(stage="done", progress="Demo mode: nothing was installed.")
            return
        if kind == "git":
            raise RuntimeError("This copy is a git checkout — run `git pull` instead.")
        if not (info.get("asset_url") or info.get("asset_api_url")):
            raise RuntimeError("The release has no downloadable asset for this install type.")
        tmpdir = tempfile.mkdtemp(prefix="hamlink-update-")
        zpath = os.path.join(tmpdir, "update.zip")
        _set_update(stage="downloading", progress=f"Downloading {info.get('asset_name')}…")
        _download(info, zpath, info.get("latest") or "update")
        _set_update(stage="installing", progress="Installing…")
        new_exe = None
        with zipfile.ZipFile(zpath) as z:
            for m, rel in _safe_zip_members(z):
                if kind == "exe":
                    if rel.lower().endswith(".exe"):
                        new_exe = os.path.join(tmpdir, os.path.basename(rel))
                        with z.open(m) as src, open(new_exe, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        continue
                    # The win64 zip carries README/MANUAL/tiles README next to the exe
                    if rel.startswith("tiles/") and rel != "tiles/README.txt":
                        continue
                dest = os.path.join(APP_DIR, *rel.split("/"))
                if rel.lower().endswith(".bat"):
                    dest += ".new"          # swapped in by the restart helper
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                if rel == "monitor.py" and os.path.isfile(dest):
                    shutil.copy2(dest, dest + ".bak")
                with z.open(m) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        if kind == "exe":
            if not new_exe:
                raise RuntimeError("No .exe found inside the downloaded archive.")
            exe = os.path.abspath(sys.executable)
            old = os.path.splitext(exe)[0] + ".old.exe"
            if os.path.exists(old):
                os.remove(old)
            os.replace(exe, old)        # renaming a running exe is allowed on Windows
            shutil.move(new_exe, exe)
        shutil.rmtree(tmpdir, ignore_errors=True)
        log.info("Update to %s installed (%s) — restarting", info.get("latest"), kind)
        _set_update(stage="restarting", progress="Installed. Restarting HamLink…", available=False,
                    current=info.get("latest") or _running_version())
        time.sleep(1.5)
        _restart_app()
    except Exception as e:
        log.error("Update failed: %s", e)
        _set_update(stage="error", progress=f"Update failed: {e}")

def apply_update():
    """Start the download/install worker. Returns (ok, message)."""
    global _update_worker
    with _update_lock:
        if _update_worker and _update_worker.is_alive():
            return False, "An update is already in progress"
        with slock:
            info = dict(state["update"])
        if not info.get("available") and not DEMO_MODE:
            return False, "No update available"
        _set_update(stage="downloading", progress="Starting…")
        _update_worker = threading.Thread(target=_apply_update_worker, daemon=True)
        _update_worker.start()
        return True, ""

def _restart_app():
    """Relaunch HamLink after an update. On Windows a tiny helper batch waits
    for this process to exit, swaps in any *.bat.new files, then starts the
    launcher (source) or the exe. Exit code 0 lets an old launcher window
    close quietly."""
    if sys.platform == "win32":
        helper_dir = tempfile.mkdtemp(prefix="hamlink-restart-")
        helper = os.path.join(helper_dir, "restart.bat")
        if getattr(sys, "frozen", False):
            launch = f'start "" "{os.path.abspath(sys.executable)}" --no-browser'
        elif os.path.isfile(os.path.join(APP_DIR, "start_hamlink.bat")) or \
                os.path.isfile(os.path.join(APP_DIR, "start_hamlink.bat.new")):
            launch = 'start "HamLink Radio" "start_hamlink.bat" --no-browser'
        else:
            launch = f'start "HamLink Radio" "{sys.executable}" "{os.path.abspath(sys.argv[0])}"'
        with open(helper, "w", encoding="ascii", errors="replace", newline="") as f:
            f.write("@echo off\r\n"
                    "timeout /t 3 /nobreak >nul\r\n"
                    f'cd /d "{APP_DIR}"\r\n'
                    'for %%f in (*.bat.new) do move /y "%%f" "%%~nf" >nul\r\n'
                    f"{launch}\r\n")
        # Strip PyInstaller's bootloader variables: a child that inherits
        # _MEIPASS2/_PYI_* would reuse this process's extraction folder, which
        # is deleted the moment we exit, so the relaunched exe would die.
        env = {k: v for k, v in os.environ.items() if not k.startswith(("_MEI", "_PYI"))}
        subprocess.Popen(["cmd.exe", "/c", helper], cwd=APP_DIR, close_fds=True, env=env,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        _cleanup()
        os._exit(0)
    else:
        _cleanup()
        os.execv(sys.executable, [sys.executable] + sys.argv)

def _remove_old_exe():
    """Delete the *.old.exe left behind by a previous self-update (best effort)."""
    if not getattr(sys, "frozen", False):
        return
    old = os.path.splitext(os.path.abspath(sys.executable))[0] + ".old.exe"
    if os.path.exists(old):
        try:
            os.remove(old)
            log.info("Removed previous version %s", os.path.basename(old))
        except OSError:
            pass

# ---------------------------------------------------------------------------
# Offline map tiles — built-in downloader (USGS The National Map)
# ---------------------------------------------------------------------------
# Tiles come from the USGS National Map basemaps, which are US-government
# public domain and permit bulk download for offline use. They are stored in
# a standard MBTiles file under tiles/ and served by /tiles/{z}/{x}/{y}.png.
MAP_SOURCES = {
    "usgs_topo": {
        "label": "USGS Topo (roads, terrain, place names)",
        "url": "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}",
        "attribution": "USGS The National Map", "max_zoom": 16,
    },
    "usgs_imagery": {
        "label": "USGS Imagery + Topo (satellite with labels)",
        "url": "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryTopo/MapServer/tile/{z}/{y}/{x}",
        "attribution": "USGS The National Map", "max_zoom": 16,
    },
}
TILES_DIR = os.path.join(APP_DIR, "tiles")
_map_dl = {"running": False, "name": "", "done": 0, "total": 0, "failed": 0,
           "error": None, "file": None, "cancel": False, "started": None, "finished": None}
_map_dl_lock = threading.Lock()

def _tile_xy(lat, lon, z):
    import math
    lat = max(-85.0511, min(85.0511, lat))
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))

def _map_bbox(lat, lon, radius_km):
    import math
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.1, math.cos(math.radians(lat))))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon   # south, west, north, east

def _map_tile_list(lat, lon, radius_km, zmin, zmax):
    s, w, n, e = _map_bbox(lat, lon, radius_km)
    tiles = []
    for z in range(zmin, zmax + 1):
        x0, y0 = _tile_xy(n, w, z)   # top-left
        x1, y1 = _tile_xy(s, e, z)   # bottom-right
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                tiles.append((z, x, y))
    return tiles, (s, w, n, e)

def map_estimate(lat, lon, radius_km, zmin, zmax):
    tiles, _ = _map_tile_list(lat, lon, radius_km, zmin, zmax)
    return {"tiles": len(tiles), "mb": round(len(tiles) * 22 / 1024, 1)}  # ~22 KB per USGS tile

def _safe_map_name(name):
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", (name or "").strip()).strip("-.")
    return name[:60] or "map"

def list_maps():
    """MBTiles files in tiles/ with size and any stored metadata."""
    out = []
    if not os.path.isdir(TILES_DIR):
        return out
    for f in sorted(os.listdir(TILES_DIR)):
        if not f.lower().endswith(".mbtiles"):
            continue
        p = os.path.join(TILES_DIR, f)
        meta = {}
        try:
            c = sqlite3.connect(p)
            meta = dict(c.execute("SELECT name, value FROM metadata").fetchall())
            c.close()
        except Exception:
            pass
        out.append({"file": f, "size_mb": round(os.path.getsize(p) / 1048576, 1),
                    "name": meta.get("name", f), "bounds": meta.get("bounds"),
                    "minzoom": meta.get("minzoom"), "maxzoom": meta.get("maxzoom"),
                    "attribution": meta.get("attribution", "")})
    return out

def _active_map_path():
    """tiles/<map_file>, or the legacy <state>.mbtiles, or None."""
    with cfglock:
        mf = (config.get("map_file") or "").strip()
        st = (config.get("map_state") or "").strip()
    if mf:
        p = os.path.join(TILES_DIR, os.path.basename(mf))
        if os.path.isfile(p):
            return p
    if st:
        p = os.path.join(TILES_DIR, st.lower().replace(" ", "-") + ".mbtiles")
        if os.path.isfile(p):
            return p
    return None

def _map_download_worker(name, lat, lon, radius_km, zmin, zmax, source):
    src = MAP_SOURCES[source]
    tiles, (s, w, n, e) = _map_tile_list(lat, lon, radius_km, zmin, zmax)
    os.makedirs(TILES_DIR, exist_ok=True)
    final = os.path.join(TILES_DIR, name + ".mbtiles")
    part = final + ".part"
    with _map_dl_lock:
        _map_dl.update(running=True, name=name, done=0, total=len(tiles), failed=0, error=None,
                       file=None, cancel=False, started=datetime.now(timezone.utc).isoformat(), finished=None)
    log.info("Map download: %s — %d tiles, z%d-%d, %.0f km around %.4f,%.4f (%s)",
             name, len(tiles), zmin, zmax, radius_km, lat, lon, source)
    conn = None
    try:
        if os.path.exists(part):
            os.remove(part)
        conn = sqlite3.connect(part, check_same_thread=False)
        conn.executescript("""
            CREATE TABLE metadata (name TEXT, value TEXT);
            CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
            CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);
        """)
        conn.executemany("INSERT INTO metadata VALUES (?, ?)", [
            ("name", name), ("format", "jpg"), ("type", "baselayer"), ("version", "1"),
            ("description", f"HamLink offline map, {radius_km:.0f} km around {lat:.4f},{lon:.4f}"),
            ("bounds", f"{w:.5f},{s:.5f},{e:.5f},{n:.5f}"), ("center", f"{lon:.5f},{lat:.5f},{zmin}"),
            ("minzoom", str(zmin)), ("maxzoom", str(zmax)), ("attribution", src["attribution"]),
        ])
        conn.commit()
        import concurrent.futures
        db_lock = threading.Lock()
        headers = {"User-Agent": f"HamLink-Radio/{__version__} (offline map cache)"}

        def fetch(t):
            z, x, y = t
            if _map_dl["cancel"]:
                return None
            url = src["url"].format(z=z, x=x, y=y)
            for attempt in range(3):
                try:
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=20) as r:
                        data = r.read()
                    if data:
                        with db_lock:
                            conn.execute("INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?)",
                                         (z, x, (2 ** z - 1) - y, sqlite3.Binary(data)))
                    return True
                except urllib.error.HTTPError as ex:
                    if ex.code == 404:
                        return True   # no tile there (ocean / outside coverage) — not a failure
                    time.sleep(0.5 * (attempt + 1))
                except Exception:
                    time.sleep(0.5 * (attempt + 1))
            return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            for i, ok in enumerate(pool.map(fetch, tiles), 1):
                with _map_dl_lock:
                    _map_dl["done"] = i
                    if ok is False:
                        _map_dl["failed"] += 1
                if i % 200 == 0:
                    with db_lock:
                        conn.commit()
                if _map_dl["cancel"]:
                    break
        with db_lock:
            conn.commit()
        conn.close()
        conn = None
        if _map_dl["cancel"]:
            os.remove(part)
            with _map_dl_lock:
                _map_dl.update(running=False, error="Cancelled", finished=datetime.now(timezone.utc).isoformat())
            log.info("Map download cancelled")
            return
        if os.path.exists(final):
            os.remove(final)
        os.replace(part, final)
        with cfglock:
            config["map_file"] = name + ".mbtiles"
            save_config(config)
        with _map_dl_lock:
            _map_dl.update(running=False, file=name + ".mbtiles", finished=datetime.now(timezone.utc).isoformat())
        log.info("Map download complete: %s (%d tiles, %d failed)", final, len(tiles), _map_dl["failed"])
    except Exception as ex:
        log.error("Map download failed: %s", ex)
        try:
            if conn:
                conn.close()
            if os.path.exists(part):
                os.remove(part)
        except Exception:
            pass
        with _map_dl_lock:
            _map_dl.update(running=False, error=str(ex), finished=datetime.now(timezone.utc).isoformat())

def start_map_download(name, lat, lon, radius_km, zmin, zmax, source):
    with _map_dl_lock:
        if _map_dl["running"]:
            return False, "A map download is already running"
    if source not in MAP_SOURCES:
        return False, "Unknown map source"
    zmin = max(3, min(int(zmin), 16))
    zmax = max(zmin, min(int(zmax), MAP_SOURCES[source]["max_zoom"]))
    radius_km = max(5.0, min(float(radius_km), 600.0))
    est = map_estimate(lat, lon, radius_km, zmin, zmax)
    if est["tiles"] > 150000:
        return False, f"That area needs {est['tiles']} tiles — reduce the radius or the max zoom"
    threading.Thread(target=_map_download_worker, args=(_safe_map_name(name), float(lat), float(lon),
                     radius_km, zmin, zmax, source), daemon=True).start()
    return True, ""

# ---------------------------------------------------------------------------
# Settings tests + validation (the "Test" buttons in Settings)
# ---------------------------------------------------------------------------
def _test_tcp(host, port, what):
    host = (host or "127.0.0.1").strip() or "127.0.0.1"
    if host in ("localhost",):
        host = "127.0.0.1"
    try:
        port = int(port)
    except (TypeError, ValueError):
        return {"ok": False, "message": f"Invalid port for {what}"}
    if _port_open(host, port, timeout=3):
        return {"ok": True, "message": f"{what} is listening on {host}:{port}"}
    return {"ok": False, "message": f"Nothing is listening on {host}:{port} — is {what} running?"}

def _test_aprs_is(server, port, callsign, passcode):
    """Log in to APRS-IS and read the server's verdict on the passcode."""
    callsign = (callsign or "").strip().upper()
    if not callsign:
        return {"ok": False, "message": "Enter a home callsign first"}
    pc = (passcode or "").strip() or str(aprs_passcode(callsign))
    try:
        with socket.create_connection(((server or "rotate.aprs2.net").strip(), int(port or 14580)), timeout=8) as s:
            s.settimeout(8)
            banner = s.recv(512).decode("ascii", "replace").strip()
            s.sendall(f"user {callsign} pass {pc} vers HamLink-Radio {__version__}\r\n".encode())
            resp = ""
            for _ in range(5):
                chunk = s.recv(512).decode("ascii", "replace")
                resp += chunk
                if "logresp" in resp or not chunk:
                    break
    except Exception as e:
        return {"ok": False, "message": f"Could not reach APRS-IS: {e}"}
    line = next((l for l in resp.splitlines() if "logresp" in l), resp.strip() or banner)
    if "verified" in line and "unverified" not in line:
        return {"ok": True, "message": f"Logged in to APRS-IS as {callsign} (passcode verified). {line.strip('# ')}"}
    if "unverified" in line:
        return {"ok": False, "message": f"Connected, but the passcode is wrong for {callsign} (server says: {line.strip('# ')}). Use Auto-generate."}
    return {"ok": False, "message": f"Unexpected reply from APRS-IS: {line.strip() or 'no response'}"}

def _test_aprs_fi(key):
    key = (key or "").strip()
    if not key:
        return {"ok": False, "message": "Enter an aprs.fi API key first"}
    calls = _aprs_traveler_calls() or ["W1AW"]
    try:
        url = "https://api.aprs.fi/api/get?" + urllib.parse.urlencode(
            {"name": calls[0], "what": "loc", "apikey": key, "format": "json"})
        req = urllib.request.Request(url, headers={"User-Agent": "HamLink-Radio"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "message": f"aprs.fi request failed: {e}"}
    if d.get("result") == "ok":
        n = len(d.get("entries") or [])
        return {"ok": True, "message": f"aprs.fi key works. {calls[0]}: {n} position record(s) on file."}
    return {"ok": False, "message": f"aprs.fi says: {d.get('description') or d.get('result')}"}

def _test_pat(http_addr, exe_path):
    parts = []
    exe_ok = bool(exe_path) and os.path.isfile(exe_path)
    parts.append("pat.exe found" if exe_ok else ("pat.exe NOT found at that path" if exe_path else "no pat.exe path set"))
    addr = (http_addr or "localhost:8080").strip()
    try:
        with urllib.request.urlopen(urllib.request.Request(f"http://{addr}/api/mailbox/in"), timeout=5) as r:
            n = len(json.loads(r.read().decode()) or [])
        parts.append(f"Pat is running on {addr} ({n} messages in inbox)")
        ok = True
    except Exception as e:
        parts.append(f"Pat is not answering on {addr} ({str(e)[:60]}) — it is launched automatically at startup when the path is set")
        ok = exe_ok
    return {"ok": ok, "message": ". ".join(parts)}

def _test_github(token):
    try:
        h = {"User-Agent": f"HamLink-Radio/{__version__}", "Accept": "application/vnd.github+json"}
        if (token or "").strip():
            h["Authorization"] = f"Bearer {token.strip()}"
        req = urllib.request.Request(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest", headers=h)
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode())
        return {"ok": True, "message": f"GitHub reachable — latest release is {d.get('tag_name')}" + (" (token accepted)" if token else "")}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"ok": False, "message": "GitHub returned 404: the repository is private and this token (or no token) cannot see it"}
        if e.code in (401, 403):
            return {"ok": False, "message": f"GitHub rejected the token (HTTP {e.code})"}
        return {"ok": False, "message": f"GitHub error HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "message": f"Could not reach GitHub: {e}"}

_CALL_RE = re.compile(r"^[A-Z0-9]{1,3}[0-9][A-Z0-9]{0,3}[A-Z](?:-(?:[0-9]|1[0-5]))?$")

def validate_config(c):
    """Return a list of {level, field, message} for things that look wrong."""
    w = []
    def warn(field, msg, level="warn"):
        w.append({"level": level, "field": field, "message": msg})
    home = (c.get("home_callsign") or "").strip().upper()
    if not home:
        warn("home_callsign", "Home station callsign is empty — replies cannot be sent.", "error")
    elif "-" in home:
        warn("home_callsign", f"Home callsign '{home}' includes an SSID. Enter the base callsign only ({home.split('-')[0]}); the APRS SSID is added from the APRS section, and VarAC/Winlink use the base callsign.")
    watch = [x.strip().upper() for x in c.get("watch_callsigns", []) if x.strip()]
    if not watch:
        warn("watch_callsigns", "No watch callsigns — every sender will trigger an alert.", "info")
    if watch and all("-" in x for x in watch):
        warn("watch_callsigns", "Watch callsigns are matched exactly against VarAC's 'from' field. VarAC usually shows the traveler as CALL or CALL/P, not CALL-9 — add that form too (e.g. 'KK4ODA, KK4ODA/P').")
    ap = c.get("aprs", {})
    if ap.get("enabled") and not home:
        warn("aprs", "APRS-IS is enabled but no home callsign is set.", "error")
    ssid = (ap.get("home_ssid") or "").strip()
    if ssid and not re.match(r"^-(?:[0-9]|1[0-5])$", ssid):
        warn("aprs.home_ssid", f"Home SSID '{ssid}' should look like -5.")
    for s in (ap.get("traveler_ssids") or "").split(","):
        s = s.strip()
        if s and not re.match(r"^-?(?:[0-9]|1[0-5])$", s):
            warn("aprs.traveler_ssids", f"Traveler SSID '{s}' should look like -7 or -9.")
    if ap.get("rf_fallback") and not c.get("soundmodem", {}).get("enabled"):
        warn("aprs.rf_fallback", "APRS RF fallback is on but the Soundmodem section is disabled — RF fallback will never be used.")
    pt = c.get("pat", {})
    if pt.get("enabled"):
        if not pt.get("exe_path"):
            warn("pat.exe_path", "Winlink is enabled but the Pat executable path is empty — Pat will not be launched.")
        elif not os.path.isfile(pt["exe_path"]):
            warn("pat.exe_path", f"Pat executable not found: {pt['exe_path']}", "error")
        gw = (pt.get("rf_gateway") or "").strip().upper()
        if gw and not _CALL_RE.match(gw):
            warn("pat.rf_gateway", f"VARA FM gateway '{gw}' does not look like a callsign (expected e.g. WD5EMA-10).", "error")
        if pt.get("rf_fallback") and not gw:
            warn("pat.rf_fallback", "Winlink RF fallback is on but no VARA FM gateway is set.")
        if not pt.get("home_tactical") or not pt.get("traveler_tactical"):
            warn("pat.tactical", "Set both tactical addresses — Winlink refuses mail from a callsign to itself.")
    sm = c.get("soundmodem", {})
    if sm.get("enabled") and sm.get("exe_path") and not os.path.isfile(sm["exe_path"]):
        warn("soundmodem.exe_path", f"Soundmodem executable not found: {sm['exe_path']}", "error")
    if c.get("varac_exe_path") and not os.path.isfile(c["varac_exe_path"]):
        warn("varac_exe_path", f"VarAC executable not found: {c['varac_exe_path']}", "error")
    if c.get("varac_db_path") and not os.path.isfile(c["varac_db_path"]):
        warn("varac_db_path", f"VarAC database not found: {c['varac_db_path']}", "error")
    elif not c.get("varac_db_path"):
        warn("varac_db_path", "No VarAC database path — VarAC messages will not be monitored.", "info")
    b = c.get("beacon", {})
    if b.get("enabled") and not (b.get("lat") and b.get("lon")):
        warn("beacon", "Beacon is enabled but latitude/longitude are not set.", "error")
    if b.get("enabled") and b.get("via_rf") and not sm.get("enabled"):
        warn("beacon.via_rf", "RF beacon is on but the Soundmodem section is disabled.")
    po = c.get("pushover", {})
    if po.get("enabled") and not (po.get("user_key") and po.get("api_token")):
        warn("pushover", "Pushover is enabled but the user key or API token is missing.", "error")
    if c.get("map_file") and not os.path.isfile(os.path.join(TILES_DIR, os.path.basename(c["map_file"]))):
        warn("map_file", f"Offline map file not found in tiles/: {c['map_file']}")
    return w

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_shutdown_done = False

def _kill_proc(name, proc):
    """Terminate a subprocess, with escalation to kill."""
    if not proc or proc.poll() is not None:
        return
    log.info("Stopping %s (PID %d)...", name, proc.pid)
    try:
        proc.terminate()
        proc.wait(timeout=5)
        log.info("%s stopped.", name)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=3)
            log.info("%s killed.", name)
        except Exception:
            log.warning("Could not stop %s", name)

def _kill_by_name(exe_name):
    """Kill a Windows process by executable name using taskkill."""
    if sys.platform != "win32":
        return
    try:
        subprocess.run(["taskkill", "/F", "/IM", exe_name],
                       capture_output=True, timeout=5)
        log.info("Killed %s via taskkill", exe_name)
    except Exception as e:
        log.warning("taskkill %s failed: %s", exe_name, e)

def _cleanup():
    """Terminate all launched subprocesses and stop background threads."""
    global _aprs_running, _kiss_running, _beacon_running, _relay_running, _shutdown_done
    if _shutdown_done:
        return
    _shutdown_done = True
    log.info("HamLink shutting down — stopping threads and processes...")

    # Signal all background threads to stop
    _aprs_running = False
    _kiss_running = False
    _beacon_running = False
    _relay_running = False

    # Terminate launched subprocesses (direct references, not globals lookup)
    _kill_proc("VarAC", _varac_proc)
    _kill_proc("Soundmodem", _soundmodem_proc)
    _kill_proc("Pat", _pat_proc)
    _kill_proc("VARA FM", _varafm_proc)

    # Kill VARA HF by process name (launched by VarAC, not tracked by HamLink)
    _kill_by_name("VARA.exe")
    # Also kill Soundmodem by name in case the proc handle didn't work
    _kill_by_name("soundmodem.exe")

    log.info("HamLink shutdown complete.")

atexit.register(_cleanup)

def _signal_handler(signum, frame):
    """Handle Ctrl+C and SIGTERM for clean shutdown."""
    log.info("Signal %d received, shutting down...", signum)
    _cleanup()
    sys.exit(0)

signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder=os.path.join(RES_DIR, "static"),
            static_url_path="/static")

# Simple CSRF protection: generate a token per session, validate on POST.
# The token is handed to the browser in /api/status and sent back either as
# the X-CSRF-Token header (dashboard JS), a `_csrf` JSON field, or a `_csrf`
# form field (the phone quick-reply confirmation page).
_csrf_token = str(uuid.uuid4())

@app.before_request
def _csrf_check():
    """Validate CSRF token on state-changing POST requests."""
    if request.method != "POST":
        return None
    token = request.headers.get("X-CSRF-Token", "")
    if not token:
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            token = body.get("_csrf", "")
    if not token:
        token = request.form.get("_csrf", "")
    if token != _csrf_token:
        return jsonify({"ok": False, "error": "Invalid CSRF token"}), 403
    return None

@app.route("/")
def index():
    # Served as a plain response: the page has no template variables, and
    # bypassing Jinja avoids parsing 2000+ lines of HTML/JS on every load.
    return Response(HTML_PAGE, mimetype="text/html")

@app.route("/api/version")
def api_version():
    return jsonify({"ok": True, "version": __version__, "demo": DEMO_MODE})

# ---------------------------------------------------------------------------
# MBTiles tile server
# ---------------------------------------------------------------------------
_mbtiles_conn = None
_mbtiles_path = None
_mbtiles_lock = threading.Lock()  # Flask serves tiles concurrently; sqlite conn is shared
_EMPTY_TILE = Response(b"", status=204)

@app.route("/tiles/<int:z>/<int:x>/<int:y>.png")
def serve_tile(z, x, y):
    """Serve map tiles from a local MBTiles file."""
    global _mbtiles_conn, _mbtiles_path
    fpath = _active_map_path()
    if not fpath:
        return Response(b"", status=204)
    # MBTiles uses TMS y-coordinate (flipped from XYZ)
    tms_y = (2 ** z - 1) - y
    with _mbtiles_lock:
        # Cache connection, reopen if path changed
        if _mbtiles_path != fpath or _mbtiles_conn is None:
            if _mbtiles_conn:
                try:
                    _mbtiles_conn.close()
                except Exception:
                    pass
                _mbtiles_conn = None
            if not os.path.isfile(fpath):
                return Response(b"", status=204)
            try:
                _mbtiles_conn = sqlite3.connect(fpath, check_same_thread=False)
                _mbtiles_path = fpath
            except Exception:
                return Response(b"", status=204)
        try:
            cur = _mbtiles_conn.cursor()
            cur.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                        (z, x, tms_y))
            row = cur.fetchone()
            if row:
                data = bytes(row[0])
                mt = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
                return Response(data, mimetype=mt)
        except Exception:
            pass
    return Response(b"", status=204)

@app.route("/api/map/list")
def api_map_list():
    with cfglock:
        active = (config.get("map_file") or "").strip()
    p = _active_map_path()
    return jsonify({"ok": True, "maps": list_maps(), "active": os.path.basename(p) if p else active,
                    "tiles_dir": TILES_DIR, "sources": {k: v["label"] for k, v in MAP_SOURCES.items()}})

@app.route("/api/map/estimate", methods=["POST"])
def api_map_estimate():
    d = request.get_json(force=True)
    try:
        return jsonify({"ok": True, **map_estimate(float(d["lat"]), float(d["lon"]), float(d.get("radius_km", 100)),
                                                   int(d.get("min_zoom", 5)), int(d.get("max_zoom", 12)))})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/map/download", methods=["POST"])
def api_map_download():
    d = request.get_json(force=True)
    try:
        ok, err = start_map_download(d.get("name") or "home-area", float(d["lat"]), float(d["lon"]),
                                     float(d.get("radius_km", 100)), int(d.get("min_zoom", 5)),
                                     int(d.get("max_zoom", 12)), d.get("source", "usgs_topo"))
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"ok": False, "error": f"Bad parameters: {e}"})
    return jsonify({"ok": ok, "error": err})

@app.route("/api/map/status")
def api_map_status():
    with _map_dl_lock:
        return jsonify({"ok": True, **{k: v for k, v in _map_dl.items() if k != "cancel"}})

@app.route("/api/map/cancel", methods=["POST"])
def api_map_cancel():
    with _map_dl_lock:
        _map_dl["cancel"] = True
    return jsonify({"ok": True})

@app.route("/api/map/select", methods=["POST"])
def api_map_select():
    d = request.get_json(force=True)
    f = os.path.basename((d.get("file") or "").strip())
    if f and not os.path.isfile(os.path.join(TILES_DIR, f)):
        return jsonify({"ok": False, "error": "File not found in tiles/"})
    with cfglock:
        config["map_file"] = f
        save_config(config)
    return jsonify({"ok": True, "active": f})

@app.route("/api/map/delete", methods=["POST"])
def api_map_delete():
    d = request.get_json(force=True)
    f = os.path.basename((d.get("file") or "").strip())
    p = os.path.join(TILES_DIR, f)
    if not f.lower().endswith(".mbtiles") or not os.path.isfile(p):
        return jsonify({"ok": False, "error": "File not found"})
    global _mbtiles_conn, _mbtiles_path
    with _mbtiles_lock:
        if _mbtiles_path == p and _mbtiles_conn:
            try:
                _mbtiles_conn.close()
            except Exception:
                pass
            _mbtiles_conn = None
            _mbtiles_path = None
    try:
        os.remove(p)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)})
    with cfglock:
        if (config.get("map_file") or "") == f:
            config["map_file"] = ""
            save_config(config)
    return jsonify({"ok": True})

@app.route("/api/test", methods=["POST"])
def api_test():
    """Generic settings tester used by the Test buttons in Settings."""
    d = request.get_json(force=True) or {}
    what = d.get("what", "")
    try:
        if what == "varac_exe":
            p = d.get("path", "")
            if not p:
                return jsonify({"ok": False, "message": "No path entered"})
            if not os.path.isfile(p):
                return jsonify({"ok": False, "message": "File not found"})
            running = _is_process_running(os.path.basename(p))
            return jsonify({"ok": True, "message": f"Found {os.path.basename(p)} — VarAC is {'running' if running else 'not running (it will be launched at startup)'}"})
        if what == "bbs_dir":
            override = (d.get("path") or "").strip()
            bbs = override or get_bbs_directory()
            if not bbs:
                return jsonify({"ok": False, "message": "No BBS directory: set the VarAC exe path (it is read from VarAC.ini) or enter an override"})
            if not os.path.isdir(bbs):
                return jsonify({"ok": False, "message": f"Directory not found: {bbs}"})
            n = len([f for f in os.listdir(bbs) if "SITREP" in f.upper()])
            return jsonify({"ok": True, "message": f"BBS folder OK: {bbs} ({n} sitrep file(s))"})
        if what == "aprs_is":
            return jsonify(_test_aprs_is(d.get("server"), d.get("port"), d.get("callsign"), d.get("passcode")))
        if what == "aprs_fi":
            return jsonify(_test_aprs_fi(d.get("key")))
        if what == "kiss":
            return jsonify(_test_tcp(d.get("host"), d.get("port"), "Soundmodem KISS"))
        if what == "varafm":
            addr = (d.get("addr") or "localhost:8300").rsplit(":", 1)
            return jsonify(_test_tcp("127.0.0.1", addr[-1], "VARA FM"))
        if what == "pat":
            return jsonify(_test_pat(d.get("http_addr"), d.get("exe_path")))
        if what == "exe":
            p = d.get("path", "")
            return jsonify({"ok": bool(p) and os.path.isfile(p), "message": "File found" if p and os.path.isfile(p) else ("File not found" if p else "No path entered")})
        if what == "github":
            return jsonify(_test_github(d.get("token")))
        if what == "beacon":
            lat, lon = float(d.get("lat") or 0), float(d.get("lon") or 0)
            if not lat or not lon:
                return jsonify({"ok": False, "message": "Latitude/longitude not set"})
            base = (d.get("callsign") or "").strip().upper().split("-")[0]
            if not base:
                return jsonify({"ok": False, "message": "Set the home callsign first"})
            pos = f"!{_lat_to_aprs(lat)}{d.get('symbol_table') or '/'}{_lon_to_aprs(lon)}{d.get('symbol_code') or '-'}{d.get('comment') or 'HamLink Radio'}"
            return jsonify({"ok": True, "message": f"Beacon packet preview (not transmitted): {base}{d.get('ssid') or '-5'}>APRS,TCPIP*:{pos}"})
        if what == "validate":
            with cfglock:
                snap = copy.deepcopy(config)
            return jsonify({"ok": True, "warnings": validate_config(snap)})
        return jsonify({"ok": False, "message": f"Unknown test '{what}'"})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Test failed: {e}"})

@app.route("/api/open_folder", methods=["POST"])
def api_open_folder():
    """Open the HamLink folder (where config.json lives) in the file manager."""
    try:
        if sys.platform == "win32":
            os.startfile(APP_DIR)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", APP_DIR])
        else:
            subprocess.Popen(["xdg-open", APP_DIR])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/status")
def api_status():
    # Snapshot config under cfglock FIRST (never nest slock -> cfglock).
    # A deep copy of the whole config keeps the settings form complete — any
    # key missing here would be blanked the next time the user hit Save.
    with cfglock:
        cfg_snap = copy.deepcopy(config)
    ap = cfg_snap.setdefault("aprs", {})
    ap["traveler_ssids"] = ap.get("traveler_ssids") or ap.get("traveler_ssid", "-7")
    # Resolve BBS directory outside cfglock to avoid deadlock
    cfg_snap["bbs_directory_resolved"] = get_bbs_directory() or ""
    # Now snapshot state under slock (no cfglock held — no deadlock possible)
    with slock:
        for a in state["history"]:
            a["friendly_time"] = _friendly_time(a.get("time"))
        for a in state["pending_alerts"]:
            a["friendly_time"] = _friendly_time(a.get("time"))
        return jsonify({
            "pending": list(state["pending_alerts"]),
            "history": list(state["history"]),
            "last_poll": state["last_poll"],
            "db_connected": state["db_connected"],
            "error": state["error"],
            "last_checkin_time": state["last_checkin_time"],
            "last_checkin_from": state["last_checkin_from"],
            "last_checkin_friendly": _friendly_time(state["last_checkin_time"]),
            "reply_status": state["reply_status"],
            "aprs_connected": state["aprs_connected"],
            "aprs_error": state["aprs_error"],
            "kiss_connected": state["kiss_connected"],
            "kiss_error": state["kiss_error"],
            "pat_connected": state["pat_connected"],
            "pat_error": state["pat_error"],
            "pat_next_sync": state["pat_next_sync"],
            "pat_using_rf": state["pat_using_rf"],
            "aprs_last_position": state["aprs_last_position"],
            "aprs_last_ack": state["aprs_last_ack"],
            "winlink_last_position": state["winlink_last_position"],
            "alarm_silenced": state["alarm_silenced"],
            "internet_up": state["internet_up"],
            "config": cfg_snap,
            "csrf_token": _csrf_token,
            "version": _running_version(),
            "demo": DEMO_MODE,
            "update": dict(state["update"]),
            "config_path": CONFIG_PATH,
            "app_dir": APP_DIR,
            "tiles_dir": TILES_DIR,
            "config_saved_at": state.get("config_saved_at"),
        })

@app.route("/api/update/status")
def api_update_status():
    with slock:
        return jsonify({"ok": True, "update": dict(state["update"])})

@app.route("/api/update/check", methods=["POST"])
def api_update_check():
    """Check GitHub for a newer release right now."""
    check_for_update(force=True)
    with slock:
        u = dict(state["update"])
    return jsonify({"ok": not u.get("error"), "error": u.get("error"), "update": u})

@app.route("/api/update/apply", methods=["POST"])
def api_update_apply():
    """Download and install the latest release, then restart."""
    ok, err = apply_update()
    return jsonify({"ok": ok, "error": err})

@app.route("/api/acknowledge", methods=["POST"])
def api_ack():
    d = request.get_json(force=True)
    aid = d.get("id")
    if aid:
        with slock:
            state["acknowledged_ids"].add(aid)
            state["pending_alerts"] = [a for a in state["pending_alerts"] if a["id"] != aid]
            if not state["pending_alerts"]:
                stop_speaker_alarm()
    return jsonify({"ok": True})

@app.route("/api/acknowledge_all", methods=["POST"])
def api_ack_all():
    with slock:
        for a in state["pending_alerts"]:
            state["acknowledged_ids"].add(a["id"])
        state["pending_alerts"] = []
    stop_speaker_alarm()
    return jsonify({"ok": True})

@app.route("/api/delete_vmail", methods=["POST"])
def api_delete_vmail():
    """Hard-delete a VarAC vmail row (and its attachments) from VarAC.db.
    VarAC's own delete only soft-flags rows, which leaves them visible in
    VarAC's inbox — this removes them for good and advances the HWM."""
    d = request.get_json(force=True)
    aid = (d.get("id") or "").strip()
    if not aid.startswith("vmail-"):
        return jsonify({"ok": False, "error": "Not a VarAC vmail id"})
    try:
        vmail_id = int(aid.split("-", 1)[1])
    except (ValueError, IndexError):
        return jsonify({"ok": False, "error": "Bad vmail id"})
    try:
        conn = _connect()
        cur = conn.cursor()
        # Hard-delete the row — VarAC's own "delete" only sets is_deleted=1,
        # which leaves the row visible in VarAC's UI. Purge attachments first.
        cur.execute("SELECT guid FROM vmail WHERE id = ?", (vmail_id,))
        row = cur.fetchone()
        if row and row["guid"]:
            cur.execute("DELETE FROM vmail_attachment WHERE vmail_guid = ?", (row["guid"],))
        cur.execute("DELETE FROM vmail WHERE id = ?", (vmail_id,))
        affected = cur.rowcount
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("Delete vmail %d failed: %s", vmail_id, e)
        return jsonify({"ok": False, "error": str(e)})
    with slock:
        state["acknowledged_ids"].add(aid)
        state["pending_alerts"] = [a for a in state["pending_alerts"] if a["id"] != aid]
        state["history"] = [a for a in state["history"] if a.get("id") != aid]
        # Advance HWM past this id so it can't be re-fetched this session
        if vmail_id > state.get("vmail_hwm", 0):
            state["vmail_hwm"] = vmail_id
        if not state["pending_alerts"]:
            stop_speaker_alarm()
    log.info("VarAC vmail %d hard-deleted from VarAC.db (rows affected=%d)", vmail_id, affected)
    return jsonify({"ok": True, "affected": affected})

@app.route("/api/dismiss_from_phone")
def api_dismiss_from_phone():
    """Silence the alarm from a phone link. Alerts remain in pending so the
    dashboard still shows them as new messages (user can reply from the PC)."""
    stop_speaker_alarm()
    with slock:
        state["alarm_silenced"] = True
    log.info("Alarm silenced from phone (alerts remain pending for reply)")
    return """<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
    <style>body{font-family:system-ui;text-align:center;padding:40px 20px;background:#f0fdf4;color:#166534}
    h2{font-size:20px}p{color:#64748b;margin-top:8px}</style></head>
    <body><h2>Alarm Silenced</h2><p>The alarm has been stopped. Messages remain on the dashboard so you can reply from the PC.</p></body></html>"""

@app.route("/api/stop_alarm", methods=["POST"])
def api_stop_alarm():
    """Stop the PC speaker alarm without removing alerts from pending."""
    stop_speaker_alarm()
    with slock:
        state["alarm_silenced"] = True
    return jsonify({"ok": True})

@app.route("/api/pushover_reply")
def api_pushover_reply():
    """Show confirmation page before transmitting. Requires explicit tap to send."""
    channel = request.args.get("channel", "varac")
    msg = request.args.get("message", "").strip()
    if not msg:
        return "<html><body><h2>Error: No message</h2></body></html>", 400
    safe_msg = html.escape(msg)
    safe_channel = html.escape(channel)

    # Determine if this channel would use non-compliant RF
    nc_rf = False
    nc_reason = ""
    if channel == "aprs":
        with slock:
            aprs_connected = state.get("aprs_connected", False)
            kiss_connected = state.get("kiss_connected", False)
        if kiss_connected and not aprs_connected:
            nc_rf = True
            nc_reason = "APRS via Soundmodem (RF) because internet is unavailable"
    elif channel == "winlink":
        with cfglock:
            wl_rf = config.get("pat", {}).get("rf_fallback", False)
        if wl_rf and not _check_internet(timeout=2):
            nc_rf = True
            nc_reason = "Winlink via VARA FM (RF) because internet is unavailable"

    if nc_rf:
        # Non-compliant RF — show blocking page with licensed/emergency override
        return f"""<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
    <style>
      body{{font-family:system-ui;padding:30px 20px;background:#f8f9fb;color:#1e293b;max-width:480px;margin:0 auto}}
      .card{{background:#fff;border-radius:14px;padding:24px;box-shadow:0 1px 3px rgba(0,0,0,0.1);margin-bottom:16px}}
      h2{{font-size:20px;margin-bottom:12px;color:#dc2626}}
      .msg{{background:#f1f5f9;padding:12px;border-radius:8px;font-size:15px;margin:12px 0}}
      .warn-block{{background:#fef2f2;border:1px solid #fca5a5;border-radius:8px;padding:12px;font-size:13px;color:#991b1b;margin:16px 0}}
      .btn{{display:block;width:100%;padding:16px;border:none;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;margin-top:10px}}
      .btn-licensed{{background:#eff6ff;color:#1d4ed8;border:1px solid #2563eb}}
      .btn-emergency{{background:#fef2f2;color:#dc2626;border:1px solid #dc2626}}
      .btn-cancel{{background:#e2e8f0;color:#64748b}}
      .via{{font-size:13px;color:#64748b;margin-top:4px}}
    </style></head>
    <body>
      <div class="card">
        <h2>RF Bandwidth Compliance Warning</h2>
        <p>This message will be transmitted via {nc_reason}.</p>
        <div class="msg">{safe_msg}</div>
        <div class="via">Channel: {safe_channel.upper()} (RF fallback)</div>
      </div>
      <div class="warn-block">
        <strong>FCC Part 97.221:</strong> Automatically controlled digital stations must not exceed
        500 Hz occupied bandwidth. {safe_channel.upper()} via RF exceeds this limit and is
        <strong>blocked by default</strong>.
        <br><br>
        To proceed, you must confirm one of the following:
      </div>
      <form method="POST" action="/api/pushover_reply_send">
        <input type="hidden" name="_csrf" value="{_csrf_token}">
        <input type="hidden" name="channel" value="{safe_channel}">
        <input type="hidden" name="message" value="{safe_msg}">
        <button type="submit" name="override" value="licensed" class="btn btn-licensed">I am a licensed operator and I am present at or supervising this station</button>
        <button type="submit" name="override" value="emergency" class="btn btn-emergency">Emergency &mdash; immediate safety of life or property (FCC Part 97.403)</button>
      </form>
      <button class="btn btn-cancel" onclick="window.close()">Cancel</button>
    </body></html>"""

    if channel == "varac":
        heading = "Confirm Message"
        description = ("Your message will be queued to the VarAC outbox. "
                        "It will be transmitted when the remote operator next connects to your station.")
        via_label = "Channel: VARAC (queued to outbox)"
        btn_label = "Queue Message"
    elif channel == "winlink":
        heading = "Confirm Transmission"
        description = ("Your message will be sent via Winlink (internet). "
                        "No RF transmission will occur from your station.")
        via_label = "Channel: WINLINK (internet)"
        btn_label = "Confirm &amp; Send"
    else:
        heading = "Confirm Transmission"
        description = ("Your message will be sent via APRS-IS (internet). "
                        "No RF transmission will occur from your station.")
        via_label = "Channel: APRS (internet)"
        btn_label = "Confirm &amp; Send"

    return f"""<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
    <style>
      body{{font-family:system-ui;padding:30px 20px;background:#f8f9fb;color:#1e293b;max-width:480px;margin:0 auto}}
      .card{{background:#fff;border-radius:14px;padding:24px;box-shadow:0 1px 3px rgba(0,0,0,0.1);margin-bottom:16px}}
      h2{{font-size:20px;margin-bottom:12px}}
      .msg{{background:#f1f5f9;padding:12px;border-radius:8px;font-size:15px;margin:12px 0}}
      .info{{background:#f0f9ff;border:1px solid #7dd3fc;border-radius:8px;padding:12px;font-size:13px;color:#0c4a6e;margin:16px 0}}
      .btn{{display:block;width:100%;padding:16px;border:none;border-radius:10px;font-size:16px;font-weight:700;cursor:pointer;margin-top:10px}}
      .btn-send{{background:#16a34a;color:#fff}}
      .btn-cancel{{background:#e2e8f0;color:#64748b}}
      .via{{font-size:13px;color:#64748b;margin-top:4px}}
    </style></head>
    <body>
      <div class="card">
        <h2>{heading}</h2>
        <p>{description}</p>
        <div class="msg">{safe_msg}</div>
        <div class="via">{via_label}</div>
      </div>
      <div class="info">
        All amateur radio transmissions are made under the authority of the station license.
        The station licensee is responsible for all messages sent through this application (FCC Part 97.115).
      </div>
      <form method="POST" action="/api/pushover_reply_send">
        <input type="hidden" name="_csrf" value="{_csrf_token}">
        <input type="hidden" name="channel" value="{safe_channel}">
        <input type="hidden" name="message" value="{safe_msg}">
        <button type="submit" class="btn btn-send">{btn_label}</button>
      </form>
      <button class="btn btn-cancel" onclick="window.close()">Cancel</button>
    </body></html>"""


@app.route("/api/pushover_reply_send", methods=["POST"])
def api_pushover_reply_send():
    """Actually send the quick reply after user confirms."""
    channel = request.form.get("channel", "varac")
    msg = request.form.get("message", "").strip()
    if not msg:
        return "<html><body><h2>Error: No message</h2></body></html>", 400

    result = ""
    if channel == "aprs":
        to_call = _aprs_traveler_call()
        if to_call:
            ok, err = aprs_send_message(to_call, msg)
            result = f"Sent via APRS to {to_call}" if ok else f"APRS failed: {err}"
        else:
            result = "Error: No traveler callsign configured"
    elif channel == "winlink":
        with cfglock:
            traveler_tac = config.get("pat", {}).get("traveler_tactical", "")
            watch = config.get("watch_callsigns", [])
        to_addr = traveler_tac.upper() if traveler_tac else (
            watch[0].upper().split("-")[0].split("/")[0] if watch else "")
        if to_addr:
            if "@" not in to_addr:
                to_addr = to_addr + "@winlink.org"
            ok, err = pat_send_message(to_addr, "Reply", msg)
            if ok:
                result = f"Sent via Winlink to {to_addr}"
                threading.Thread(target=pat_connect_telnet, daemon=True).start()
            else:
                result = f"Winlink failed: {err}"
        else:
            result = "Error: No traveler address configured"
    else:
        # VarAC reply
        with cfglock:
            home_call = config.get("home_callsign", "").strip().upper()
        if not home_call:
            result = "Error: Home callsign not configured"
        else:
            to_call = ""
            with slock:
                for a in reversed(state["history"]):
                    if a.get("type") in ("vmail", "aprs", "winlink") and a.get("from_call"):
                        to_call = a["from_call"]
                        break
            if not to_call:
                with cfglock:
                    watch = config.get("watch_callsigns", [])
                if watch:
                    to_call = watch[0].upper().split("/")[0].strip()
            if to_call:
                try:
                    conn = _connect()
                    cur = conn.cursor()
                    cur.execute("SELECT folder_id FROM vmail_folder WHERE folder='Outbox'")
                    row = cur.fetchone()
                    outbox_id = row["folder_id"] if row else 3
                    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    g = str(uuid.uuid4())
                    cur.execute("""INSERT INTO vmail
                        (guid, creation_time, sent_time, received_time,
                         folder_id, vmail_to, vmail_from, vmail_via,
                         delivery_band, delivery_snr, subject, msg,
                         read_status, is_deleted, frequency, has_attachment, urgent)
                        VALUES (?, ?, NULL, NULL, ?, ?, ?, '', '', '', 'Reply', ?, 0, 0, 0, 0, 0)""",
                        (g, now_utc, outbox_id, to_call.upper(), home_call, msg))
                    conn.commit()
                    conn.close()
                    result = f"Queued to VarAC outbox for {to_call}"
                    log_reply(to_call, msg, channel="varac")
                except Exception as e:
                    result = f"VarAC error: {e}"
            else:
                result = "Error: No callsign to reply to"

    # Stop alarms since user responded — clear pending so browser alarm stops too
    stop_speaker_alarm()
    with slock:
        for a in state["pending_alerts"]:
            state["acknowledged_ids"].add(a["id"])
        state["pending_alerts"].clear()
        state["reply_status"] = f"Replied via {channel}: {msg[:40]}"
    log.info("Pushover quick reply [%s]: %s -> %s", channel, msg[:40], result)

    safe_result = html.escape(result)
    safe_msg = html.escape(msg[:50])
    return f"""<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
    <style>body{{font-family:system-ui;text-align:center;padding:40px 20px;background:#f0fdf4;color:#166534}}
    h2{{font-size:20px}}p{{color:#64748b;margin-top:8px}}</style></head>
    <body><h2>{"Message Queued" if channel == "varac" else "Reply Sent"}</h2><p>{safe_result}</p><p>"{safe_msg}"</p></body></html>"""


@app.route("/api/pat_config", methods=["GET"])
def api_pat_config_get():
    """Read Pat's config.json and return key fields."""
    pcfg = _pat_read_config()
    return jsonify({
        "ok": True,
        "path": _pat_config_path(),
        "mycall": pcfg.get("mycall", ""),
        "secure_login_password": pcfg.get("secure_login_password", ""),
        "locator": pcfg.get("locator", ""),
        "http_addr": pcfg.get("http_addr", "localhost:8080"),
        "schedule": pcfg.get("schedule", {}),
        "varafm_addr": pcfg.get("varafm", {}).get("addr", "localhost:8300"),
    })

@app.route("/api/pat_config", methods=["POST"])
def api_pat_config_set():
    """Update Pat's config.json with key fields."""
    d = request.get_json(force=True)
    pcfg = _pat_read_config()
    if "mycall" in d and d["mycall"]:
        pcfg["mycall"] = d["mycall"].upper()
    if "secure_login_password" in d:
        pcfg["secure_login_password"] = d["secure_login_password"]
    if "locator" in d and d["locator"]:
        pcfg["locator"] = d["locator"].upper()
    if "home_tactical" in d:
        tac = d["home_tactical"].upper().strip() if d["home_tactical"] else ""
        if tac:
            # Add to Pat's auxiliary_addresses if not already there
            aux = pcfg.get("auxiliary_addresses", [])
            # Normalize: Pat expects plain strings like "BRECKEN" or "BRECKEN:password"
            existing = [a.get("Address", a) if isinstance(a, dict) else a for a in aux]
            if tac not in existing:
                existing.append(tac)
            pcfg["auxiliary_addresses"] = existing
    # Clear Pat's internal schedule — HamLink controls sync timing now
    pcfg.pop("schedule", None)
    if "varafm_addr" in d and d["varafm_addr"]:
        if "varafm" not in pcfg:
            pcfg["varafm"] = {}
        pcfg["varafm"]["addr"] = d["varafm_addr"]
    # Ensure connect_aliases has telnet
    if "connect_aliases" not in pcfg:
        pcfg["connect_aliases"] = {}
    if "telnet" not in pcfg.get("connect_aliases", {}):
        mycall = pcfg.get("mycall", "N0CALL")
        pcfg["connect_aliases"]["telnet"] = f"telnet://{mycall}:CMSTelnet@cms.winlink.org:8772/wl2k"
    ok = _pat_write_config(pcfg)
    # Restart Pat so it picks up the new config
    if ok:
        _restart_pat()
    return jsonify({"ok": ok})

@app.route("/api/pat_gateways")
def api_pat_gateways():
    """List nearby VARA FM RMS gateways via Pat rmslist."""
    try:
        with cfglock:
            exe = config.get("pat", {}).get("exe_path", "")
        if not exe or not os.path.isfile(exe):
            return jsonify({"ok": False, "error": "Pat not configured", "gateways": []})
        proc = subprocess.run(
            [exe, "rmslist", "-s", "-m", "varafm"],
            cwd=os.path.dirname(exe),
            capture_output=True, timeout=30
        )
        output = (proc.stdout or b"").decode(errors="replace").strip()
        err_output = (proc.stderr or b"").decode(errors="replace").strip()

        if proc.returncode != 0 or not output:
            # Some Pat versions print to stderr
            error = err_output or output or "No gateways found. Check your grid locator in Pat config."
            return jsonify({"ok": False, "error": error[:300], "gateways": []})

        # Parse text output — each line is a gateway entry like:
        # "varafm:///W4DOG?freq=145050000 (FM P2P) [18 km]"
        # or "W4DOG 145.050 MHz varafm 18 km EM84"
        gateways = []
        for line in output.split("\n"):
            line = line.strip()
            if not line:
                continue
            gw = {"callsign": "", "info": line}
            # Try to extract callsign from varafm:///CALL? format
            if ":///" in line:
                try:
                    url_part = line.split("///")[1]
                    call = url_part.split("?")[0].split("/")[0].strip()
                    gw["callsign"] = call
                except (IndexError, ValueError):
                    pass
            # Try to extract callsign as first word if it looks like one
            if not gw["callsign"]:
                parts = line.split()
                if parts and len(parts[0]) >= 3:
                    gw["callsign"] = parts[0]
            # Extract distance if present
            dist_match = re.search(r'\[?\s*(\d+)\s*km\s*\]?', line, re.IGNORECASE)
            if dist_match:
                gw["distance"] = int(dist_match.group(1))
            gateways.append(gw)

        return jsonify({"ok": True, "gateways": gateways[:20]})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Pat rmslist timed out — try again", "gateways": []})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "gateways": []})

@app.route("/api/winlink_reply", methods=["POST"])
def api_winlink_reply():
    """Send a Winlink message via Pat."""
    d = request.get_json(force=True)
    msg = (d.get("message") or "").strip()
    to_addr = (d.get("to_address") or "").strip()
    subject = (d.get("subject") or "Message").strip()
    if not msg:
        return jsonify({"ok": False, "error": "Message cannot be empty"})
    with cfglock:
        home = config.get("home_callsign", "").strip().upper()
        traveler_tac = config.get("pat", {}).get("traveler_tactical", "")
    if not to_addr:
        # Default to traveler's tactical address, or callsign
        if traveler_tac:
            to_addr = traveler_tac.upper()
        else:
            with cfglock:
                watch = config.get("watch_callsigns", [])
            to_addr = watch[0].upper().split("-")[0].split("/")[0] if watch else home
    if not to_addr:
        return jsonify({"ok": False, "error": "No destination address"})
    # Add @winlink.org if no domain
    if "@" not in to_addr:
        to_addr = to_addr + "@winlink.org"
    ok, err = pat_send_message(to_addr, subject, msg)
    if ok:
        log.info("Winlink reply: FROM home tactical TO %s", to_addr)
        # Trigger telnet sync to send it
        threading.Thread(target=pat_connect_telnet, daemon=True).start()
        return jsonify({"ok": True, "to": to_addr})
    return jsonify({"ok": False, "error": err})

@app.route("/api/reply", methods=["POST"])
def api_reply():
    """Queue a VMail reply by inserting into the VarAC vmail table (Outbox)."""
    d = request.get_json(force=True)
    msg_text = (d.get("message") or "").strip()
    to_call = (d.get("to_callsign") or "").strip().upper()
    subject = (d.get("subject") or "Reply").strip()

    if not msg_text:
        return jsonify({"ok": False, "error": "Message cannot be empty"})

    with cfglock:
        home_call = config.get("home_callsign", "").strip().upper()
    if not home_call:
        return jsonify({"ok": False, "error": "Home callsign not configured. Set it in Settings."})
    if not to_call:
        # Try to find the traveler's callsign from any source
        with slock:
            last_from = None
            for a in reversed(state["history"]):
                if a.get("type") in ("vmail", "aprs", "winlink") and a.get("from_call"):
                    last_from = a["from_call"]
                    break
        if last_from:
            to_call = last_from.upper()
        else:
            # Fall back to first watch callsign (the traveler)
            with cfglock:
                watch = config.get("watch_callsigns", [])
            if watch:
                to_call = watch[0].upper().split("/")[0].strip()
            else:
                return jsonify({"ok": False, "error": "No callsign to reply to — add a watch callsign in Settings"})

    try:
        conn = _connect()
        cur = conn.cursor()

        # Determine folder_id for Outbox
        cur.execute("SELECT folder_id FROM vmail_folder WHERE folder='Outbox'")
        row = cur.fetchone()
        outbox_id = row["folder_id"] if row else 3  # fallback

        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        g = str(uuid.uuid4())

        # Relay-aware reply routing: if the original message came via a relay,
        # route the reply back through the same relay station
        via = (d.get("relay_via") or "").strip()
        if not via:
            with cfglock:
                route_via = config.get("relay", {}).get("route_replies_via_relay", True)
            if route_via:
                with slock:
                    via = state.get("relay_paths", {}).get(to_call, "")
        if via:
            log.info("Reply to %s routed via relay %s", to_call, via)

        cur.execute("""INSERT INTO vmail
            (guid, creation_time, sent_time, received_time,
             folder_id, vmail_to, vmail_from, vmail_via,
             delivery_band, delivery_snr, subject, msg,
             read_status, is_deleted, frequency, has_attachment, urgent)
            VALUES (?, ?, NULL, NULL, ?, ?, ?, ?, '', '', ?, ?, 0, 0, 0, 0, 0)""",
            (g, now_utc, outbox_id, to_call, home_call, via, subject, msg_text))

        conn.commit()
        conn.close()

        with slock:
            state["reply_status"] = {
                "time": now_utc, "to": to_call,
                "message": msg_text[:100], "status": "queued",
                "via": via,
            }

        via_msg = f" via relay {via}" if via else ""
        log.info("Reply queued to %s%s: %s", to_call, via_msg, msg_text[:60])
        log_reply(to_call, msg_text, channel="varac")
        return jsonify({"ok": True, "to": to_call, "via": via})

    except Exception as e:
        log.error("Reply failed: %s", e)
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/config", methods=["POST"])
def api_set_config():
    d = request.get_json(force=True)
    old_db = config.get("varac_db_path", "")
    with cfglock:
        for k in ["varac_db_path", "varac_exe_path", "varac_profile",
                   "bbs_directory",
                   "poll_interval_seconds", "watch_callsigns",
                   "alert_sound", "alert_volume", "alarm_timeout_minutes", "operator_name", "home_callsign",
                   "quick_replies", "map_state", "map_file"]:
            if k in d:
                config[k] = d[k]
        if "pushover" in d:
            for pk in ["enabled", "user_key", "api_token", "priority", "sound", "quick_replies"]:
                if pk in d["pushover"]:
                    config["pushover"][pk] = d["pushover"][pk]
        if "aprs" in d:
            for ak in ["enabled", "home_ssid", "traveler_ssids", "passcode", "server", "port", "use_mailbox", "rf_fallback", "aprs_fi_api_key"]:
                if ak in d["aprs"]:
                    config["aprs"][ak] = d["aprs"][ak]
        if "soundmodem" in d:
            for sk in ["enabled", "exe_path", "kiss_host", "kiss_port", "auto_launch"]:
                if sk in d["soundmodem"]:
                    config["soundmodem"][sk] = d["soundmodem"][sk]
        if "pat" in d:
            for pk2 in ["enabled", "exe_path", "http_addr", "auto_launch", "poll_interval", "home_tactical", "traveler_tactical", "position_reports", "rf_fallback", "rf_poll_interval", "rf_gateway", "varafm_addr", "varafm_exe_path"]:
                if pk2 in d["pat"]:
                    config["pat"][pk2] = d["pat"][pk2]
        if "beacon" in d:
            for bk in ["enabled", "lat", "lon", "symbol_table", "symbol_code", "comment", "interval_minutes", "via_aprsis", "via_rf"]:
                if bk in d["beacon"]:
                    config["beacon"][bk] = d["beacon"][bk]
        if "relay" in d:
            for rk in ["enabled", "auto_retrieve", "auto_retrieve_delay_seconds",
                        "confirm_before_connect", "max_retries", "retry_delay_seconds",
                        "cooldown_seconds", "route_replies_via_relay", "ignore_stations", "min_snr"]:
                if rk in d["relay"]:
                    config["relay"][rk] = d["relay"][rk]
        if "updates" in d:
            for uk in ["auto_check", "interval_hours", "github_token"]:
                if uk in d["updates"]:
                    config["updates"][uk] = d["updates"][uk]
        save_config(config)
        new_db = config.get("varac_db_path", "")
        snap = copy.deepcopy(config)
    saved_at = datetime.now(timezone.utc).isoformat()
    with slock:
        state["config_saved_at"] = saved_at
    log.info("Settings saved to %s", CONFIG_PATH)
    warnings = validate_config(snap)
    if new_db != old_db and new_db and os.path.isfile(new_db):
        init_hwm()
    # Launch VarAC if configured
    with cfglock:
        varac_exe = config.get("varac_exe_path", "")
    if varac_exe:
        _launch_varac()
    # Start APRS if enabled
    with cfglock:
        aprs_on = config.get("aprs", {}).get("enabled", False)
    if aprs_on:
        start_aprs()
    with cfglock:
        kiss_on = config.get("soundmodem", {}).get("enabled", False)
    if kiss_on:
        _launch_soundmodem()
        start_kiss()
    with cfglock:
        pat_on = config.get("pat", {}).get("enabled", False)
    if pat_on:
        _launch_varafm()
        _launch_pat()
        start_pat()
    # Start beacon if enabled
    with cfglock:
        beacon_on = config.get("beacon", {}).get("enabled", False)
        relay_on = config.get("relay", {}).get("enabled", False)
    if beacon_on:
        start_beacon()
    # Start/restart relay thread if enabled
    if relay_on:
        start_relay()
    return jsonify({"ok": True, "path": CONFIG_PATH, "saved_at": saved_at, "warnings": warnings})


@app.route("/api/aprs_reply", methods=["POST"])
def api_aprs_reply():
    """Send an APRS message to the traveler."""
    d = request.get_json(force=True)
    msg = (d.get("message") or "").strip()
    if not msg:
        return jsonify({"ok": False, "error": "Message cannot be empty"})
    if len(msg) > 67:
        msg = msg[:67]
    to_call = (d.get("to_callsign") or "").strip().upper()
    if not to_call:
        to_call = _aprs_traveler_call()
    if not to_call:
        return jsonify({"ok": False, "error": "No destination callsign"})
    ok, err = aprs_send_message(to_call, msg)
    if ok:
        return jsonify({"ok": True, "to": to_call, "chars": len(msg)})
    return jsonify({"ok": False, "error": err})


@app.route("/api/aprs_passcode", methods=["POST"])
def api_gen_passcode():
    """Generate APRS-IS passcode from callsign."""
    d = request.get_json(force=True)
    call = (d.get("callsign") or "").strip().upper()
    if not call:
        return jsonify({"ok": False, "error": "No callsign"})
    pc = aprs_passcode(call)
    return jsonify({"ok": True, "passcode": str(pc)})


# ---------------------------------------------------------------------------
# Relay API endpoints
# ---------------------------------------------------------------------------
@app.route("/api/relay/status")
def api_relay_status():
    """Return relay tracking state for UI display."""
    with slock:
        return jsonify({
            "ok": True,
            "tracking": dict(state["relay_tracking"]),
            "queue_length": len(state["relay_retrieval_queue"]),
            "active": state["relay_retrieval_active"],
            "pending_confirm": state["relay_pending_confirm"],
            "relay_paths": dict(state["relay_paths"]),
        })


@app.route("/api/relay/approve", methods=["POST"])
def api_relay_approve():
    """Approve a pending relay retrieval (confirm_before_connect mode)."""
    d = request.get_json(force=True)
    relay_call = (d.get("relay_station") or "").strip().upper()
    if not relay_call:
        # If no specific station given, approve whatever is pending
        with slock:
            relay_call = state.get("relay_pending_confirm", "")
    if not relay_call:
        return jsonify({"ok": False, "error": "No relay pending confirmation"})

    now_utc = datetime.now(timezone.utc).isoformat()
    with slock:
        entry = state["relay_tracking"].get(relay_call)
        if not entry:
            return jsonify({"ok": False, "error": f"No tracking entry for {relay_call}"})
        if entry.get("status") != "confirmed_wait":
            return jsonify({"ok": False, "error": f"Relay {relay_call} not awaiting confirmation (status: {entry.get('status')})"})
        entry["status"] = "queued"
        state["relay_pending_confirm"] = None
        # Add to retrieval queue
        if relay_call not in [q.get("relay_station", "").upper() for q in state["relay_retrieval_queue"]]:
            state["relay_retrieval_queue"].append({
                "relay_station": entry["relay_station"],
                "frequency_mhz": entry.get("frequency_mhz", ""),
                "queued_at": now_utc,
            })
    log.info("Relay retrieval approved for %s", relay_call)
    # Ensure relay thread is running
    start_relay()
    return jsonify({"ok": True, "relay_station": relay_call})


@app.route("/api/relay/dismiss", methods=["POST"])
def api_relay_dismiss():
    """Dismiss a pending relay retrieval."""
    d = request.get_json(force=True)
    relay_call = (d.get("relay_station") or "").strip().upper()
    if not relay_call:
        with slock:
            relay_call = state.get("relay_pending_confirm", "")
    if not relay_call:
        return jsonify({"ok": False, "error": "No relay pending"})

    with slock:
        entry = state["relay_tracking"].get(relay_call)
        if entry:
            entry["status"] = "pending"
        if state.get("relay_pending_confirm") == relay_call:
            state["relay_pending_confirm"] = None
    log.info("Relay retrieval dismissed for %s", relay_call)
    return jsonify({"ok": True})


@app.route("/api/relay/retry", methods=["POST"])
def api_relay_retry():
    """Manually retry a failed relay retrieval."""
    d = request.get_json(force=True)
    relay_call = (d.get("relay_station") or "").strip().upper()
    if not relay_call:
        return jsonify({"ok": False, "error": "relay_station required"})

    now_utc = datetime.now(timezone.utc).isoformat()
    with slock:
        entry = state["relay_tracking"].get(relay_call)
        if not entry:
            return jsonify({"ok": False, "error": f"No tracking entry for {relay_call}"})
        entry["status"] = "queued"
        entry["attempts"] = 0
        entry["error"] = None
        # Clear cooldown so retry can proceed immediately
        state["relay_last_attempt"].pop(relay_call, None)
        if relay_call not in [q.get("relay_station", "").upper() for q in state["relay_retrieval_queue"]]:
            state["relay_retrieval_queue"].append({
                "relay_station": entry["relay_station"],
                "frequency_mhz": entry.get("frequency_mhz", ""),
                "queued_at": now_utc,
            })
    log.info("Relay retrieval retry queued for %s", relay_call)
    start_relay()
    return jsonify({"ok": True})


@app.route("/api/relay/retrieve", methods=["POST"])
def api_relay_retrieve():
    """Manually trigger retrieval from a specific relay station (one-shot)."""
    d = request.get_json(force=True)
    relay_call = (d.get("relay_station") or "").strip()
    if not relay_call:
        return jsonify({"ok": False, "error": "relay_station required"})

    relay_upper = relay_call.upper()
    now_utc = datetime.now(timezone.utc).isoformat()

    with slock:
        # Create/update tracking entry
        if relay_upper not in state["relay_tracking"]:
            state["relay_tracking"][relay_upper] = {
                "relay_station": relay_call,
                "frequency_mhz": d.get("frequency_mhz", ""),
                "first_seen": now_utc,
                "last_seen": now_utc,
                "notification_count": 0,
                "urgent": False,
                "status": "queued",
                "last_attempt": None,
                "attempts": 0,
                "error": None,
                "retrieved_vmail_ids": [],
            }
        else:
            state["relay_tracking"][relay_upper]["status"] = "queued"
            state["relay_tracking"][relay_upper]["attempts"] = 0
            state["relay_tracking"][relay_upper]["error"] = None

        # Clear cooldown and add to queue
        state["relay_last_attempt"].pop(relay_upper, None)
        if relay_upper not in [q.get("relay_station", "").upper() for q in state["relay_retrieval_queue"]]:
            state["relay_retrieval_queue"].append({
                "relay_station": relay_call,
                "frequency_mhz": d.get("frequency_mhz", ""),
                "queued_at": now_utc,
            })

    log.info("Manual relay retrieval queued for %s", relay_call)
    start_relay()
    return jsonify({"ok": True, "relay_station": relay_call})


@app.route("/api/relay/approve_from_phone")
def api_relay_approve_from_phone():
    """Approve relay retrieval from a Pushover notification link (GET, no CSRF)."""
    station = request.args.get("station", "").strip().upper()
    if not station:
        return "<h2>No relay station specified</h2>", 400
    safe_station = html.escape(station)
    now_utc = datetime.now(timezone.utc).isoformat()
    with slock:
        entry = state["relay_tracking"].get(station)
        if not entry:
            return f"<h2>No tracking entry for {safe_station}</h2>", 404
        if entry.get("status") != "confirmed_wait":
            return f"<h2>Relay {safe_station} is not awaiting confirmation (status: {html.escape(str(entry.get('status')))})</h2>", 400
        entry["status"] = "queued"
        state["relay_pending_confirm"] = None
        if station not in [q.get("relay_station", "").upper() for q in state["relay_retrieval_queue"]]:
            state["relay_retrieval_queue"].append({
                "relay_station": entry["relay_station"],
                "frequency_mhz": entry.get("frequency_mhz", ""),
                "queued_at": now_utc,
            })
    log.info("Relay retrieval approved from phone for %s", station)
    start_relay()
    return f"""<html><body style="font-family:sans-serif;text-align:center;padding:40px">
        <h2 style="color:#16a34a">Relay Retrieval Approved</h2>
        <p>HamLink will now connect to <b>{safe_station}</b> to retrieve your VMail.</p>
        </body></html>"""


@app.route("/api/test_db", methods=["POST"])
def api_test_db():
    d = request.get_json(force=True)
    p = d.get("path", "")
    if not p or not os.path.isfile(p):
        return jsonify({"ok": False, "error": "File not found"})
    try:
        c = sqlite3.connect(p, timeout=15)
        cur = c.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in cur.fetchall()}
        ok = {"vmail", "vmail_relay_notification"}.issubset(tables)
        cur.execute("SELECT COUNT(*) FROM vmail")
        vc = cur.fetchone()[0]
        c.close()
        if ok:
            return jsonify({"ok": True, "vmail_count": vc})
        return jsonify({"ok": False, "error": "Not a valid VarAC database"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/bbs_status")
def api_bbs_status():
    bbs_dir = get_bbs_directory()
    if not bbs_dir:
        return jsonify({"ok": False, "error": "BBS directory not configured", "path": None})
    exists = os.path.isdir(bbs_dir)
    files = []
    if exists:
        try:
            files = sorted(os.listdir(bbs_dir), reverse=True)[:20]
        except Exception:
            pass
    return jsonify({"ok": exists, "path": bbs_dir, "files": files,
                    "error": None if exists else f"Directory not found: {bbs_dir}"})


def _next_sitrep_number():
    """Derive next sitrep number from existing files in the BBS directory."""
    bbs_dir = get_bbs_directory()
    if not bbs_dir or not os.path.isdir(bbs_dir):
        return 1
    max_num = 0
    for f in os.listdir(bbs_dir):
        m = re.match(r'(?:\$\$)?SITREP_(\d+)', f)
        if m:
            max_num = max(max_num, int(m.group(1)))
    return max_num + 1


@app.route("/api/sitrep", methods=["POST"])
def api_sitrep():
    bbs_dir = get_bbs_directory()
    if not bbs_dir or not os.path.isdir(bbs_dir):
        return jsonify({"ok": False, "error": "BBS directory not available"})
    d = request.get_json(force=True)
    with cfglock:
        callsign = config.get("home_callsign", "N0CALL")
    num = _next_sitrep_number()
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%d %H:%M UTC")
    ts_file = now.strftime("%Y-%m-%d_%H%MZ")

    house = d.get("house", "All OK")
    vehicles = d.get("vehicles", "All OK")
    utilities = d.get("utilities", "All OK")
    health = d.get("health", "All OK")
    needs = d.get("needs", "None")
    relocation = d.get("relocation", "No change")
    location = d.get("location", "")
    remarks = d.get("remarks", "")

    content = (
        f"========================================\n"
        f"SITREP #{num:03d} — {callsign} Home Station\n"
        f"{ts}\n"
        f"========================================\n"
        f"\n"
        f"HOUSE:      {house}\n"
        f"VEHICLES:   {vehicles}\n"
        f"UTILITIES:  {utilities}\n"
        f"HEALTH:     {health}\n"
        f"NEEDS:      {needs}\n"
        f"RELOCATION: {relocation}\n"
        f"LOCATION:   {location}\n"
        f"REMARKS:    {remarks}\n"
        f"\n"
        f"--- End SITREP #{num:03d} ---\n"
    )

    # Remove previous $$SITREP_ file (superseded)
    for f in os.listdir(bbs_dir):
        if f.startswith("$$SITREP_"):
            try:
                old = os.path.join(bbs_dir, f)
                new_name = f[2:]  # strip $$ prefix
                os.rename(old, os.path.join(bbs_dir, new_name))
            except Exception as e:
                log.warning("Failed to rename old sitrep %s: %s", f, e)

    # Write new sitrep with $$ prefix so it sorts to top
    filename = f"$$SITREP_{num:03d}_{ts_file}.txt"
    filepath = os.path.join(bbs_dir, filename)
    try:
        with open(filepath, "w", encoding="utf-8") as fh:
            fh.write(content)
        log.info("Sitrep #%03d saved to %s", num, filepath)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

    # Send VarAC broadcast via UI automation
    varac_broadcast_sent = False
    varac_broadcast_msg = ""
    qsy = get_varac_next_qsy()
    varac_parts = [f"SITREP#{num:03d} on BBS"]
    if qsy:
        varac_parts.append(f"QSY {qsy[0]}Z {qsy[1]}")
    varac_parts.append("pls connect & relay")
    varac_broadcast_msg = " ".join(varac_parts)[:81]
    try:
        ok, err = varac_send_broadcast(varac_broadcast_msg, to="ALL")
        varac_broadcast_sent = ok
        if not ok:
            log.warning("VarAC broadcast failed: %s", err)
    except Exception as e:
        log.warning("VarAC broadcast error: %s", e)

    return jsonify({"ok": True, "number": num, "filename": filename, "path": filepath,
                    "varac_broadcast_sent": varac_broadcast_sent,
                    "varac_broadcast_msg": varac_broadcast_msg})


@app.route("/api/sitrep/latest")
def api_sitrep_latest():
    """Return the latest sitrep content for pre-filling the form."""
    bbs_dir = get_bbs_directory()
    if not bbs_dir or not os.path.isdir(bbs_dir):
        return jsonify({"ok": False})
    sitreps = []
    for f in os.listdir(bbs_dir):
        m = re.match(r'(?:\$\$)?SITREP_(\d+)', f)
        if m:
            sitreps.append((int(m.group(1)), f))
    if not sitreps:
        return jsonify({"ok": True, "number": 0, "fields": None})
    sitreps.sort(reverse=True)
    latest_file = os.path.join(bbs_dir, sitreps[0][1])
    fields = {}
    try:
        with open(latest_file, "r", encoding="utf-8") as fh:
            for line in fh:
                for key in ["HOUSE", "VEHICLES", "UTILITIES", "HEALTH", "NEEDS", "RELOCATION", "LOCATION", "REMARKS"]:
                    if line.startswith(f"{key}:"):
                        fields[key.lower()] = line.split(":", 1)[1].strip()
    except Exception:
        pass
    return jsonify({"ok": True, "number": sitreps[0][0], "fields": fields})


@app.route("/api/test_pushover", methods=["POST"])
def api_test_pushover():
    d = request.get_json(force=True)
    uk, at = d.get("user_key", ""), d.get("api_token", "")
    if not uk or not at:
        return jsonify({"ok": False, "error": "User key and API token required"})
    try:
        p = {"token": at, "user": uk, "title": "HamLink Radio",
             "message": "Notifications are working!", "priority": 0, "sound": "pushover"}
        data = urllib.parse.urlencode(p).encode()
        r = urllib.request.Request("https://api.pushover.net/1/messages.json", data=data)
        with urllib.request.urlopen(r, timeout=10):
            pass
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """Gracefully stop HamLink and all associated programs."""
    log.info("Shutdown requested via web UI")
    def _do_shutdown():
        time.sleep(1)  # Give Flask time to send the response
        _cleanup()
        os._exit(0)
    threading.Thread(target=_do_shutdown, daemon=True).start()
    return jsonify({"ok": True, "message": "HamLink is shutting down..."})


@app.route("/api/browse_path", methods=["POST"])
def api_browse():
    d = request.get_json(force=True)
    p = d.get("path", "")
    if not p:
        if sys.platform == "win32":
            drives = [f"{x}:\\" for x in string.ascii_uppercase if os.path.exists(f"{x}:\\")]
        else:
            drives = ["/home", "/Users", "/tmp", "/"]
        return jsonify({"ok": True, "entries": [{"name": x, "is_dir": True, "path": x} for x in drives]})

    # Resolve to real path to prevent symlink traversal
    real_p = os.path.realpath(p)

    # Block sensitive directories
    blocked = ["/etc", "/proc", "/sys", "/dev", "/boot", "/root",
               "/var/log", "/var/run", "/private/var"]
    if sys.platform == "win32":
        blocked = [os.path.join(os.environ.get("SYSTEMROOT", "C:\\Windows"), x)
                   for x in ["System32", "SysWOW64", "security"]]
    for b in blocked:
        if real_p.lower().startswith(b.lower()):
            return jsonify({"ok": False, "error": "Access restricted"})

    if not os.path.isdir(real_p):
        return jsonify({"ok": False, "error": "Not a directory"})
    try:
        entries = []
        parent = os.path.dirname(real_p.rstrip(os.sep))
        if parent and parent != real_p:
            entries.append({"name": "..", "is_dir": True, "path": parent})
        for n in sorted(os.listdir(real_p)):
            if n.startswith("."):
                continue  # Skip hidden files/dirs
            fp = os.path.join(real_p, n)
            isd = os.path.isdir(fp)
            if isd or n.lower().endswith((".db", ".exe")):
                entries.append({"name": n, "is_dir": isd, "path": fp})
        return jsonify({"ok": True, "entries": entries, "current": real_p})
    except PermissionError:
        return jsonify({"ok": False, "error": "Permission denied"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/message_log")
def api_message_log():
    """Return the persistent message log (most recent first)."""
    try:
        if not os.path.isfile(LOG_FILE):
            return jsonify({"ok": True, "entries": []})
        entries = []
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                entries.append(dict(row))
        entries.reverse()  # newest first
        return jsonify({"ok": True, "entries": entries[:200]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ---------------------------------------------------------------------------
# HTML — Family-friendly single-page app
# ---------------------------------------------------------------------------
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>HamLink Radio</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%2316a34a'/%3E%3Ccircle cx='32' cy='36' r='5' fill='%23fff'/%3E%3Cpath d='M20 24a17 17 0 0 1 24 0M14 18a25 25 0 0 1 36 0' fill='none' stroke='%23fff' stroke-width='4' stroke-linecap='round'/%3E%3Cpath d='M32 41v12' stroke='%23fff' stroke-width='4' stroke-linecap='round'/%3E%3C/svg%3E">
<link rel="stylesheet" href="/static/leaflet.css">
<script src="/static/leaflet.js"></script>
<style>
/* ---------------------------------------------------------------------
   Theme tokens. Light is the default; dark is applied either by explicit
   choice (html[data-theme=dark]) or by the OS preference when no choice
   has been made. Every colour used below comes from these tokens.
   ------------------------------------------------------------------ */
:root{
  --bg:#f3f5f8;--bg2:#e9edf2;--surface:#ffffff;--surface2:#f6f8fa;--surface3:#eef1f5;
  --border:#dde3ea;--border2:#c9d2dc;
  --text:#0f172a;--text2:#475569;--text3:#8595a8;
  --accent:#2563eb;--accent-h:#1d4ed8;--accent-soft:#e6efff;
  --green:#16a34a;--green-h:#15803d;--green-soft:#e6f6ec;
  --red:#dc2626;--red-h:#b91c1c;--red-soft:#fdecec;
  --amber:#d97706;--amber-soft:#fff4e0;
  --violet:#7c3aed;--violet-soft:#f1ebff;
  --orange:#ea580c;--orange-soft:#fff0e6;
  --shadow:0 1px 2px rgba(15,23,42,.06),0 1px 3px rgba(15,23,42,.04);
  --shadow-lg:0 12px 32px rgba(15,23,42,.14);
  --radius:14px;--radius-sm:10px;
  --font:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --mono:ui-monospace,"Cascadia Mono",Consolas,Menlo,monospace;
}
html[data-theme=dark]{
  --bg:#0b1220;--bg2:#0f172a;--surface:#131c2e;--surface2:#182338;--surface3:#1f2b42;
  --border:#243247;--border2:#2f3f58;
  --text:#e5ecf6;--text2:#a7b4c6;--text3:#6b7a90;
  --accent:#60a5fa;--accent-h:#93c5fd;--accent-soft:#1a2a4a;
  --green:#34d399;--green-h:#6ee7b7;--green-soft:#0f2e24;
  --red:#f87171;--red-h:#fca5a5;--red-soft:#3a1717;
  --amber:#fbbf24;--amber-soft:#3a2a0d;
  --violet:#a78bfa;--violet-soft:#26204a;
  --orange:#fb923c;--orange-soft:#3a2312;
  --shadow:0 1px 2px rgba(0,0,0,.4);
  --shadow-lg:0 12px 32px rgba(0,0,0,.5);
}
@media (prefers-color-scheme:dark){
  html:not([data-theme=light]){
    --bg:#0b1220;--bg2:#0f172a;--surface:#131c2e;--surface2:#182338;--surface3:#1f2b42;
    --border:#243247;--border2:#2f3f58;
    --text:#e5ecf6;--text2:#a7b4c6;--text3:#6b7a90;
    --accent:#60a5fa;--accent-h:#93c5fd;--accent-soft:#1a2a4a;
    --green:#34d399;--green-h:#6ee7b7;--green-soft:#0f2e24;
    --red:#f87171;--red-h:#fca5a5;--red-soft:#3a1717;
    --amber:#fbbf24;--amber-soft:#3a2a0d;
    --violet:#a78bfa;--violet-soft:#26204a;
    --orange:#fb923c;--orange-soft:#3a2312;
    --shadow:0 1px 2px rgba(0,0,0,.4);
    --shadow-lg:0 12px 32px rgba(0,0,0,.5);
  }
}

*{margin:0;padding:0;box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{font-family:var(--font);background:var(--bg);color:var(--text);min-height:100vh;min-height:100dvh;
  -webkit-font-smoothing:antialiased;line-height:1.45}
button,input,select,textarea{font:inherit;color:inherit}
button{cursor:pointer}
a{color:var(--accent)}
.hidden{display:none!important}
.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

/* ---------- Buttons ---------- */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;border:1px solid var(--border2);
  background:var(--surface);color:var(--text);font-weight:600;font-size:14px;padding:9px 16px;
  border-radius:var(--radius-sm);transition:background .15s,border-color .15s,transform .1s,box-shadow .15s;
  white-space:nowrap;line-height:1.2}
.btn:hover{background:var(--surface2);border-color:var(--accent)}
.btn:active{transform:translateY(1px)}
.btn:disabled{opacity:.5;cursor:not-allowed;transform:none}
.btn.sm{font-size:13px;padding:7px 12px}
.btn.xs{font-size:12px;padding:5px 10px;border-radius:8px}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.primary:hover{background:var(--accent-h);border-color:var(--accent-h)}
.btn.green{background:var(--green);border-color:var(--green);color:#fff}
.btn.green:hover{background:var(--green-h);border-color:var(--green-h)}
.btn.danger{background:var(--red-soft);border-color:transparent;color:var(--red)}
.btn.danger:hover{border-color:var(--red)}
.btn.ghost{background:transparent;border-color:transparent;color:var(--text2)}
.btn.ghost:hover{background:var(--surface2);border-color:var(--border)}
.btn.big{width:100%;font-size:16px;padding:15px 20px;border-radius:12px}
.btn.icon{width:38px;height:38px;padding:0;border-radius:10px;font-size:17px}

/* ---------- Header ---------- */
.header{position:sticky;top:0;z-index:100;background:var(--surface);border-bottom:1px solid var(--border);
  box-shadow:var(--shadow)}
.header-inner{max-width:720px;margin:0 auto;padding:12px 16px;display:flex;align-items:center;gap:12px}
.brand{display:flex;align-items:center;gap:12px;min-width:0;flex:1}
.brand-mark{width:42px;height:42px;flex-shrink:0;border-radius:12px;background:var(--green);
  display:flex;align-items:center;justify-content:center;box-shadow:0 4px 12px rgba(22,163,74,.28)}
.brand-mark svg{width:26px;height:26px}
.brand h1{font-size:17px;font-weight:800;letter-spacing:-.01em;line-height:1.15}
.brand .sub{font-size:12px;color:var(--text3);font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.header-actions{display:flex;gap:8px}
.demo-tag{font-size:10px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;background:var(--amber-soft);
  color:var(--amber);padding:3px 8px;border-radius:999px;margin-left:8px;vertical-align:middle}

/* ---------- Tabs ---------- */
.tabs{max-width:720px;margin:0 auto;padding:10px 16px 0;display:flex;gap:4px}
.tab{flex:1;background:none;border:none;border-bottom:2px solid transparent;padding:10px 6px;font-size:14px;
  font-weight:600;color:var(--text3);transition:color .15s,border-color .15s}
.tab:hover{color:var(--text2)}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}

/* ---------- Layout ---------- */
.main{max-width:720px;margin:0 auto;padding:16px 16px 40px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow)}
.section-label{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text3);
  margin:26px 0 12px;display:flex;align-items:center;gap:10px}
.section-label::after{content:'';flex:1;height:1px;background:var(--border)}
.section-label .count{background:var(--surface3);color:var(--text2);border-radius:999px;padding:1px 8px;font-size:11px}
.empty{text-align:center;padding:36px 20px;color:var(--text3);border:1px dashed var(--border2);border-radius:var(--radius)}
.empty .icon{font-size:32px;margin-bottom:6px;opacity:.6}
.empty p{font-size:13px}
.error-bar{background:var(--red-soft);border:1px solid var(--red);color:var(--red);border-radius:var(--radius-sm);
  padding:11px 14px;margin-bottom:12px;font-size:13px;display:none}

/* ---------- Hero status ---------- */
.hero{padding:26px 22px 20px;text-align:center;transition:background .3s,border-color .3s;margin-bottom:12px}
.hero-icon{font-size:52px;line-height:1;margin-bottom:10px}
.hero-title{font-size:23px;font-weight:800;letter-spacing:-.01em;margin-bottom:4px}
.hero-sub{font-size:14px;color:var(--text2)}
.hero.ok{background:var(--green-soft);border-color:var(--green)}
.hero.ok .hero-title{color:var(--green)}
.hero.alert{background:var(--red-soft);border-color:var(--red);animation:pulse-border 2s ease-in-out infinite}
.hero.alert .hero-title{color:var(--red)}
@keyframes pulse-border{0%,100%{box-shadow:0 0 0 0 rgba(220,38,38,.25)}50%{box-shadow:0 0 0 12px rgba(220,38,38,0)}}
.hero-actions{display:flex;gap:10px;justify-content:center;flex-wrap:wrap;margin-top:16px}
.hero-actions .btn{font-size:15px;padding:12px 22px}
.hero-actions .btn.danger{background:var(--red);color:#fff;border-color:var(--red)}
.hero-actions .btn.danger:hover{background:var(--red-h);border-color:var(--red-h)}
.pills{display:flex;flex-wrap:wrap;gap:6px;justify-content:center;margin-top:18px}
.pill{display:inline-flex;align-items:center;gap:7px;font-size:12px;font-weight:600;color:var(--text2);
  background:var(--surface);border:1px solid var(--border);border-radius:999px;padding:5px 11px 5px 9px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--text3);flex-shrink:0}
.dot.ok{background:var(--green);box-shadow:0 0 0 3px rgba(22,163,74,.18);animation:blink 2.4s infinite}
.dot.err{background:var(--red);box-shadow:0 0 0 3px rgba(220,38,38,.18)}
.dot.warn{background:var(--amber);box-shadow:0 0 0 3px rgba(217,119,6,.18)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.45}}
.rf-countdown{margin:14px auto 0;display:inline-block;padding:7px 12px;background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius-sm);font-size:12px;color:var(--text2)}
.rf-countdown b{color:var(--text)}

/* ---------- Position card ---------- */
.loc{padding:14px 18px;margin-bottom:12px;display:flex;gap:12px;align-items:flex-start;transition:border-color .3s,box-shadow .3s}
.loc.new{border-color:var(--green);box-shadow:0 0 0 3px rgba(22,163,74,.2);animation:pulse-loc 2s ease-in-out infinite}
@keyframes pulse-loc{0%,100%{background:var(--surface)}50%{background:var(--green-soft)}}
.loc-icon{font-size:24px;line-height:1;padding-top:2px}
.loc-body{flex:1;min-width:0}
.loc-title{font-size:14px;font-weight:700;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.loc-time{font-size:12px;color:var(--text3)}
.loc-details{font-size:13px;color:var(--text2);margin-top:5px;word-break:break-word}
.loc-links{display:flex;gap:12px;margin-top:8px;font-size:12px;font-weight:600}
.loc-links a{text-decoration:none}
.badge-new{background:var(--green);color:#fff;font-size:10px;font-weight:800;padding:2px 8px;border-radius:999px;
  letter-spacing:.06em;animation:pulse-badge 1.5s ease-in-out infinite}
@keyframes pulse-badge{0%,100%{opacity:1}50%{opacity:.5}}

/* ---------- Action buttons ---------- */
.actions{display:grid;gap:8px;margin-top:4px}

/* ---------- Message cards ---------- */
.msg{padding:16px 18px;margin-bottom:10px;border-left:4px solid var(--border2);transition:background .2s,border-color .2s}
.msg.ch-vmail{border-left-color:var(--violet)}
.msg.ch-aprs{border-left-color:var(--accent)}
.msg.ch-winlink{border-left-color:var(--amber)}
.msg.ch-relay{border-left-color:var(--orange)}
.msg.ch-sent{border-left-color:var(--border2);background:var(--surface2)}
.msg.ch-sent.delivered{border-left-color:var(--green)}
.msg.ch-system{border-left-color:var(--text3);opacity:.85}
.msg.unread{background:var(--green-soft)}
.msg.unread.urgent{background:var(--red-soft);border-left-color:var(--red)}
.msg-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:6px}
.msg-from{font-weight:700;font-size:15px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-width:0}
.msg-time{font-size:12px;color:var(--text3);font-weight:500;white-space:nowrap;padding-top:2px}
.msg-subject{font-size:14px;font-weight:600;margin-bottom:4px}
.msg-body{font-size:15px;color:var(--text2);line-height:1.55;white-space:pre-wrap;word-wrap:break-word}
.msg-meta{font-size:11px;color:var(--text3);margin-top:6px}
.msg-status{font-size:12px;margin-top:8px;font-weight:600}
.msg-status.ok{color:var(--green)}
.msg-status.warn{color:var(--amber)}
.msg-status.err{color:var(--red)}
.msg-status.info{color:var(--accent)}
.msg-actions{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}
.chip{display:inline-flex;align-items:center;font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;
  padding:3px 8px;border-radius:6px;line-height:1.2}
.chip.vmail{background:var(--violet-soft);color:var(--violet)}
.chip.aprs{background:var(--accent-soft);color:var(--accent)}
.chip.winlink{background:var(--amber-soft);color:var(--amber)}
.chip.relay{background:var(--orange-soft);color:var(--orange)}
.chip.sent{background:var(--surface3);color:var(--text2)}
.chip.new{background:var(--green);color:#fff}
.chip.urgent{background:var(--red);color:#fff}
.chip.sys{background:var(--surface3);color:var(--text3)}

/* ---------- Compose ---------- */
.compose{padding:18px;margin:12px 0 16px;border-color:var(--accent)}
.compose-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.compose-head h3{font-size:16px;font-weight:700}
.quick{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
.quick button{background:var(--accent-soft);color:var(--accent);border:1px solid transparent;font-size:13px;font-weight:600;
  padding:7px 13px;border-radius:999px;transition:background .15s,color .15s}
.quick button:hover{background:var(--accent);color:#fff}
.textarea{width:100%;border:1px solid var(--border2);border-radius:var(--radius-sm);padding:12px;font-size:15px;
  background:var(--surface);resize:vertical;min-height:84px;outline:none;transition:border-color .15s}
.textarea:focus{border-color:var(--accent)}
.compose-foot{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-top:10px;flex-wrap:wrap}
.compose-status{font-size:12px;color:var(--text3);flex:1;min-width:0}
.counter{font-size:11px;color:var(--text3);margin-top:4px;text-align:right}
.counter.over{color:var(--red);font-weight:600}
.channels{display:flex;gap:6px 16px;flex-wrap:wrap;margin-top:12px;font-size:13px}
.channels label{display:inline-flex;align-items:center;gap:6px;cursor:pointer}
.channels input{accent-color:var(--accent);width:16px;height:16px}
.channels .path{color:var(--text3);font-size:12px}
.channels .path.rf{color:var(--amber);font-weight:600}
.notice{border-radius:var(--radius-sm);padding:10px 12px;font-size:12.5px;line-height:1.5;margin-top:10px}
.notice.warn{background:var(--amber-soft);border:1px solid var(--amber);color:var(--text)}
.notice.info{background:var(--violet-soft);border:1px solid var(--violet);color:var(--text)}
.notice.danger{background:var(--red-soft);border:1px solid var(--red);color:var(--text)}
.notice.blue{background:var(--accent-soft);border:1px solid var(--accent);color:var(--text)}

/* ---------- Modal ---------- */
.modal{position:fixed;inset:0;background:rgba(2,6,23,.55);z-index:400;display:none;align-items:center;justify-content:center;
  padding:20px;backdrop-filter:blur(3px)}
.modal.open{display:flex}
.modal-box{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:24px;max-width:500px;width:100%;
  box-shadow:var(--shadow-lg);max-height:90vh;overflow-y:auto}
.modal-box h3{font-size:18px;margin-bottom:10px}
.modal-box p{font-size:14px;color:var(--text2);line-height:1.5}
.modal-actions{display:flex;flex-direction:column;gap:8px;margin-top:14px}
.modal-actions .btn{white-space:normal;text-align:left;justify-content:flex-start}

/* ---------- Toast ---------- */
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(16px);background:var(--text);color:var(--bg);
  font-weight:600;font-size:14px;padding:11px 20px;border-radius:12px;z-index:900;opacity:0;transition:opacity .25s,transform .25s;
  pointer-events:none;box-shadow:var(--shadow-lg);max-width:calc(100vw - 32px);text-align:center}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.toast.err{background:var(--red);color:#fff}

/* ---------- Full-screen gates (compliance + start) ---------- */
.gate{position:fixed;inset:0;background:var(--bg);display:flex;align-items:center;justify-content:center;z-index:10000;overflow-y:auto;padding:20px}
.gate-inner{max-width:620px;width:100%}
.gate h2{font-size:24px;font-weight:800;text-align:center}
.gate .sub{font-size:13px;color:var(--text3);text-align:center;margin:4px 0 18px}
.gate-body{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:22px;max-height:52vh;
  overflow-y:auto;font-size:13.5px;line-height:1.7;color:var(--text2);margin-bottom:18px}
.gate-body h3{font-size:14px;font-weight:700;color:var(--text);margin:18px 0 8px;padding-top:12px;border-top:1px solid var(--border)}
.gate-body h3:first-child{margin-top:0;padding-top:0;border-top:none}
.gate-body strong{color:var(--text)}
.gate-body .ref{font-size:12px;color:var(--text3);font-style:italic}
.check{display:flex;align-items:flex-start;gap:12px;margin-bottom:12px;font-size:13.5px;cursor:pointer;user-select:none;
  padding:10px 12px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--surface)}
.check.on{border-color:var(--green);background:var(--green-soft)}
.check .box{width:20px;height:20px;border-radius:6px;border:2px solid var(--border2);flex-shrink:0;margin-top:1px;
  display:flex;align-items:center;justify-content:center;font-size:13px;color:#fff;background:var(--surface)}
.check.on .box{background:var(--green);border-color:var(--green)}
.splash{text-align:center;padding:30px 20px}
.splash .brand-mark{width:76px;height:76px;border-radius:22px;margin:0 auto 18px}
.splash .brand-mark svg{width:46px;height:46px}
.splash p{font-size:15px;color:var(--text2);margin:10px auto 26px;max-width:420px;line-height:1.55}

/* ---------- Drawers (settings + sitrep) ---------- */
.drawer-bg{position:fixed;inset:0;background:rgba(2,6,23,.45);z-index:200;display:none;backdrop-filter:blur(3px)}
.drawer-bg.open{display:block}
.drawer{position:fixed;top:0;right:0;bottom:0;width:480px;max-width:100vw;background:var(--bg);z-index:201;
  display:flex;flex-direction:column;transform:translateX(100%);transition:transform .28s ease;box-shadow:var(--shadow-lg)}
.drawer-bg.open .drawer{transform:translateX(0)}
.drawer-head{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;
  background:var(--surface);flex-shrink:0}
.drawer-head h2{font-size:18px;font-weight:800}
.drawer-body{padding:16px 20px;overflow-y:auto;flex:1}
.drawer-foot{padding:12px 20px;border-top:1px solid var(--border);background:var(--surface);flex-shrink:0;display:flex;gap:10px;align-items:center}

/* ---------- Settings sections ---------- */
.sec{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);margin-bottom:12px;box-shadow:var(--shadow)}
.sec>summary{list-style:none;cursor:pointer;padding:14px 18px;display:flex;align-items:center;gap:10px;font-weight:700;font-size:14px;
  color:var(--text);user-select:none}
.sec>summary::-webkit-details-marker{display:none}
.sec>summary::before{content:'';width:7px;height:7px;border-right:2px solid var(--text3);border-bottom:2px solid var(--text3);
  transform:rotate(-45deg);transition:transform .15s;margin-right:2px;flex-shrink:0}
.sec[open]>summary::before{transform:rotate(45deg)}
.sec>summary .state{margin-left:auto;font-size:10px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;
  padding:3px 8px;border-radius:999px;background:var(--surface3);color:var(--text3)}
.sec>summary .state.on{background:var(--green-soft);color:var(--green)}
.sec-body{padding:2px 18px 18px}
.fg{margin-bottom:14px}
.fg:last-child{margin-bottom:0}
.fl{display:block;font-size:12px;font-weight:600;color:var(--text2);margin-bottom:5px}
.fi,.fsel{width:100%;border:1px solid var(--border2);border-radius:8px;padding:9px 12px;font-size:14px;background:var(--surface2);
  outline:none;transition:border-color .15s}
.fi:focus,.fsel:focus{border-color:var(--accent);background:var(--surface)}
.fi.w-xs{width:110px}.fi.w-sm{width:160px}.fi.w-md{width:220px}
.fhint{font-size:11.5px;color:var(--text3);margin-top:4px;line-height:1.45}
.frow{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.frow .fi{flex:1;min-width:140px}
.subhead{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);margin:18px 0 10px;
  padding-top:14px;border-top:1px solid var(--border)}
.tgl-row{display:flex;align-items:center;justify-content:space-between;gap:12px}
.tgl-row .fl{margin:0;font-size:13px;color:var(--text)}
.tgl{width:44px;height:24px;background:var(--border2);border-radius:12px;position:relative;cursor:pointer;transition:background .2s;flex-shrink:0}
.tgl.on{background:var(--green)}
.tgl::after{content:'';position:absolute;top:2px;left:2px;width:20px;height:20px;background:#fff;border-radius:50%;
  transition:transform .2s;box-shadow:0 1px 3px rgba(0,0,0,.25)}
.tgl.on::after{transform:translateX(20px)}
.tgl.disabled{opacity:.4;pointer-events:none}
.fb{background:var(--surface2);border:1px solid var(--border);border-radius:8px;max-height:200px;overflow-y:auto;margin-top:6px;display:none}
.fb.open{display:block}
.fe{padding:8px 12px;font-size:13px;cursor:pointer;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border)}
.fe:last-child{border-bottom:none}
.fe:hover{background:var(--surface3)}
.fe.dir{color:var(--accent);font-weight:600}
.fe.file{color:var(--orange);font-weight:600}
.fc{padding:5px 10px;font-size:11px;color:var(--text3);background:var(--surface3);border-bottom:1px solid var(--border);
  font-family:var(--mono);word-break:break-all}
.tr{margin-top:6px;padding:9px 12px;border-radius:8px;font-size:12px;display:none}
.tr.ok{display:block;background:var(--green-soft);color:var(--green)}
.tr.err{display:block;background:var(--red-soft);color:var(--red)}
input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:6px;background:var(--border2);border-radius:3px;outline:none}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:20px;height:20px;background:var(--accent);border-radius:50%;cursor:pointer}
.gw a{display:block;margin:3px 0;text-decoration:none;font-weight:600}

/* ---------- Sitrep ---------- */
.sr-field{margin-bottom:12px}
.sr-field label{display:block;font-size:13px;font-weight:700;margin-bottom:4px}
.sr-hint{font-size:11.5px;color:var(--text3);margin-top:3px}
.sr-result{margin-top:12px;padding:10px 12px;border-radius:8px;font-size:13px;font-weight:600;display:none;white-space:pre-line}
.sr-result.ok{display:block;background:var(--green-soft);color:var(--green)}
.sr-result.err{display:block;background:var(--red-soft);color:var(--red)}

/* ---------- Map ---------- */
#mapContainer{height:520px;border-radius:var(--radius);overflow:hidden;border:1px solid var(--border);display:none}
.map-info{margin-top:12px;padding:14px 16px;display:none}
.map-tools{display:flex;gap:8px;margin-top:10px}

/* ---------- Update banner ---------- */
.update-bar{background:var(--accent-soft);border-bottom:1px solid var(--accent)}
.update-inner{max-width:720px;margin:0 auto;padding:10px 16px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:13.5px}
.update-inner b{color:var(--text)}
.update-inner .spacer{flex:1}
.update-notes{white-space:pre-wrap;font-size:13px;line-height:1.5;background:var(--surface2);border:1px solid var(--border);
  border-radius:var(--radius-sm);padding:12px;max-height:240px;overflow-y:auto;margin:10px 0}
.progress{height:8px;background:var(--surface3);border-radius:4px;overflow:hidden;margin-top:10px}
.progress>i{display:block;height:100%;background:var(--accent);width:0;transition:width .3s}
.progress.indet>i{width:40%;animation:indet 1.2s ease-in-out infinite}
@keyframes indet{0%{margin-left:-40%}100%{margin-left:100%}}

/* ---------- Settings file + save confirmation ---------- */
.cfgfile{background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius-sm);padding:10px 12px;font-size:12.5px;margin-bottom:12px}
.cfgfile code{font-family:var(--mono);font-size:11.5px;word-break:break-all;color:var(--text2)}
.cfgfile-row{display:flex;align-items:center;gap:8px;margin-top:6px;color:var(--text3);font-size:11.5px}
.cfgfile-row .spacer{flex:1}
.saved-box{text-align:left}
.saved-box .path{font-family:var(--mono);font-size:12px;word-break:break-all;background:var(--surface2);padding:8px 10px;border-radius:8px;margin:8px 0}
.saved-box ul{margin:6px 0 0 18px;font-size:13px}
.saved-box li{margin:3px 0}
.saved-box li.error{color:var(--red)}
.saved-box li.warn{color:var(--amber)}
.saved-box li.info{color:var(--text2)}
.maplist{display:flex;flex-direction:column;gap:6px;margin:8px 0}
.mapitem{display:flex;align-items:center;gap:8px;padding:8px 10px;border:1px solid var(--border);border-radius:8px;font-size:13px;background:var(--surface2)}
.mapitem.active{border-color:var(--green);background:var(--green-soft)}
.mapitem .meta{color:var(--text3);font-size:11.5px}
.mapitem .grow{flex:1;min-width:0}

/* ---------- Footer ---------- */
.footer{text-align:center;margin-top:36px;color:var(--text3);font-size:11.5px}
.footer a{color:inherit}
.footer .btn{margin-bottom:6px}

@media (max-width:520px){
  .hero-title{font-size:20px}
  .hero-icon{font-size:44px}
  .drawer{width:100vw}
  .msg{padding:14px}
  .header-inner{padding:10px 12px}
}
</style>
</head>
<body>

<!-- ===================== REGULATORY COMPLIANCE GATE ===================== -->
<div class="gate" id="complianceScreen">
  <div class="gate-inner">
    <div style="text-align:center;font-size:40px;margin-bottom:8px">⚖️</div>
    <h2>Regulatory Notice</h2>
    <div class="sub">Please read and acknowledge before using this application.</div>
    <div class="gate-body">
      <h3>Amateur Radio License Required</h3>
      This application is designed for use under United States FCC Part 97 amateur radio regulations.
      Operation outside the United States requires the operator to verify compliance with their
      country's amateur radio regulations, which may differ significantly from FCC rules.
      <br><br>
      A valid FCC amateur radio license is required for the station operator. The station licensee
      is responsible for all transmissions made from the station, including those initiated through this app.

      <h3>Third-Party Communications</h3>
      When an unlicensed person (such as a family member) uses this app to send or reply to messages,
      this constitutes <strong>third-party communications</strong> under FCC Part 97.115.
      The following conditions must be met:

      <br><br><strong>1. A licensed control operator must be designated.</strong>
      The station licensee (the licensed ham) must serve as the control operator of the home station,
      even if operating remotely. The licensee is responsible for configuring the station and ensuring
      all transmissions comply with FCC rules.

      <br><br><strong>2. Data emission exception and bandwidth limit.</strong>
      FCC 97.115(c) states: "No station may transmit third party communications while being automatically
      controlled except a station transmitting a RTTY or data emission." All modes used by this
      application (VarAC/VARA, APRS, Winlink) are classified as data emissions, satisfying this exception.
      However, FCC 97.221 also requires that automatically controlled digital stations must not exceed
      500 Hz occupied bandwidth. <strong>Only VARA HF in 500 Hz mode meets both requirements.</strong>
      APRS via Soundmodem (1200-baud AFSK) and VARA FM exceed the 500 Hz bandwidth limit and
      <strong>must not</strong> be operated under automatic control — a licensed control operator must
      be present or supervising when using these modes. This application blocks non-compliant RF
      transmissions by default and requires the operator to confirm they are licensed or to invoke
      the emergency exception (97.403) before proceeding.

      <br><br><strong>3. Messages must be personal in nature.</strong>
      Communications must be limited to remarks of a personal character — family check-ins,
      welfare messages, and personal coordination. Commercial use is prohibited under Part 97.113.

      <br><br><strong>4. Domestic vs. international.</strong>
      Third-party traffic between US amateur stations is permitted without restriction.
      International third-party traffic requires a bilateral agreement between the US and
      the other country. Check the FCC/ARRL list of third-party agreement countries.

      <br><br><strong>5. Station identification.</strong>
      VarAC and APRS handle station identification automatically by transmitting the assigned
      callsign within their protocols, satisfying FCC 97.119 requirements.

      <h3>Emergency Communications</h3>
      FCC 97.403 states that no provision of the rules prevents the use of any means of
      radiocommunication to provide essential communications in connection with the
      immediate safety of human life and immediate protection of property when normal
      communication systems are not available. In a genuine emergency, these rules
      are secondary to the preservation of life and property.

      <h3>APRS-IS Considerations</h3>
      When APRS messaging is enabled, this app connects to the APRS-IS internet network.
      Replies sent via APRS-IS are transmitted over RF by independently operated iGate stations.
      The station licensee should be aware that APRS messages are publicly visible on the
      APRS network (e.g., aprs.fi).

      <h3>Disclaimer</h3>
      This application is provided as-is for amateur radio use. It is the responsibility
      of the station licensee to ensure compliance with all applicable FCC regulations.
      This notice is informational and does not constitute legal advice. For definitive
      guidance, consult FCC Part 97 directly or contact the ARRL.

      <br><br><span class="ref">References: 47 CFR §97.3, §97.109, §97.113, §97.115, §97.119, §97.221, §97.403</span>
    </div>

    <div class="check" data-check="1"><div class="box"></div>
      <span>I am (or am acting on behalf of) a licensed amateur radio operator who is the designated control operator of this station.</span></div>
    <div class="check" data-check="2"><div class="box"></div>
      <span>I understand the third-party communication rules described above and will ensure all use of this application complies with FCC Part 97.</span></div>
    <div class="check" data-check="3"><div class="box"></div>
      <span>I understand that the station licensee is responsible for all transmissions initiated through this application.</span></div>

    <button class="btn green big" id="btnAccept" onclick="acceptCompliance()" disabled>I Understand &amp; Accept</button>
  </div>
</div>

<!-- ===================== START SPLASH (unlocks audio) ===================== -->
<div class="gate hidden" id="splash">
  <div class="gate-inner splash">
    <div class="brand-mark"><svg viewBox="0 0 64 64" aria-hidden="true"><circle cx="32" cy="36" r="5" fill="#fff"/><path d="M20 24a17 17 0 0 1 24 0M14 18a25 25 0 0 1 36 0" fill="none" stroke="#fff" stroke-width="4" stroke-linecap="round"/><path d="M32 41v12" stroke="#fff" stroke-width="4" stroke-linecap="round"/></svg></div>
    <h2>HamLink Radio</h2>
    <p>This app watches for check-in messages from your loved one who is traveling. You'll be alerted when they check in, and you can reply and post family status reports.</p>
    <button class="btn green big" style="max-width:280px" onclick="start()">Start Monitoring</button>
    <div class="fhint" style="margin-top:12px">Tap Start to enable alert sounds in this browser.</div>
    <div class="hidden" id="splashAlarm" style="margin-top:18px">
      <div class="notice danger" style="display:inline-block;text-align:left">📨 <b id="splashAlarmText">A message is waiting and the PC alarm is sounding.</b><br>Tap Start to see it, or silence the alarm first.</div><br>
      <button class="btn danger" style="margin-top:10px" onclick="silenceFromSplash()" title="Stop the PC speaker alarm now">🔕 Silence alarm</button>
    </div>
  </div>
</div>

<div class="toast" id="toast" role="status" aria-live="polite"></div>

<!-- ===================== HEADER ===================== -->
<header class="header">
  <div class="header-inner">
    <div class="brand">
      <div class="brand-mark"><svg viewBox="0 0 64 64" aria-hidden="true"><circle cx="32" cy="36" r="5" fill="#fff"/><path d="M20 24a17 17 0 0 1 24 0M14 18a25 25 0 0 1 36 0" fill="none" stroke="#fff" stroke-width="4" stroke-linecap="round"/><path d="M32 41v12" stroke="#fff" stroke-width="4" stroke-linecap="round"/></svg></div>
      <div style="min-width:0"><h1>HamLink Radio<span class="demo-tag hidden" id="demoTag">Demo</span></h1><div class="sub" id="headerSub">Waiting for messages…</div></div>
    </div>
    <div class="header-actions">
      <button class="btn icon" id="btnTheme" onclick="toggleTheme()" title="Switch between light and dark theme" aria-label="Toggle theme">🌙</button>
      <button class="btn icon" onclick="openSett()" title="Settings: callsigns, channels, alerts, maps, updates" aria-label="Settings">⚙️</button>
    </div>
  </div>
  <nav class="tabs" aria-label="Views">
    <button class="tab active" id="tabDashboard" onclick="switchTab('dashboard')" title="Messages, status, and replies">Dashboard</button>
    <button class="tab" id="tabMap" onclick="switchTab('map')" title="The traveler's last position on a map that works without internet">Offline Map</button>
  </nav>
</header>

<!-- ===================== UPDATE BANNER ===================== -->
<div class="update-bar hidden" id="updateBar">
  <div class="update-inner">
    <span>🎉 <b id="updText">A new version of HamLink is available</b></span>
    <a id="updLink" href="#" target="_blank" rel="noopener">What's new</a>
    <span class="spacer"></span>
    <button class="btn xs primary" onclick="openUpdate()" title="Download and install the new version, then restart HamLink">Update now</button>
    <button class="btn xs ghost" onclick="skipUpdate()" title="Hide this banner for this version">Later</button>
  </div>
</div>

<!-- ===================== UPDATE MODAL ===================== -->
<div class="modal" id="updateModal" role="dialog" aria-modal="true" aria-labelledby="updTitle">
  <div class="modal-box">
    <h3 id="updTitle">Update HamLink</h3>
    <p id="updIntro"></p>
    <div class="update-notes" id="updNotes"></div>
    <p id="updHow" style="font-size:13px"></p>
    <div id="updProgressWrap" class="hidden">
      <div class="progress" id="updProgress"><i></i></div>
      <p id="updStatus" style="margin-top:8px;font-weight:600"></p>
    </div>
    <div class="modal-actions" id="updActions">
      <button class="btn primary" id="updGo" onclick="startUpdate()">⬇️ Download &amp; install</button>
      <button class="btn ghost" onclick="closeUpdate()">Cancel</button>
    </div>
  </div>
</div>

<!-- ===================== MAP TAB ===================== -->
<div class="main hidden" id="mapTab">
  <div class="empty" id="mapNoConfig">
    <div class="icon">🗺️</div>
    <p>No offline map configured.</p>
    <p style="margin-top:6px">Open Settings → Offline map and press <b>Download map</b> for the area around home. It works without internet afterwards.</p>
    <p style="margin-top:10px"><button class="btn sm primary" onclick="openSett()">Open Settings</button></p>
  </div>
  <div id="mapContainer"></div>
  <div class="card map-info" id="mapPosInfo">
    <div style="display:flex;align-items:center;gap:8px"><span style="font-size:18px">📍</span><b id="mapPosTitle"></b></div>
    <div class="loc-time" id="mapPosTime"></div>
    <div class="loc-details" id="mapPosDetails"></div>
    <div class="map-tools"><button class="btn sm" onclick="centerMap()">Center on position</button></div>
  </div>
</div>

<!-- ===================== DASHBOARD TAB ===================== -->
<main class="main" id="dashboardTab">
  <div class="error-bar" id="errBar"></div>

  <section class="card hero" id="statusCard">
    <div class="hero-icon" id="statusIcon">📻</div>
    <div class="hero-title" id="statusTitle">Waiting for check-in…</div>
    <div class="hero-sub" id="statusSub">The monitor is watching for messages.</div>
    <div class="hero-actions hidden" id="heroActions">
      <button class="btn danger" id="btnSilence" onclick="silenceAll()" title="Stop the sound on this PC and in this browser. Messages stay listed below so you can still reply.">🔕 Silence alarm</button>
      <button class="btn" id="btnCloseAll" onclick="closeAll()" title="Mark every new message as seen and move them to Previous Messages">✓ Mark all as read</button>
    </div>
    <div class="pills" id="pills">
      <span class="pill" id="pillInet" title="Internet connectivity, checked every poll. When it is down, APRS and Winlink can fall back to radio."><i class="dot" id="inetDot"></i><span id="inetText">Internet</span></span>
      <span class="pill" id="pillDb" title="Connection to the VarAC database (VarAC.db). Green = VMails are being monitored."><i class="dot" id="connDot"></i><span id="connText">VarAC</span></span>
      <span class="pill hidden" id="aprsConn" title="APRS-IS: the APRS internet network. Green = logged in and receiving."><i class="dot" id="aprsDot"></i><span id="aprsText">APRS</span></span>
      <span class="pill hidden" id="kissConn" title="APRS over radio through Soundmodem's KISS port."><i class="dot" id="kissDot"></i><span id="kissText">APRS RF</span></span>
      <span class="pill hidden" id="patConn" title="Winlink through the Pat client. Green = Pat is running and syncing."><i class="dot" id="patDot"></i><span id="patText">Winlink</span></span>
    </div>
    <div class="rf-countdown hidden" id="rfCountdown">Next RF Winlink sync: <b id="rfCountdownTime"></b></div>
  </section>

  <section class="card loc hidden" id="locCard">
    <div class="loc-icon">📍</div>
    <div class="loc-body">
      <div class="loc-title"><span id="locTitle">Last known position</span><span class="badge-new hidden" id="locNewBadge">NEW</span></div>
      <div class="loc-time" id="locTime"></div>
      <div class="loc-details" id="locDetails"></div>
      <div class="loc-links">
        <a id="locLink" href="#" target="_blank" rel="noopener">Google Maps ↗</a>
        <a href="#" onclick="switchTab('map');return false" id="locMapLink">Offline map</a>
      </div>
    </div>
    <button class="btn xs hidden" id="locAckBtn" onclick="ackPosition()" title="Acknowledge the new position and stop the highlight">✓ Seen</button>
  </section>

  <div class="actions">
    <button class="btn green big hidden" id="btnSendMsg" onclick="openCompose('multi')" title="Write a new message and send it on any of the available channels">✉️ Send Message</button>
    <button class="btn primary big hidden" id="btnSitrep" onclick="openSitrep()" title="Post a family status report to the VarAC BBS and announce it on the air">📋 Post Sitrep to BBS</button>
  </div>

  <div class="section-label">New Messages <span class="count hidden" id="activeCount"></span></div>
  <div id="activeArea"></div>

  <!-- Compose / reply box -->
  <section class="card compose hidden" id="replyBox">
    <div class="compose-head">
      <h3 id="composeTitle">✉️ Send Message</h3>
      <button class="btn xs ghost" onclick="closeReply()">✕ Cancel</button>
    </div>
    <div class="quick" id="quickBtns"></div>
    <textarea class="textarea" id="replyText" placeholder="Type your message…" oninput="onComposeInput()"></textarea>
    <div class="counter" id="charCounter"></div>
    <div class="channels">
      <label id="chkAprsLabel" title="Short message (67 characters) to the traveler's APRS address. Goes over the internet, or over radio when the internet is down."><input type="checkbox" id="chkAprs" checked> APRS <span class="path" id="aprsPathLabel">(internet)</span></label>
      <label id="chkWinlinkLabel" title="Email-style message to the traveler's Winlink tactical address."><input type="checkbox" id="chkWinlink" checked> Winlink <span class="path" id="wlPathLabel">(internet)</span></label>
      <label id="chkVaracLabel" title="Saved to the VarAC outbox and delivered the next time the traveler connects. Never transmits by itself."><input type="checkbox" id="chkVarac" checked> VarAC <span class="path" id="varacPathLabel">(queued to outbox)</span></label>
    </div>
    <div class="notice info hidden" id="relayViaIndicator"></div>
    <div class="notice warn hidden" id="rfWarning">
      <strong>RF transmission:</strong> one or more selected channels will transmit over radio.
      Only licensed amateur radio operators, or authorized third parties under direct supervision,
      may initiate RF transmissions (FCC Part 97). In an emergency involving immediate safety of life or
      property, any means of communication may be used (97.403).
      <br><br><strong>Note:</strong> VarAC does not transmit immediately — replies are saved to a local outbox
      and delivered when the remote operator connects, so VarAC is safe to use regardless of license status.
    </div>
    <div class="compose-foot">
      <div class="compose-status" id="replyStatus"></div>
      <button class="btn green" id="sendBtn" onclick="confirmAndSend()" title="Send on every ticked channel">Send</button>
    </div>
  </section>

  <div class="section-label">Previous Messages</div>
  <div id="histArea"></div>

  <div class="section-label" style="margin-top:30px">Saved Message Log
    <button class="btn xs" id="logToggleBtn" onclick="toggleLog()" style="margin-left:auto">View log</button>
  </div>
  <div id="logArea" class="hidden"><div id="logEntries"></div></div>

  <div class="footer">
    <button class="btn sm danger" onclick="confirmShutdown()" title="Shut down HamLink cleanly, including the radio programs it launched">⏻ Stop HamLink</button>
    <div>Stops the app and closes VarAC, Soundmodem, Pat, and VARA FM.</div>
    <div style="margin-top:10px">HamLink Radio <span id="verText"></span> · <a href="#" onclick="checkUpdateNow(true);return false" title="Ask GitHub whether a newer release exists">Check for updates</a></div>
    <div style="margin-top:4px" id="footCfg" title="Where your settings are stored"></div>
  </div>
</main>

<!-- ===================== NON-COMPLIANT RF MODAL ===================== -->
<div class="modal" id="rfNonCompliantModal" role="dialog" aria-modal="true" aria-labelledby="ncTitle">
  <div class="modal-box">
    <h3 id="ncTitle" style="color:var(--red)">RF Bandwidth Compliance Warning</h3>
    <p id="ncRfDetails"></p>
    <div class="notice danger"><strong>FCC Part 97.221:</strong> automatically controlled digital stations must not exceed 500 Hz occupied bandwidth. The selected channel(s) exceed this limit and are <strong>blocked by default</strong>.</div>
    <p style="margin-top:12px">To proceed, confirm one of the following:</p>
    <div class="modal-actions">
      <button class="btn" style="border-color:var(--accent);color:var(--accent)" onclick="proceedNcRf()">I am a licensed amateur radio operator and I am present at or supervising this station</button>
      <button class="btn" style="border-color:var(--red);color:var(--red)" onclick="proceedNcRf()">Emergency — immediate safety of life or property (FCC Part 97.403)</button>
      <button class="btn ghost" onclick="cancelNcRfSend()">Cancel</button>
    </div>
  </div>
</div>

<!-- ===================== SAVED CONFIRMATION MODAL ===================== -->
<div class="modal" id="savedModal" role="dialog" aria-modal="true" aria-labelledby="savedTitle">
  <div class="modal-box saved-box">
    <h3 id="savedTitle" style="color:var(--green)">✓ Settings saved</h3>
    <p>Written to:</p>
    <div class="path" id="savedPath"></div>
    <p id="savedTime"></p>
    <div id="savedWarnWrap" class="hidden"><p style="margin-top:10px"><b>Things worth checking:</b></p><ul id="savedWarnList"></ul></div>
    <p id="savedAllGood" class="hidden" style="color:var(--green);margin-top:10px">No problems found in these settings.</p>
    <div class="modal-actions"><button class="btn primary" onclick="$('savedModal').classList.remove('open')">OK</button></div>
  </div>
</div>

<!-- ===================== SITREP DRAWER ===================== -->
<div class="drawer-bg" id="sitrepOverlay" onclick="if(event.target===this)closeSitrep()">
  <div class="drawer" role="dialog" aria-modal="true" aria-labelledby="sitrepTitle">
    <div class="drawer-head"><h2 id="sitrepTitle">📋 Post Sitrep</h2><button class="btn icon ghost" onclick="closeSitrep()" aria-label="Close">✕</button></div>
    <div class="drawer-body">
      <div class="notice blue" style="margin:0 0 16px">A situation report is saved to the VarAC BBS for pickup or relay, and a 500 Hz VarAC broadcast announces it. Next: <strong>#<span id="sitrepNum">001</span></strong></div>
      <div class="sr-field"><label>House</label><input class="fi" id="srHouse" value="All OK"><div class="sr-hint">Damage, flooding, structural issues</div></div>
      <div class="sr-field"><label>Vehicles</label><input class="fi" id="srVehicles" value="All OK"><div class="sr-hint">Availability, fuel, damage</div></div>
      <div class="sr-field"><label>Utilities</label><input class="fi" id="srUtilities" value="All OK"><div class="sr-hint">Power, water, gas, internet</div></div>
      <div class="sr-field"><label>Health</label><input class="fi" id="srHealth" value="All OK"><div class="sr-hint">Health status of family members</div></div>
      <div class="sr-field"><label>Immediate needs</label><input class="fi" id="srNeeds" value="None"><div class="sr-hint">Fuel, medicine, supplies</div></div>
      <div class="sr-field"><label>Relocation</label><input class="fi" id="srRelocation" value="No change"><div class="sr-hint">Staying put, evacuating, relocated</div></div>
      <div class="sr-field"><label>Location</label><input class="fi" id="srLocation" placeholder="GPS coords, address, or What3Words"><div class="sr-hint">Only if relocated — leave blank if staying home</div></div>
      <div class="sr-field"><label>Remarks</label><textarea class="fi" id="srRemarks" rows="3" placeholder="Any additional details…"></textarea></div>
      <div class="sr-result" id="sitrepResult"></div>
    </div>
    <div class="drawer-foot">
      <button class="btn green" style="flex:1" onclick="sendQuickSitrep()">✅ Quick All-OK</button>
      <button class="btn primary" style="flex:1" onclick="sendSitrep()">📋 Post Sitrep</button>
    </div>
  </div>
</div>

<!-- ===================== SETTINGS DRAWER ===================== -->
<div class="drawer-bg" id="settOverlay" onclick="if(event.target===this)closeSett()">
  <div class="drawer" role="dialog" aria-modal="true" aria-labelledby="settTitle">
    <div class="drawer-head"><h2 id="settTitle">Settings</h2><button class="btn icon ghost" onclick="closeSett()" aria-label="Close" title="Close settings (Esc)">✕</button></div>
    <div class="drawer-body">
      <div class="cfgfile" id="cfgFile" title="Every setting on this page is stored in this file. Back it up to keep your configuration.">
        <div><b>Settings file:</b> <code id="cfgPath">config.json</code></div>
        <div class="cfgfile-row"><span id="cfgSaved"></span><span class="spacer"></span>
          <button class="btn xs" onclick="copyCfgPath()" title="Copy the full path to the clipboard">Copy path</button>
          <button class="btn xs" onclick="openFolder()" title="Open the folder that contains config.json in Explorer">Open folder</button></div>
      </div>
      <div class="notice warn hidden" id="saveWarnings"></div>

      <details class="sec" open>
        <summary>👤 People &amp; callsigns</summary>
        <div class="sec-body">
          <div class="notice blue" style="margin:0 0 14px">
            <b>How callsigns work in HamLink</b><br>
            Enter the <b>base callsign without an SSID</b> here (e.g. <code>KK4ODA</code>, not <code>KK4ODA-1</code>). Each channel then derives its own address:
            <ul style="margin:6px 0 0 18px">
              <li><b>VarAC</b> — uses the base callsign as-is (VMails to/from <code>KK4ODA</code>; the traveler usually appears as <code>KK4ODA/P</code>).</li>
              <li><b>APRS</b> — base callsign <b>+ the SSIDs set in the APRS section</b> (home <code>-5</code>, traveler <code>-7</code>/<code>-9</code>). Do not type SSIDs here.</li>
              <li><b>Winlink</b> — uses the base callsign to log in, but messages go between the <b>tactical addresses</b> set in the Winlink section.</li>
            </ul>
            <div id="callPreview" style="margin-top:8px;font-family:var(--mono);font-size:12px"></div>
          </div>
          <div class="fg"><label class="fl">Their name (shown in alerts)</label><input class="fi" id="cName" placeholder="e.g. Alex"><div class="fhint">The traveler's first name. Used in "New message from …" and in the Send Message button.</div></div>
          <div class="fg"><label class="fl">Home station callsign — base only, no SSID</label><input class="fi" id="cHomeCall" placeholder="e.g. KK4ODA" oninput="updateCallPreview()">
            <div class="fhint">The licensed callsign of the home station. Used for: VarAC replies (<i>from</i> address), the APRS-IS login and home address (with the APRS SSID added), and the beacon. <b>No -SSID, no /P.</b></div></div>
          <div class="fg"><label class="fl">Watch for callsign(s) — VarAC and Winlink senders</label><input class="fi" id="cWatch" placeholder="e.g. KK4ODA, KK4ODA/P" oninput="updateCallPreview()">
            <div class="fhint">Comma-separated. Alerts fire only for VMails and Winlink mail <i>from</i> these, matched exactly as the sender appears in VarAC or Winlink — normally the base callsign and its portable form (<code>KK4ODA, KK4ODA/P</code>). <b>APRS SSIDs do not belong here</b>; set them in the APRS section. Leave empty to alert on every sender.</div></div>
        </div>
      </details>

      <details class="sec" open>
        <summary>📡 VarAC <span class="state" id="stVarac">Off</span></summary>
        <div class="sec-body">
          <div class="fg"><label class="fl">VarAC database path</label>
            <div class="frow"><input class="fi" id="cDb" placeholder="C:\VarAC\VarAC.db"><button class="btn sm" onclick="browse('')">Browse</button><button class="btn sm primary" onclick="testDb()">Test</button></div>
            <div class="fb" id="fb"></div><div class="tr" id="dbTr"></div></div>
          <div class="fg"><label class="fl">VarAC executable path</label>
            <div class="frow"><input class="fi" id="cVaracExe" placeholder="C:\VarAC\VarAC.exe"><button class="btn sm" onclick="runTest('varac_exe', 'varacExeTr', {path: val('cVaracExe')})" title="Check that the file exists and whether VarAC is running">Test</button></div>
            <div class="tr" id="varacExeTr"></div>
            <div class="fhint">Launched automatically on startup if not already running. Leave blank to skip.</div></div>
          <div class="fg"><label class="fl">VarAC profile (.ini file name)</label><input class="fi w-md" id="cVaracProfile" placeholder="e.g. varac_7300.ini">
            <div class="fhint">Optional. Leave blank for the default profile.</div></div>
          <div class="fg"><label class="fl">BBS directory override</label>
            <div class="frow"><input class="fi" id="cBbsDir" placeholder="Auto-read from VarAC .ini"><button class="btn sm" onclick="runTest('bbs_dir', 'bbsTr', {path: val('cBbsDir')})" title="Check that the BBS folder (where sitreps are posted) exists">Test</button></div>
            <div class="tr" id="bbsTr"></div>
            <div class="fhint">Leave blank to auto-read from the VarAC profile.</div><div class="fhint" id="bbsResolved" style="color:var(--green)"></div></div>
          <div class="fg"><label class="fl">Check every (seconds)</label><input class="fi w-xs" id="cPoll" type="number" min="5" max="300" value="15"></div>
        </div>
      </details>

      <details class="sec">
        <summary>🔔 Alert sound &amp; quick replies</summary>
        <div class="sec-body">
          <div class="fg"><label class="fl">Sound</label>
            <select class="fsel" id="cSound">
              <option value="gentle">Gentle chime</option><option value="two_tone">Two-tone beep</option>
              <option value="sonar">Sonar ping</option><option value="siren">Rising siren</option>
              <option value="klaxon">Klaxon horn</option><option value="telegraph">Telegraph</option>
              <option value="voice">Voice alert</option>
            </select></div>
          <div class="fg"><label class="fl">Volume: <span id="volL">30%</span></label>
            <input type="range" id="cVol" min="0" max="100" value="30" oninput="document.getElementById('volL').textContent=this.value+'%'"></div>
          <div class="fg"><label class="fl">Stop the alarm by itself after (minutes)</label><input class="fi w-xs" id="cAlarmTimeout" type="number" min="0" max="720" value="15">
            <div class="fhint">The PC speaker beep and the browser sound repeat until you press <b>Silence alarm</b>, <b>Dismiss</b>, or reply — or until this many minutes have passed. 0 = never stop on its own. Messages stay on the dashboard either way.</div></div>
          <div class="fg"><button class="btn sm" onclick="preview()">▶ Preview</button></div>
          <div class="subhead">Quick replies</div>
          <div class="fg"><label class="fl">Pre-written messages (one per line)</label>
            <textarea class="fi" id="cQuick" rows="5" style="resize:vertical"></textarea>
            <div class="fhint">Shown as one-tap buttons when composing a message.</div></div>
        </div>
      </details>

      <details class="sec">
        <summary>📱 Phone notifications (Pushover) <span class="state" id="stPo">Off</span></summary>
        <div class="sec-body">
          <div class="fg"><div class="tgl-row"><label class="fl">Enable</label><div class="tgl" id="cPoOn" onclick="this.classList.toggle('on')"></div></div></div>
          <div class="fg"><label class="fl">User key</label><input class="fi" id="cPoUser" placeholder="Pushover user key" autocomplete="off"></div>
          <div class="fg"><label class="fl">API token</label><input class="fi" id="cPoToken" placeholder="Pushover app token" autocomplete="off"></div>
          <div class="fg"><label class="fl">Priority</label>
            <select class="fsel" id="cPoPri"><option value="0">Normal</option><option value="1">High</option><option value="2">Emergency (repeats until acknowledged)</option></select></div>
          <div class="fg"><label class="fl">Sound</label>
            <select class="fsel" id="cPoSnd">
              <option value="pushover">Pushover</option><option value="bike">Bike</option><option value="bugle">Bugle</option>
              <option value="cosmic">Cosmic</option><option value="falling">Falling</option><option value="incoming">Incoming</option>
              <option value="magic">Magic</option><option value="persistent">Persistent</option><option value="siren">Siren</option>
              <option value="spacealarm">Space Alarm</option><option value="none">Silent</option></select></div>
          <div class="fg"><div class="tgl-row"><label class="fl">Include quick-reply links in notifications</label><div class="tgl" id="cPoQuickReplies" onclick="this.classList.toggle('on')"></div></div>
            <div class="fhint">Each link opens a confirmation page before anything is sent. Your phone must be on the same network as HamLink.</div></div>
          <div class="fg"><button class="btn sm" onclick="testPo()">Send test</button><div class="tr" id="poTr"></div></div>
        </div>
      </details>

      <details class="sec">
        <summary>🌐 APRS messaging (APRS-IS) <span class="state" id="stAprs">Off</span></summary>
        <div class="sec-body">
          <div class="fg"><div class="tgl-row"><label class="fl">Enable APRS-IS</label><div class="tgl" id="cAprsOn" onclick="this.classList.toggle('on')"></div></div>
            <div class="fhint">Two-way short messages and position tracking through the APRS internet network.</div></div>
          <div class="fg"><label class="fl">Home station APRS SSID (just the -number)</label><input class="fi w-xs" id="cAprsSsid" placeholder="-5" oninput="updateCallPreview()">
            <div class="fhint">Enter only the suffix, e.g. <code>-5</code>. It is added to the home callsign from the People section to make the home APRS address (<code>KK4ODA-5</code>). The traveler sends APRS messages <b>to</b> this address. <code>-5</code> is conventional for a home/fixed station; use anything from -0 to -15 that is not already used by one of your other radios.</div></div>
          <div class="fg"><label class="fl">Traveler APRS SSID(s) (just the -numbers)</label><input class="fi w-sm" id="cAprsTravSsid" placeholder="-7, -9" oninput="updateCallPreview()">
            <div class="fhint">Comma-separated suffixes, e.g. <code>-7, -9</code> (<code>-7</code> = handheld, <code>-9</code> = mobile/car by APRS convention). Added to the same base callsign to form the traveler's APRS addresses (<code>KK4ODA-7</code>, <code>KK4ODA-9</code>). Position beacons and messages from these are tracked; replies go to whichever one last sent a message, or the first listed.</div></div>
          <div class="fg"><label class="fl">APRS-IS passcode</label>
            <div class="frow"><input class="fi" id="cAprsPass" placeholder="12345"><button class="btn sm" onclick="genPasscode()">Auto-generate</button></div>
            <div class="fhint">Derived from your callsign.</div></div>
          <div class="fg"><label class="fl">APRS-IS server</label><input class="fi" id="cAprsSrv" placeholder="rotate.aprs2.net"></div>
          <div class="fg"><label class="fl">APRS-IS port</label>
            <div class="frow"><input class="fi w-xs" id="cAprsPort" placeholder="14580" style="flex:0"><button class="btn sm" onclick="runTest('aprs_is', 'aprsTr', {server: val('cAprsSrv'), port: val('cAprsPort'), callsign: val('cHomeCall') + (val('cAprsSsid') || '-5'), passcode: val('cAprsPass')})" title="Log in to the APRS-IS server with this callsign and passcode and report whether the passcode was accepted">Test login</button></div>
            <div class="tr" id="aprsTr"></div></div>
          <div class="fg"><div class="tgl-row"><label class="fl">Send via RF when internet is down (needs Soundmodem)</label><div class="tgl" id="cAprsRfFallback" onclick="this.classList.toggle('on')"></div></div>
            <div class="fhint">Blocked by default at send time — requires a licensed operator present or the emergency exception.</div></div>
          <div class="fg"><div class="tgl-row"><label class="fl">Copy messages to APRS mailbox (MAIL store &amp; forward)</label><div class="tgl" id="cAprsMailbox" onclick="this.classList.toggle('on')"></div></div>
            <div class="fhint">Also sends a copy to the MAIL bot so the traveler can retrieve it later with spotty coverage.</div></div>
          <div class="fg"><label class="fl">aprs.fi API key (optional)</label>
            <div class="frow"><input class="fi" id="cAprsFiKey" type="password" placeholder="free at aprs.fi/account/me" autocomplete="off"><button class="btn sm" onclick="runTest('aprs_fi', 'aprsFiTr', {key: val('cAprsFiKey')})" title="Ask aprs.fi for the traveler's last position using this key">Test</button></div>
            <div class="tr" id="aprsFiTr"></div>
            <div class="fhint">On startup, fetches the traveler's latest position so you see movement that happened while HamLink was off. Get a key at <a href="https://aprs.fi/account/me" target="_blank" rel="noopener">aprs.fi → My account</a>.</div></div>
        </div>
      </details>

      <details class="sec">
        <summary>📻 APRS RF monitor (Soundmodem) <span class="state" id="stSm">Off</span></summary>
        <div class="sec-body">
          <div class="fg"><div class="tgl-row"><label class="fl">Enable RF APRS via Soundmodem</label><div class="tgl" id="cSmOn" onclick="this.classList.toggle('on');updateBcnRfAvail()"></div></div>
            <div class="fhint">Connects to the UZ7HO Soundmodem KISS port for RF APRS receive and transmit.</div></div>
          <div class="fg"><label class="fl">Soundmodem path</label>
            <div class="frow"><input class="fi" id="cSmPath" placeholder="C:\Soundmodem\soundmodem.exe"><button class="btn sm" onclick="runTest('exe', 'smExeTr', {path: val('cSmPath')})" title="Check that the file exists">Test</button></div>
            <div class="tr" id="smExeTr"></div><div class="fhint">Launched automatically on startup.</div></div>
          <div class="fg"><label class="fl">KISS TCP host</label><input class="fi w-sm" id="cSmHost" placeholder="127.0.0.1"></div>
          <div class="fg"><label class="fl">KISS TCP port</label>
            <div class="frow"><input class="fi w-xs" id="cSmPort" type="number" placeholder="8100" style="flex:0"><button class="btn sm" onclick="runTest('kiss', 'kissTr', {host: val('cSmHost'), port: val('cSmPort')})" title="Try to connect to Soundmodem's KISS port (Soundmodem must be running)">Test connection</button></div>
            <div class="tr" id="kissTr"></div><div class="fhint">Must match Soundmodem's KISS server port (default 8100).</div></div>
        </div>
      </details>

      <details class="sec">
        <summary>📍 APRS position beacon <span class="state" id="stBcn">Off</span></summary>
        <div class="sec-body">
          <div class="fg"><div class="tgl-row"><label class="fl">Enable position beacon</label><div class="tgl" id="cBcnOn" onclick="this.classList.toggle('on')"></div></div>
            <div class="fhint">Periodically beacons the home station position so nearby iGates know you exist. Important for receiving RF APRS messages when internet is down.</div></div>
          <div class="fg"><label class="fl">Latitude / longitude</label>
            <div class="frow"><input class="fi" id="cBcnLat" type="number" step="0.0001" placeholder="33.7490"><input class="fi" id="cBcnLon" type="number" step="0.0001" placeholder="-84.3880"><button class="btn sm" onclick="detectBeaconLocation()">📍 Detect</button></div></div>
          <div class="fg"><label class="fl">Symbol</label>
            <select class="fsel" id="cBcnSymbol">
              <option value="/- ">House</option><option value="/r ">Antenna</option><option value="\- ">Diamond</option>
              <option value="/y ">House with Yagi</option><option value="/# ">Digipeater</option><option value="/& ">Gateway</option><option value="/I ">TCP/IP station</option>
            </select></div>
          <div class="fg"><label class="fl">Beacon interval (minutes)</label><input class="fi w-xs" id="cBcnInterval" type="number" min="5" max="120" value="30"><div class="fhint">5–120 minutes. 30 is typical for a fixed station.</div></div>
          <div class="fg"><label class="fl">Beacon comment</label><input class="fi" id="cBcnComment" placeholder="HamLink Radio"></div>
          <div class="fg"><button class="btn sm" onclick="runTest('beacon', 'bcnTr', {lat: val('cBcnLat'), lon: val('cBcnLon'), callsign: val('cHomeCall'), ssid: val('cAprsSsid'), symbol_table: $('cBcnSymbol').value.charAt(0), symbol_code: $('cBcnSymbol').value.charAt(1), comment: val('cBcnComment')})" title="Show the exact APRS packet that would be sent (nothing is transmitted)">Preview beacon packet</button><div class="tr" id="bcnTr"></div></div>
          <div class="fg"><div class="tgl-row"><label class="fl">Beacon via APRS-IS (internet)</label><div class="tgl" id="cBcnAprsIs" onclick="this.classList.toggle('on')"></div></div></div>
          <div class="fg"><div class="tgl-row"><label class="fl">Beacon via RF (Soundmodem)</label><div class="tgl" id="cBcnRf" onclick="this.classList.toggle('on');updateBcnRfWarn()"></div></div>
            <div class="notice danger hidden" id="bcnRfWarn"><strong>FCC Part 97.221:</strong> RF beacons via Soundmodem exceed the 500 Hz limit for automatically controlled digital stations. Enabling this requires a licensed amateur radio operator present at or supervising the station, or the emergency exception (97.403).</div>
            <div class="fhint">Off by default. Requires the Soundmodem section to be enabled.</div></div>
        </div>
      </details>

      <details class="sec">
        <summary>🔁 VMail relay automation <span class="state" id="stRelay">Off</span></summary>
        <div class="sec-body">
          <div class="fg"><div class="tgl-row"><label class="fl">Enable relay features</label><div class="tgl" id="cRelayOn" onclick="this.classList.toggle('on')"></div></div>
            <div class="fhint">Relay notification tracking, filtering, and reply routing via relay stations.</div></div>
          <div class="fg"><div class="tgl-row"><label class="fl">Auto-retrieve VMails</label><div class="tgl" id="cRelayAutoRetrieve" onclick="this.classList.toggle('on')"></div></div>
            <div class="fhint">Connects to relay stations and downloads pending VMails using VarAC UI automation. Requires VarAC to be running.</div></div>
          <div class="fg"><div class="tgl-row"><label class="fl">Confirm before connecting</label><div class="tgl on" id="cRelayConfirm" onclick="this.classList.toggle('on')"></div></div>
            <div class="fhint">Recommended — shows an approval prompt before VarAC QSYs to a relay frequency.</div></div>
          <div class="fg"><div class="tgl-row"><label class="fl">Route replies via relay</label><div class="tgl on" id="cRelayRouteReplies" onclick="this.classList.toggle('on')"></div></div>
            <div class="fhint">Send VarAC replies back through the relay station that delivered the original message.</div></div>
          <div class="fg"><label class="fl">Cooldown between retrievals (seconds)</label><input class="fi w-xs" id="cRelayCooldown" type="number" min="60" max="3600" value="300"></div>
          <div class="fg"><label class="fl">Delay before auto-retrieve (seconds)</label><input class="fi w-xs" id="cRelayDelay" type="number" min="0" max="120" value="10"></div>
          <div class="fg"><label class="fl">Max retries</label><input class="fi w-xs" id="cRelayMaxRetries" type="number" min="0" max="5" value="2"></div>
          <div class="fg"><label class="fl">Ignore stations</label><input class="fi" id="cRelayIgnore" placeholder="e.g. W5XYZ, N0CALL"><div class="fhint">Comma-separated relay callsigns to never auto-connect to.</div></div>
        </div>
      </details>

      <details class="sec">
        <summary>✉️ Winlink (via Pat) <span class="state" id="stPat">Off</span></summary>
        <div class="sec-body">
          <div class="fg"><div class="tgl-row"><label class="fl">Enable Winlink via Pat</label><div class="tgl" id="cPatOn" onclick="this.classList.toggle('on')"></div></div>
            <div class="fhint">Email-like messaging through Winlink gateways using the Pat client.</div></div>
          <div class="fg"><label class="fl">Pat executable path</label><input class="fi" id="cPatPath" placeholder="C:\Pat\pat.exe"><div class="fhint">Launched automatically on startup.</div></div>
          <div class="fg"><label class="fl">Pat HTTP address</label>
            <div class="frow"><input class="fi w-md" id="cPatAddr" placeholder="localhost:8080" style="flex:0"><button class="btn sm" onclick="runTest('pat', 'patTr', {http_addr: val('cPatAddr'), exe_path: val('cPatPath')})" title="Check the pat.exe path and whether Pat is answering on this address">Test</button></div>
            <div class="tr" id="patTr"></div></div>
          <div class="fg"><label class="fl">Check Winlink every (seconds)</label><input class="fi w-xs" id="cPatPoll" type="number" min="60" max="900" value="300"><div class="fhint">Full sync with the Winlink CMS. Minimum 60 seconds.</div></div>
          <div class="fg"><div class="tgl-row"><label class="fl">Query Winlink position reports</label><div class="tgl" id="cPatPosReports" onclick="this.classList.toggle('on')"></div></div>
            <div class="fhint">Checks the Winlink CMS for the traveler's latest position report.</div></div>

          <div class="subhead">Pat Winlink account</div>
          <div class="fhint" style="margin-bottom:10px">These are written to Pat's own config file.</div>
          <div class="fg"><label class="fl">Winlink callsign — base only, no SSID</label><input class="fi w-sm" id="cPatCall" placeholder="KK4ODA"><div class="fhint">The callsign registered with Winlink, exactly as on winlink.org (e.g. <code>KK4ODA</code>). Pat logs in to the CMS with it. No -SSID.</div></div>
          <div class="fg"><label class="fl">Winlink password</label><input class="fi w-md" id="cPatPass" type="password" placeholder="Secure login password" autocomplete="off"><div class="fhint">Your Winlink account password (the one used for Winlink Express or winlink.org).</div></div>
          <div class="fg"><label class="fl">Grid locator</label>
            <div class="frow"><input class="fi w-xs" id="cPatLoc" placeholder="EM73" style="flex:0"><button class="btn sm" onclick="detectLocation()">📍 Use my location</button></div>
            <div class="fhint">Maidenhead grid square — used to find nearby gateways.</div></div>
          <div class="subhead">Tactical addresses</div>
          <div class="fhint" style="margin-bottom:10px">Winlink refuses mail from a callsign to itself, and both of you share one callsign. Tactical addresses are extra Winlink mailboxes attached to that callsign, so home and traveler each get their own. They are <b>not callsigns</b>: pick a word (3–12 letters, digits allowed after a dash), e.g. <code>BRECKEN</code> and <code>FACUNDO</code>. The traveler must add their own tactical address in their Winlink program too.</div>
          <div class="fg"><label class="fl">Home station tactical address</label><input class="fi w-sm" id="cPatHomeTac" placeholder="e.g. BRECKEN" oninput="updateCallPreview()"><div class="fhint">Home sends Winlink mail <b>from</b> this address, and the traveler sends <b>to</b> it. HamLink registers it in Pat as an auxiliary address when you press Save Pat configuration.</div></div>
          <div class="fg"><label class="fl">Traveler tactical address</label><input class="fi w-sm" id="cPatTravTac" placeholder="e.g. FACUNDO" oninput="updateCallPreview()"><div class="fhint">Home sends Winlink replies <b>to</b> this address, and alerts only for mail <b>from</b> it. The traveler must set up this same address in Winlink Express / Pat on their end.</div></div>
          <div class="fg"><button class="btn sm primary" onclick="savePatConfig()">Save Pat configuration</button> <span id="patSaveStatus" class="fhint" style="display:inline;margin-left:8px"></span></div>

          <div class="subhead">RF fallback (VARA FM)</div>
          <div class="fg"><div class="tgl-row"><label class="fl">Use a VARA FM gateway when internet is down</label><div class="tgl" id="cPatRfFallback" onclick="this.classList.toggle('on')"></div></div>
            <div class="fhint">Blocked by default at send time — requires a licensed operator present or the emergency exception.</div></div>
          <div class="fg"><label class="fl">RF gateway poll interval (seconds)</label><input class="fi w-sm" id="cPatRfPoll" type="number" min="600" max="43200" value="10800"><div class="fhint">Default 10800 (3 hours). Minimum 600.</div></div>
          <div class="fg"><label class="fl">VARA FM gateway</label>
            <div class="frow"><input class="fi w-sm" id="cPatRfGw" placeholder="e.g. WD5EMA-10" style="flex:0"><button class="btn sm" onclick="loadGateways()">Find nearby</button></div>
            <div class="gw fhint" id="gwList"></div>
            <div class="fhint">The gateway station's <b>callsign with its SSID</b>, exactly as listed by Winlink — the SSID is part of the name here (e.g. <code>WD5EMA-10</code>; RMS gateways are usually <code>-10</code>). Use a dash, not =. This is another ham's station, not yours.</div></div>
          <div class="fg"><label class="fl">VARA FM executable path</label>
            <div class="frow"><input class="fi" id="cPatVaraExe" placeholder="C:\VARA FM\VARAFM.exe"><button class="btn sm" onclick="runTest('exe', 'varaExeTr', {path: val('cPatVaraExe')})" title="Check that the file exists">Test</button></div>
            <div class="tr" id="varaExeTr"></div><div class="fhint">Leave blank if VARA FM is already running.</div></div>
          <div class="fg"><label class="fl">VARA FM modem address</label>
            <div class="frow"><input class="fi w-md" id="cPatVaraAddr" placeholder="localhost:8300" style="flex:0"><button class="btn sm" onclick="runTest('varafm', 'varaTr', {addr: val('cPatVaraAddr')})" title="Try to connect to the VARA FM modem port (VARA FM must be running)">Test connection</button></div>
            <div class="tr" id="varaTr"></div></div>
        </div>
      </details>

      <details class="sec">
        <summary>⬆️ Updates <span class="state" id="stUpd">Off</span></summary>
        <div class="sec-body">
          <div class="fg"><div class="tgl-row"><label class="fl">Check for new releases automatically</label><div class="tgl" id="cUpdOn" onclick="this.classList.toggle('on')"></div></div>
            <div class="fhint">Asks GitHub for the latest release shortly after startup and then periodically. Nothing is installed without your confirmation.</div></div>
          <div class="fg"><label class="fl">Check every (hours)</label><input class="fi w-xs" id="cUpdHours" type="number" min="1" max="168" value="6"></div>
          <div class="fg"><label class="fl">GitHub token (optional)</label>
            <div class="frow"><input class="fi" id="cUpdToken" type="password" placeholder="github_pat_…" autocomplete="off" title="Only needed while the HamLink repository is private"><button class="btn sm" onclick="runTest('github', 'updTestTr', {token: val('cUpdToken')})" title="Ask GitHub for the latest release using this token">Test</button></div>
            <div class="tr" id="updTestTr"></div>
            <div class="fhint">Only needed while the HamLink repository on GitHub is <b>private</b>; leave blank once it is public. To create one: sign in to GitHub → <a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noopener">Settings → Developer settings → Fine-grained tokens → Generate new token</a>. Give it a name, set <i>Repository access</i> to <b>Only select repositories → KK4ODA/HamLink-Radio</b>, and under <i>Permissions → Repository permissions</i> set <b>Contents: Read-only</b>. Generate, copy the <code>github_pat_…</code> value, paste it here, press Test, then Save.</div></div>
          <div class="fg"><div class="fhint" id="updInfo"></div></div>
          <div class="fg"><button class="btn sm" onclick="checkUpdateNow(false)">Check now</button> <button class="btn sm primary hidden" id="updInstallBtn" onclick="closeSett();openUpdate()">Install update</button><div class="tr" id="updTr"></div></div>
        </div>
      </details>

      <details class="sec">
        <summary>🗺️ Offline map <span class="state" id="stMap">Off</span></summary>
        <div class="sec-body">
          <div class="fhint" style="margin-bottom:10px">HamLink downloads map tiles from the USGS National Map (US public domain) and keeps them in the <code>tiles/</code> folder, so the map works with no internet. Pick the area around home and press Download.</div>
          <div class="subhead" style="margin-top:0;padding-top:0;border:none">Download a map area</div>
          <div class="fg"><label class="fl">Center (latitude / longitude)</label>
            <div class="frow"><input class="fi" id="cMapLat" type="number" step="0.0001" placeholder="33.7490" title="Latitude of the center of the map area"><input class="fi" id="cMapLon" type="number" step="0.0001" placeholder="-84.3880" title="Longitude of the center of the map area"><button class="btn sm" onclick="mapUseBeacon()" title="Copy the coordinates from the Position beacon section">Use home position</button></div></div>
          <div class="fg"><label class="fl">Radius around center (km)</label><input class="fi w-xs" id="cMapRadius" type="number" min="5" max="600" value="150" oninput="mapEstimate()" title="How far from the center the map should extend. 150 km covers a typical day's drive."></div>
          <div class="fg"><label class="fl">Detail (max zoom)</label>
            <select class="fsel w-md" id="cMapZoom" onchange="mapEstimate()" title="Higher zoom = more street detail but many more tiles to download">
              <option value="10">10 — towns and highways (small)</option>
              <option value="12" selected>12 — roads and neighborhoods (recommended)</option>
              <option value="13">13 — streets</option>
              <option value="14">14 — street names (large)</option>
            </select></div>
          <div class="fg"><label class="fl">Map style</label>
            <select class="fsel" id="cMapSource" title="Which USGS basemap to download">
              <option value="usgs_topo">USGS Topo — roads, terrain, place names</option>
              <option value="usgs_imagery">USGS Imagery + Topo — satellite with labels</option>
            </select></div>
          <div class="fg"><label class="fl">Name for this map</label><input class="fi w-md" id="cMapName" placeholder="home-area" title="File name for the downloaded map (saved as tiles/<name>.mbtiles)"></div>
          <div class="fg"><div class="fhint" id="mapEst"></div></div>
          <div class="fg"><button class="btn sm primary" id="mapDlBtn" onclick="mapDownload()" title="Download the tiles for this area now. You can keep using HamLink while it runs.">⬇️ Download map</button>
            <button class="btn sm ghost hidden" id="mapCancelBtn" onclick="mapCancel()">Cancel</button>
            <div class="progress hidden" id="mapProgress"><i></i></div><div class="fhint" id="mapDlStatus"></div></div>
          <div class="subhead">Maps on this computer</div>
          <div class="maplist" id="mapList"><div class="fhint">Loading…</div></div>
          <div class="fhint">Files live in <code id="tilesDirText">tiles/</code>. You can also drop an <code>.mbtiles</code> file made elsewhere into that folder.</div>
        </div>
      </details>

      <div style="text-align:center;margin:14px 0 4px">
        <button class="btn xs ghost" onclick="showCompliance()">⚖️ View regulatory notice</button>
      </div>
    </div>
    <div class="drawer-foot">
      <button class="btn ghost" onclick="closeSett()" title="Close without saving">Cancel</button>
      <button class="btn" onclick="checkSettings()" title="Look for mistakes in the current saved settings without changing anything">Check settings</button>
      <button class="btn primary" style="flex:1" onclick="saveSett()" title="Write all settings to config.json and apply them">Save settings</button>
    </div>
  </div>
</div>

<script>
/* =====================================================================
   HamLink Radio dashboard
   Talks to the Flask backend over /api/*. Polls /api/status every 3s.
   ===================================================================== */
'use strict';
const $ = id => document.getElementById(id);

/* ---------- state ---------- */
let cfg = {};              // last config snapshot from /api/status
let last = null;           // last full status payload
let csrfToken = '';
let audioCtx = null, soundOn = false, alarmInt = null;
const dismissedIds = new Set();   // alerts silenced locally (still shown as new)
const seenIds = new Set();        // alerts already shown as browser notifications
let currentTab = 'dashboard';
let _replyToCall = '', _composeChan = 'multi', aprsManualUncheck = false;
let _ackedPosKey = null;
let _lastActiveHtml = '', _lastHistHtml = '';

/* ---------- helpers ---------- */
function esc(s){
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function cpost(url, body){
  return fetch(url, {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken}, body:JSON.stringify(body||{})});
}
function toast(msg, isErr){
  const t = $('toast'); t.textContent = msg; t.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), msg.length > 40 ? 4500 : 2500);
}
function show(id, on){ $(id).classList.toggle('hidden', !on); }
function fmtTime(iso){ try { return new Date(iso).toLocaleString(); } catch(e){ return iso || ''; } }
function setDot(dotId, textId, status, text){ $(dotId).className = 'dot' + (status ? ' ' + status : ''); $(textId).textContent = text; }

/* ---------- theme ---------- */
function applyTheme(t){
  if (t) document.documentElement.setAttribute('data-theme', t); else document.documentElement.removeAttribute('data-theme');
  const dark = t === 'dark' || (!t && window.matchMedia('(prefers-color-scheme: dark)').matches);
  $('btnTheme').textContent = dark ? '☀️' : '🌙';
}
function toggleTheme(){
  const dark = document.documentElement.getAttribute('data-theme') === 'dark' ||
    (!document.documentElement.getAttribute('data-theme') && window.matchMedia('(prefers-color-scheme: dark)').matches);
  const next = dark ? 'light' : 'dark';
  try { localStorage.setItem('hamlink_theme', next); } catch(e){}
  applyTheme(next);
}
(function(){ let t = null; try { t = localStorage.getItem('hamlink_theme'); } catch(e){} applyTheme(t); })();

/* ---------- compliance gate ---------- */
const compChecked = {1:false, 2:false, 3:false};
document.querySelectorAll('.check').forEach(el => el.addEventListener('click', () => {
  const n = el.dataset.check; compChecked[n] = !compChecked[n];
  el.classList.toggle('on', compChecked[n]); el.querySelector('.box').textContent = compChecked[n] ? '✓' : '';
  $('btnAccept').disabled = !(compChecked[1] && compChecked[2] && compChecked[3]);
}));
function acceptCompliance(){
  if (!(compChecked[1] && compChecked[2] && compChecked[3])) return;
  try { localStorage.setItem('hamlink_compliance_accepted', '1'); } catch(e){}
  show('complianceScreen', false); show('splash', !soundOn);
  if (!soundOn) splashPeek();
}
function showCompliance(){
  closeSett();
  for (let i = 1; i <= 3; i++){ compChecked[i] = true; const el = document.querySelector('.check[data-check="'+i+'"]'); el.classList.add('on'); el.querySelector('.box').textContent = '✓'; }
  $('btnAccept').disabled = false; show('complianceScreen', true);
}
(function initCompliance(){
  let ok = false; try { ok = localStorage.getItem('hamlink_compliance_accepted') === '1'; } catch(e){}
  if (ok){ show('complianceScreen', false); show('splash', true); }
})();

/* Before Start is tapped nothing polls, so peek once: if messages are already
   pending (the PC speaker may be beeping) offer a Silence button on the splash. */
async function splashPeek(){
  try {
    const r = await fetch('/api/status'); const d = await r.json();
    if (d.csrf_token) csrfToken = d.csrf_token;
    const n = (d.pending || []).length;
    if (n > 0){ $('splashAlarmText').textContent = n === 1 ? 'A message is waiting' + (d.alarm_silenced ? '.' : ' and the PC alarm is sounding.') : n + ' messages are waiting' + (d.alarm_silenced ? '.' : ' and the PC alarm is sounding.'); show('splashAlarm', true); }
  } catch(e){}
}
function silenceFromSplash(){ cpost('/api/stop_alarm').then(() => { toast('Alarm silenced'); $('splashAlarmText').textContent = 'Alarm silenced. Tap Start to read the message.'; }); }
if (!$('splash').classList.contains('hidden')) splashPeek();
function start(){
  try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch(e){}
  soundOn = true; show('splash', false);
  if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission();
  poll(); setInterval(poll, 3000);
}

/* ---------- sound engine ---------- */
function vol(){ return typeof cfg.alert_volume === 'number' ? cfg.alert_volume : 0.3; }
function speak(text, v){
  if (!('speechSynthesis' in window)) return;
  window.speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.volume = v !== undefined ? v : vol(); u.rate = 0.95; u.pitch = 1;
  const voices = window.speechSynthesis.getVoices();
  const en = voices.filter(x => x.lang && x.lang.startsWith('en'));
  const local = en.filter(x => x.localService), online = en.filter(x => !x.localService);
  // Prefer local voices so alerts still work with no internet
  const pick = local.find(x => /female|zira/i.test(x.name)) || local[0] || online.find(x => /female/i.test(x.name)) || online[0];
  if (pick) u.voice = pick;
  window.speechSynthesis.speak(u);
}
let _lastAlertName = '';
function play(type, v, fromName){
  if (type === 'voice'){ speak('New message from ' + (fromName || _lastAlertName || cfg.operator_name || 'someone'), v !== undefined ? v : vol()); return; }
  if (!audioCtx || !soundOn) return;
  const V = v !== undefined ? v : vol(), t = audioCtx.currentTime;
  const tone = (freq, at, dur, wave, gain) => {
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.connect(g); g.connect(audioCtx.destination); o.type = wave || 'sine';
    o.frequency.setValueAtTime(freq, t + at); g.gain.setValueAtTime(gain !== undefined ? gain : V, t + at);
    g.gain.exponentialRampToValueAtTime(0.001, t + at + dur); o.start(t + at); o.stop(t + at + dur + 0.05);
    return o;
  };
  const S = {
    gentle(){ [523, 659, 784].forEach((f, i) => tone(f, i * 0.22, 0.5, 'sine', V * 0.6)); },
    two_tone(){ for (let i = 0; i < 3; i++){ const o = tone(800, i * 0.3, 0.28, 'square'); o.frequency.setValueAtTime(1200, t + i * 0.3 + 0.15); } },
    sonar(){ for (let i = 0; i < 2; i++) tone(1200, i * 0.8, 0.6); },
    siren(){ const o = tone(400, 0, 1.8, 'sawtooth'); o.frequency.exponentialRampToValueAtTime(1400, t + 1.5); },
    klaxon(){ for (let i = 0; i < 4; i++) tone(i % 2 ? 380 : 440, i * 0.25, 0.22, 'sawtooth'); },
    telegraph(){ let at = 0; [0.08, 0.08, 0.08, 0.24, 0.08].forEach(d => { tone(700, at, d); at += d + 0.08; }); },
  };
  (S[type] || S.gentle)();
}
let _alarmStarted = 0;
function startAlarm(fromName){
  if (fromName) _lastAlertName = fromName;
  if (alarmInt) return;
  const s = cfg.alert_sound || 'gentle';
  _alarmStarted = Date.now();
  play(s, undefined, fromName);
  alarmInt = setInterval(() => {
    const limit = (typeof cfg.alarm_timeout_minutes === 'number' ? cfg.alarm_timeout_minutes : 15) * 60000;
    if (limit > 0 && Date.now() - _alarmStarted > limit){ silenceAll(); return; }   // stop by itself, like the PC speaker does
    play(s, undefined, fromName);
  }, s === 'voice' ? 8000 : 5000);
}
function stopAlarm(){
  if (alarmInt){ clearInterval(alarmInt); alarmInt = null; }
  if ('speechSynthesis' in window) window.speechSynthesis.cancel();   // outside the if: avoids a race with a queued utterance
}
function preview(){ play($('cSound').value, parseInt($('cVol').value) / 100, cfg.operator_name || 'someone'); }

/* ---------- polling ---------- */
let _pollFailures = 0;
async function poll(){
  let d;
  try {
    const r = await fetch('/api/status'); d = await r.json();
  } catch(e){
    // One dropped poll is normal (tab in background, brief stall); only flag after two in a row.
    if (++_pollFailures >= 2) setDot('connDot', 'connText', 'err', 'Connection to HamLink lost');
    return;
  }
  _pollFailures = 0;
  cfg = d.config || cfg; if (d.csrf_token) csrfToken = d.csrf_token;
  if (cfg.relay && cfg.relay.enabled){
    try { const rr = await fetch('/api/relay/status'); const rd = await rr.json(); if (rd.ok){ window._relayTracking = rd.tracking || {}; window._relayData = rd; } } catch(e){}
  }
  try { ui(d); } catch(e){ console.error('ui() failed', e); }
}

/* Pick the freshest known position (APRS or Winlink). */
function currentPos(d){
  const a = d.aprs_last_position, w = d.winlink_last_position;
  if (a && w){ const aT = new Date(a.time).getTime() || 0, wT = new Date(w.time).getTime() || 0; return wT > aT ? {pos:w, src:'Winlink'} : {pos:a, src:'APRS'}; }
  if (a) return {pos:a, src:'APRS'};
  if (w) return {pos:w, src:'Winlink'};
  return {pos:null, src:''};
}
function posDetails(pos){
  let det = pos.lat.toFixed(4) + ', ' + pos.lon.toFixed(4);
  if (pos.altitude) det += ' · ' + Math.round(pos.altitude) + ' m';
  if (pos.speed) det += ' · ' + Math.round(pos.speed) + ' km/h';
  if (pos.comment) det += ' · ' + pos.comment;
  return det;
}
function posKey(pos){ return pos.lat.toFixed(5) + ',' + pos.lon.toFixed(5) + ',' + (pos.time || ''); }

function ui(d){
  const c = d.config;
  show('demoTag', !!d.demo);
  $('verText').textContent = d.version ? 'v' + d.version : '';
  if (d.config_path) $('footCfg').textContent = 'Settings: ' + d.config_path;

  // --- connection pills ---
  setDot('inetDot', 'inetText', d.internet_up ? 'ok' : 'err', d.internet_up ? 'Internet' : 'No internet');
  if (!c.varac_db_path) setDot('connDot', 'connText', '', 'VarAC not configured');
  else if (d.db_connected) setDot('connDot', 'connText', 'ok', 'VarAC');
  else setDot('connDot', 'connText', 'err', 'VarAC database error');

  const eb = $('errBar');
  if (d.error && c.varac_db_path){ eb.textContent = d.error; eb.style.display = 'block'; } else eb.style.display = 'none';

  const aprsOn = !!(c.aprs && c.aprs.enabled);
  show('aprsConn', aprsOn);
  if (aprsOn){
    if (!c.home_callsign) setDot('aprsDot', 'aprsText', 'warn', 'APRS: set callsign');
    else if (d.aprs_connected && d.internet_up) setDot('aprsDot', 'aprsText', 'ok', 'APRS-IS');
    else if (d.aprs_connected) setDot('aprsDot', 'aprsText', 'warn', 'APRS-IS stale');
    else if (!d.internet_up && d.kiss_connected) setDot('aprsDot', 'aprsText', 'warn', 'APRS-IS offline · RF available');
    else if (!d.internet_up) setDot('aprsDot', 'aprsText', 'err', 'APRS-IS offline');
    else setDot('aprsDot', 'aprsText', 'warn', 'APRS-IS connecting…');
  }
  const kissOn = !!(c.soundmodem && c.soundmodem.enabled);
  show('kissConn', kissOn);
  if (kissOn) setDot('kissDot', 'kissText', d.kiss_connected ? 'ok' : 'warn', d.kiss_connected ? 'APRS RF' : 'APRS RF connecting…');

  const patOn = !!(c.pat && c.pat.enabled);
  show('patConn', patOn);
  if (patOn){
    if (d.pat_connected && d.internet_up) setDot('patDot', 'patText', 'ok', 'Winlink');
    else if (d.pat_connected && !d.internet_up && c.pat.rf_fallback) setDot('patDot', 'patText', 'ok', 'Winlink (RF fallback)');
    else if (d.pat_connected) setDot('patDot', 'patText', 'warn', 'Winlink: no internet');
    else setDot('patDot', 'patText', 'warn', d.pat_error || 'Winlink: waiting for Pat');
  }

  // --- RF Winlink countdown ---
  if (d.pat_using_rf && d.pat_next_sync){
    const rem = Math.max(0, Math.round((new Date(d.pat_next_sync).getTime() - Date.now()) / 1000));
    $('rfCountdownTime').textContent = rem > 0 ? (Math.floor(rem / 60) + 'm ' + (rem % 60) + 's') : 'syncing…';
    show('rfCountdown', true);
  } else show('rfCountdown', false);

  // --- action buttons ---
  const anyAvail = (aprsOn && d.aprs_connected) || (kissOn && d.kiss_connected) || (!!c.varac_db_path && d.db_connected) || patOn;
  const opName = c.operator_name || '';
  $('btnSendMsg').textContent = opName ? '✉️ Send Message to ' + opName : '✉️ Send Message';
  show('btnSendMsg', anyAvail);
  show('btnSitrep', !!c.bbs_directory_resolved);

  // --- position card ---
  const {pos, src} = currentPos(d);
  const lc = $('locCard');
  if (pos){
    show('locCard', true);
    $('locTitle').textContent = (opName || pos.callsign) + ' — last position (' + src + ')';
    $('locTime').textContent = fmtTime(pos.time);
    $('locDetails').textContent = posDetails(pos);
    $('locLink').href = 'https://www.google.com/maps?q=' + pos.lat + ',' + pos.lon;
    show('locMapLink', !!(c.map_file || c.map_state));
    const key = posKey(pos);
    if (_ackedPosKey === null) _ackedPosKey = key;   // the first position seen is not "new"
    const isNew = key !== _ackedPosKey;
    show('locNewBadge', isNew); show('locAckBtn', isNew); lc.classList.toggle('new', isNew);
  } else show('locCard', false);

  // --- hero ---
  const active = d.pending.filter(a => !dismissedIds.has(a.id));
  const name = opName || 'Someone';
  const sc = $('statusCard');
  if (active.length > 0){
    sc.className = 'card hero alert'; $('statusIcon').textContent = '💌';
    $('statusTitle').textContent = 'New message from ' + name + '!';
    $('statusSub').textContent = active.length === 1 ? 'Scroll down to read and reply.' : 'You have ' + active.length + ' new messages below.';
  } else if (d.last_checkin_time){
    sc.className = 'card hero ok'; $('statusIcon').textContent = '✅';
    $('statusTitle').textContent = name + ' checked in';
    $('statusSub').textContent = 'Last heard: ' + d.last_checkin_friendly;
  } else {
    sc.className = 'card hero'; $('statusIcon').textContent = '📻';
    $('statusTitle').textContent = 'Waiting for check-in…';
    $('statusSub').textContent = 'The monitor is watching for messages.';
  }
  $('headerSub').textContent = d.last_checkin_time ? 'Last heard: ' + d.last_checkin_friendly : 'Waiting for messages…';

  // --- browser notifications for brand-new alerts ---
  for (const a of d.pending){
    if (!seenIds.has(a.id) && !dismissedIds.has(a.id)){
      seenIds.add(a.id);
      if ('Notification' in window && Notification.permission === 'granted')
        new Notification('Message from ' + (a.from_name || 'Someone'), {body: a.message || a.subject || 'New check-in', requireInteraction: true});
    }
  }
  for (const a of d.history){ if (a.type === 'system' && !seenIds.has(a.id)){ seenIds.add(a.id); toast('✓ ' + (a.message || 'Message delivered')); } }

  // --- APRS delivery confirmation toast ---
  if (d.aprs_last_ack){
    const k = d.aprs_last_ack.from + '_' + d.aprs_last_ack.msgno;
    if (!window._ackInit){ window._ackInit = true; window._lastAck = k; }
    else if (k !== window._lastAck){ window._lastAck = k; toast('✓ Delivered to ' + d.aprs_last_ack.from + ' (msg #' + d.aprs_last_ack.msgno + ')'); }
  } else window._ackInit = true;

  last = d; render();
  if (currentTab === 'map' && _map) updateMapPosition();
  if (d.update) showUpdateBanner(d.update);
}

/* ---------- updates ---------- */
let _updPollTimer = null;
function _skippedTag(){ try { return localStorage.getItem('hamlink_update_skipped') || ''; } catch(e){ return ''; } }
function showUpdateBanner(u){
  const on = !!(u && u.available && u.latest && u.latest !== _skippedTag() && u.stage !== 'restarting');
  if (on){ $('updText').textContent = 'HamLink ' + u.latest + ' is available (you have v' + (u.current || '?') + ')'; $('updLink').href = u.url || '#'; }
  show('updateBar', on);
}
function skipUpdate(){ const u = last && last.update; if (u && u.latest){ try { localStorage.setItem('hamlink_update_skipped', u.latest); } catch(e){} } show('updateBar', false); }
function openUpdate(){
  const u = (last && last.update) || {};
  $('updTitle').textContent = 'Update to HamLink ' + (u.latest || '');
  $('updIntro').textContent = 'You are running v' + (u.current || '?') + '.' + (u.published ? ' Released ' + fmtTime(u.published) + '.' : '');
  $('updNotes').textContent = (u.notes || '').replace(/^#+\s*/gm, '').trim() || 'No release notes.';
  const how = {
    exe: 'The new HamLink_Radio.exe will be downloaded and swapped in. Your config.json, message log, and map tiles are kept. HamLink restarts when done.',
    source: 'monitor.py and the support files will be replaced (the old monitor.py is kept as monitor.py.bak). Your config.json, message log, and map tiles are kept. HamLink restarts when done.',
    git: 'This copy is a git checkout. Update it with git pull instead of the in-app updater.',
  };
  $('updHow').textContent = how[u.install_kind] || how.source;
  $('updGo').disabled = u.install_kind === 'git' || !u.asset_url;
  show('updProgressWrap', false); show('updActions', true);
  $('updateModal').classList.add('open');
}
function closeUpdate(){ $('updateModal').classList.remove('open'); if (_updPollTimer){ clearInterval(_updPollTimer); _updPollTimer = null; } }
async function startUpdate(){
  $('updGo').disabled = true;
  try {
    const r = await cpost('/api/update/apply'); const d = await r.json();
    if (!d.ok){ toast(d.error || 'Could not start update', true); $('updGo').disabled = false; return; }
  } catch(e){ toast('Could not start update: ' + e, true); $('updGo').disabled = false; return; }
  show('updActions', false); show('updProgressWrap', true);
  $('updProgress').className = 'progress indet'; $('updStatus').textContent = 'Starting…';
  _updPollTimer = setInterval(pollUpdateProgress, 1000);
}
async function pollUpdateProgress(){
  let u;
  try { const r = await fetch('/api/update/status'); u = (await r.json()).update; }
  catch(e){ // server is restarting — wait for it to come back, then reload
    $('updStatus').textContent = 'HamLink is restarting… waiting for it to come back.';
    waitForRestart(); return;
  }
  const m = /(\d+)%/.exec(u.progress || '');
  const bar = $('updProgress');
  if (m){ bar.className = 'progress'; bar.firstElementChild.style.width = m[1] + '%'; } else bar.className = 'progress indet';
  $('updStatus').textContent = u.progress || u.stage;
  if (u.stage === 'error'){ clearInterval(_updPollTimer); _updPollTimer = null; bar.className = 'progress'; show('updActions', true); $('updGo').disabled = false; }
  if (u.stage === 'done'){ clearInterval(_updPollTimer); _updPollTimer = null; bar.className = 'progress'; bar.firstElementChild.style.width = '100%'; show('updActions', true); $('updGo').disabled = true; }
  if (u.stage === 'restarting'){ clearInterval(_updPollTimer); _updPollTimer = null; setTimeout(waitForRestart, 2500); }
}
function waitForRestart(){
  if (_updPollTimer) return;
  let tries = 0;
  _updPollTimer = setInterval(async () => {
    tries++;
    try { const r = await fetch('/api/version', {cache: 'no-store'}); const d = await r.json();
      if (d.ok){ clearInterval(_updPollTimer); _updPollTimer = null; $('updStatus').textContent = 'Back online with v' + d.version + '. Reloading…'; setTimeout(() => location.reload(), 800); } }
    catch(e){ $('updStatus').textContent = 'Waiting for HamLink to restart… (' + tries * 2 + 's)'; }
    if (tries > 90){ clearInterval(_updPollTimer); _updPollTimer = null; $('updStatus').textContent = 'HamLink did not come back on its own. Start it again from start_hamlink.bat or HamLink_Radio.exe, then reload this page.'; }
  }, 2000);
}
async function checkUpdateNow(fromFooter){
  const tr = $('updTr');
  if (!fromFooter){ tr.className = 'tr ok'; tr.textContent = 'Checking…'; } else toast('Checking for updates…');
  try {
    const r = await cpost('/api/update/check'); const d = await r.json(); const u = d.update || {};
    if (last) last.update = u;
    if (u.error){ tr.className = 'tr err'; tr.textContent = u.error; if (fromFooter) toast(u.error, true); }
    else if (u.available){ tr.className = 'tr ok'; tr.textContent = 'Update available: ' + u.latest; try { localStorage.removeItem('hamlink_update_skipped'); } catch(e){} showUpdateBanner(u); if (fromFooter) toast('HamLink ' + u.latest + ' is available'); }
    else { tr.className = 'tr ok'; tr.textContent = 'You are up to date (v' + u.current + ').'; if (fromFooter) toast('You are up to date (v' + u.current + ')'); }
    _updateInfoLine(u);
  } catch(e){ tr.className = 'tr err'; tr.textContent = 'Check failed: ' + e; }
}
function _updateInfoLine(u){
  u = u || {};
  let s = 'Running v' + (u.current || '?') + (u.install_kind ? ' (' + u.install_kind + ' install)' : '') + '.';
  if (u.checked_at) s += ' Last checked ' + fmtTime(u.checked_at) + '.';
  if (u.latest) s += ' Latest release: ' + u.latest + '.';
  $('updInfo').textContent = s;
  show('updInstallBtn', !!u.available && u.install_kind !== 'git');
}

/* ---------- message lists ---------- */
function render(){
  const d = last; if (!d) return;
  const alerting = d.pending.filter(a => !dismissedIds.has(a.id));
  const ringing = alerting.length > 0 && !d.alarm_silenced;
  if (ringing) startAlarm(alerting[alerting.length - 1].from_name || ''); else stopAlarm();
  show('heroActions', d.pending.length > 0); show('btnSilence', ringing);
  $('btnCloseAll').textContent = d.pending.length > 1 ? '✓ Mark all ' + d.pending.length + ' as read' : '✓ Mark as read';

  const active = [...d.pending].reverse();
  const cnt = $('activeCount'); cnt.textContent = active.length; show('activeCount', active.length > 0);
  const html = active.length ? active.map(a => card(a, true)).join('') : '<div class="empty"><div class="icon">📭</div><p>No new messages</p></div>';
  if (html !== _lastActiveHtml){ $('activeArea').innerHTML = html; _lastActiveHtml = html; }   // only touch the DOM when something changed

  const pendingIds = new Set(d.pending.map(a => a.id));
  const h = d.history.filter(a => !pendingIds.has(a.id)).reverse().slice(0, 50);
  const hh = h.length ? h.map(a => card(a, false)).join('') : '<div class="empty"><div class="icon">📋</div><p>No messages yet</p></div>';
  if (hh !== _lastHistHtml){ $('histArea').innerHTML = hh; _lastHistHtml = hh; }
}

const CHAN = {vmail:'VarAC', aprs:'APRS', winlink:'Winlink', relay:'Relay'};
function card(a, isActive){
  const dismissed = dismissedIds.has(a.id);
  const name = a.from_name || a.from_call || 'Unknown';
  const id = esc(a.id);

  if (a.type === 'system'){
    return '<article class="card msg ch-system"><div class="msg-head"><div class="msg-from"><span class="chip sys">✓ Delivery confirmation</span></div>'
      + '<div class="msg-time">' + esc(a.friendly_time) + '</div></div><div class="msg-body">' + esc(a.message) + '</div></article>';
  }
  if (a.type === 'sent'){
    const ch = (a.channel || '').toUpperCase();
    let status = '';
    if (a.delivered) status += '<div class="msg-status ok">✓ Delivered to ' + esc(a.delivered_by || '') + '</div>';
    if (a.mail_bot_confirmed) status += '<div class="msg-status info">📬 Stored in APRS mailbox</div>';
    const body = (a.message || '').replace(/^\[(APRS|Winlink|VarAC)\]\s*/i, '');
    const toName = cfg.operator_name || a.to_callsign || '';
    return '<article class="card msg ch-sent' + (a.delivered ? ' delivered' : '') + '"><div class="msg-head"><div class="msg-from"><span class="chip sent">You → ' + esc(toName) + '</span>'
      + (ch ? '<span class="chip ' + esc(a.channel) + '">' + esc(ch) + '</span>' : '') + '</div><div class="msg-time">' + esc(fmtTime(a.time)) + '</div></div>'
      + '<div class="msg-body">' + esc(body) + '</div>' + status + '</article>';
  }

  let chips = '';
  if (isActive && !dismissed) chips += '<span class="chip new">New</span>';
  if (a.urgent) chips += '<span class="chip urgent">Urgent</span>';
  chips += '<span class="chip ' + esc(a.type) + '">' + (CHAN[a.type] || esc(a.type)) + '</span>';

  const subj = a.subject ? '<div class="msg-subject">' + esc(a.subject) + '</div>' : '';
  let body = '';
  if (a.type === 'relay') body = relayBody(a);
  else if (a.message) body = '<div class="msg-body">' + esc(a.message) + '</div>';
  let meta = '';
  if (a.type === 'vmail' && (a.band || a.snr || a.via)){
    const bits = []; if (a.via) bits.push('via ' + esc(a.via)); if (a.band) bits.push(esc(a.band)); if (a.snr) bits.push('SNR ' + esc(a.snr) + ' dB');
    meta = '<div class="msg-meta">' + bits.join(' · ') + '</div>';
  }

  const replyChan = a.type === 'aprs' ? 'aprs' : a.type === 'winlink' ? 'winlink' : 'varac';
  const cls = 'card msg ch-' + esc(a.type) + (isActive && !dismissed ? ' unread' : '') + (a.urgent ? ' urgent' : '');
  let actions = '';
  if (isActive){
    actions = '<div class="msg-actions">'
      + (!dismissed ? '<button class="btn sm" data-act="dismiss" data-id="' + id + '" title="Stop the alarm but keep this message here so you can reply">🔕 Dismiss alert</button>' : '')
      + (a.type !== 'relay' ? '<button class="btn sm primary" data-act="reply" data-chan="' + replyChan + '" data-to="' + esc(a.from_call || '') + '" title="Answer this message on the same channel">💬 Reply</button>' : '')
      + '<button class="btn sm ghost" data-act="close" data-id="' + id + '" title="File this message under Previous Messages">✕ Close</button>'
      + (a.type === 'vmail' ? '<button class="btn sm danger" data-act="delete" data-id="' + id + '" title="Permanently delete this VMail from VarAC.db">🗑 Delete</button>' : '')
      + '</div>';
  }
  const fromLine = a.type === 'relay' ? 'Relay station ' + esc(a.relay_station || name) : '📨 ' + esc(name) + ' → You';
  return '<article class="' + cls + '"><div class="msg-head"><div class="msg-from">' + chips + fromLine + '</div>'
    + '<div class="msg-time">' + esc(a.friendly_time) + '</div></div>' + subj + body + meta + actions + '</article>';
}

function relayBody(a){
  const rs = a.relay_station || 'unknown', rt = (window._relayTracking || {})[rs.toUpperCase()] || {}, st = rt.status || '';
  const rsq = esc(rs), fq = esc(a.frequency_mhz || '');
  let txt = 'Station <b>' + rsq + '</b> is holding a message for you' + (a.frequency_mhz ? ' on ' + fq + ' MHz' : '') + '.';
  let status = '';
  if (st === 'queued') status = '<div class="msg-status info">⏳ Auto-retrieval queued</div>';
  else if (st === 'retrieving') status = '<div class="msg-status warn">📡 Connecting to relay…</div>';
  else if (st === 'retrieved') status = '<div class="msg-status ok">✓ Retrieved</div>';
  else if (st === 'failed') status = '<div class="msg-status err">✕ Retrieval failed' + (rt.error ? ' — ' + esc(rt.error) : '') + '</div><div class="msg-actions"><button class="btn sm" data-act="relay-retry" data-station="' + rsq + '">Retry</button></div>';
  else if (st === 'confirmed_wait') status = '<div class="msg-status warn">⚠ Awaiting your approval</div><div class="msg-actions"><button class="btn sm primary" data-act="relay-approve" data-station="' + rsq + '">Approve retrieval</button><button class="btn sm ghost" data-act="relay-dismiss" data-station="' + rsq + '">Not now</button></div>';
  else status = '<div class="msg-actions"><button class="btn sm primary" data-act="relay-retrieve" data-station="' + rsq + '" data-freq="' + fq + '">Retrieve now</button></div>';
  return '<div class="msg-body">' + txt + '</div>' + status;
}

/* One delegated click handler for every message-card button. */
document.addEventListener('click', e => {
  const b = e.target.closest('[data-act]'); if (!b) return;
  const id = b.dataset.id, st = b.dataset.station;
  switch (b.dataset.act){
    case 'dismiss': dismiss(id); break;
    case 'close': closeMessage(id); break;
    case 'delete': deleteVmail(id); break;
    case 'reply': openReply(b.dataset.chan, b.dataset.to); break;
    case 'relay-approve': relayAction('/api/relay/approve', {relay_station: st}, 'Relay retrieval approved for ' + st); break;
    case 'relay-dismiss': relayAction('/api/relay/dismiss', {relay_station: st}, 'Relay dismissed: ' + st); break;
    case 'relay-retry': relayAction('/api/relay/retry', {relay_station: st}, 'Relay retry queued: ' + st); break;
    case 'relay-retrieve': relayAction('/api/relay/retrieve', {relay_station: st, frequency_mhz: b.dataset.freq || ''}, 'Relay retrieval queued: ' + st); break;
  }
});
function relayAction(url, body, okMsg){
  cpost(url, body).then(r => r.json()).then(d => { if (d.ok){ toast(okMsg); poll(); } else toast(d.error || 'Relay action failed', true); })
    .catch(e => toast('Relay error: ' + e, true));
}

/* Silence = stop the browser sound AND the PC speaker, everywhere, right now.
   Messages stay in "New Messages" so they can still be read and answered. */
function silenceAll(){
  stopAlarm();
  if (last) last.alarm_silenced = true;   // so the next render() doesn't restart it before the poll catches up
  cpost('/api/stop_alarm');
  render();
}
function _afterLocalChange(){ silenceAll(); }
function dismiss(id){ dismissedIds.add(id); silenceAll(); }              // soft: silence, keep in New Messages
function dismissAll(){ if (last) last.pending.forEach(a => dismissedIds.add(a.id)); silenceAll(); }
function closeAll(){                                                    // hard: acknowledge everything server-side
  if (last){ last.pending.forEach(a => dismissedIds.add(a.id)); last.pending = []; }
  cpost('/api/acknowledge_all'); silenceAll();
}
function closeMessage(id){                                                // hard: acknowledge server-side
  dismissedIds.add(id); cpost('/api/acknowledge', {id});
  if (last) last.pending = last.pending.filter(a => a.id !== id);
  _afterLocalChange();
}
function deleteVmail(id){
  if (!confirm('Permanently delete this message from VarAC.db? It will not reappear in VarAC or HamLink.')) return;
  dismissedIds.add(id);
  cpost('/api/delete_vmail', {id}).then(r => r.json()).then(d => { if (!d.ok) toast(d.error || 'Delete failed', true); });
  if (last){ last.pending = last.pending.filter(a => a.id !== id); last.history = last.history.filter(a => a.id !== id); }
  _afterLocalChange();
}

/* ---------- compose ---------- */
function openReply(chan, replyTo){ _replyToCall = replyTo || ''; $('composeTitle').textContent = '💬 Reply'; _openComposeBox(chan); }
function openCompose(chan){ _replyToCall = ''; const n = cfg.operator_name || ''; $('composeTitle').textContent = n ? '✉️ Send Message to ' + n : '✉️ Send Message'; _openComposeBox(chan); }
function closeReply(){ show('replyBox', false); }
function setQuickReply(txt){ $('replyText').value = txt; onComposeInput(); $('replyText').focus(); }

function _channelState(){
  const d = last || {}, c = d.config || {};
  return {
    aprsOn: !!(c.aprs && c.aprs.enabled && (d.aprs_connected || d.kiss_connected)),
    wlOn: !!(c.pat && c.pat.enabled),
    varacOn: !!(c.varac_db_path && d.db_connected),
    aprsRf: !!(d.kiss_connected && !d.internet_up),
    wlRf: !!(c.pat && c.pat.rf_fallback && !d.internet_up),
    inet: !!d.internet_up,
  };
}
function _openComposeBox(chan){
  _composeChan = chan; aprsManualUncheck = false;
  $('replyText').value = ''; $('replyStatus').textContent = ''; $('charCounter').textContent = '';
  const s = _channelState();
  $('chkAprs').checked = s.aprsOn && (chan === 'multi' || chan === 'aprs');
  $('chkWinlink').checked = s.wlOn && (chan === 'multi' || chan === 'winlink');
  $('chkVarac').checked = s.varacOn && (chan === 'multi' || chan === 'varac');
  show('chkAprsLabel', s.aprsOn); show('chkWinlinkLabel', s.wlOn); show('chkVaracLabel', s.varacOn);
  const ap = $('aprsPathLabel'); ap.textContent = s.aprsRf ? '(RF — no internet)' : !s.inet ? '(no internet)' : '(internet)'; ap.className = 'path' + (s.aprsRf || !s.inet ? ' rf' : '');
  const wp = $('wlPathLabel'); wp.textContent = s.wlRf ? '(RF — no internet)' : '(internet)'; wp.className = 'path' + (s.wlRf ? ' rf' : '');
  // relay routing hint for VarAC replies
  const toCall = (_replyToCall || '').toUpperCase(); let via = '';
  if (cfg.relay && cfg.relay.route_replies_via_relay && toCall) via = ((window._relayData || {}).relay_paths || {})[toCall] || '';
  $('relayViaIndicator').innerHTML = via ? '📡 VarAC reply will be routed via relay <b>' + esc(via) + '</b>' : ''; show('relayViaIndicator', !!via);
  updateRfWarning();
  $('chkAprs').onchange = function(){ aprsManualUncheck = !this.checked; updateRfWarning(); };
  $('chkWinlink').onchange = updateRfWarning; $('chkVarac').onchange = updateRfWarning;
  $('quickBtns').innerHTML = (cfg.quick_replies || []).map(q => '<button type="button" data-q="' + esc(q) + '">' + esc(q) + '</button>').join('');
  $('quickBtns').querySelectorAll('button').forEach(b => b.onclick = () => setQuickReply(b.dataset.q));
  show('replyBox', true);
  setTimeout(() => { $('replyBox').scrollIntoView({behavior:'smooth', block:'center'}); $('replyText').focus(); }, 80);
}
function updateRfWarning(){
  const s = _channelState();
  const anyRf = ($('chkAprs').checked && s.aprsRf) || ($('chkWinlink').checked && s.wlRf);   // VarAC never transmits at send time
  show('rfWarning', anyRf);
}
function onComposeInput(){
  const t = $('replyText').value, over = t.length > 67, cc = $('charCounter');
  const aprsSel = !$('chkAprsLabel').classList.contains('hidden');
  cc.textContent = aprsSel ? t.length + '/67 for APRS' + (over ? ' — too long, APRS will be skipped' : '') : (t.length ? t.length + ' chars' : '');
  cc.className = 'counter' + (over && aprsSel ? ' over' : '');
  if (_composeChan === 'multi' || _composeChan === 'aprs'){
    const chk = $('chkAprs');
    if (over){ if (!aprsManualUncheck) chk.checked = false; }
    else if (!aprsManualUncheck) chk.checked = _channelState().aprsOn;
    updateRfWarning();
  }
}
function confirmAndSend(){
  const s = _channelState(), nc = [];
  if ($('chkAprs').checked && s.aprsRf) nc.push('APRS via Soundmodem');
  if ($('chkWinlink').checked && s.wlRf) nc.push('Winlink via VARA FM');
  if (nc.length){
    $('ncRfDetails').textContent = 'The following selected channel' + (nc.length > 1 ? 's' : '') + ' will transmit via RF with bandwidth exceeding 500 Hz: ' + nc.join(', ') + '.';
    $('rfNonCompliantModal').classList.add('open');
  } else sendMulti();
}
function cancelNcRfSend(){ $('rfNonCompliantModal').classList.remove('open'); }
function proceedNcRf(){ $('rfNonCompliantModal').classList.remove('open'); sendMulti(); }

async function sendMulti(){
  const msg = $('replyText').value.trim(); if (!msg) return;
  const sendAprs = $('chkAprs').checked, sendWl = $('chkWinlink').checked, sendVarac = $('chkVarac').checked;
  const st = $('replyStatus');
  if (!sendAprs && !sendWl && !sendVarac){ st.textContent = 'Select at least one channel'; st.style.color = 'var(--red)'; return; }
  $('sendBtn').disabled = true; st.textContent = 'Sending…'; st.style.color = '';
  const results = [];
  const go = async (label, url, body) => {
    try { const r = await cpost(url, body); const d = await r.json(); results.push(d.ok ? label + ' ✓' : label + ': ' + (d.error || 'failed')); }
    catch(e){ results.push(label + ': error'); }
  };
  if (sendAprs) await go('APRS', '/api/aprs_reply', {message: msg.substring(0, 67), to_callsign: _replyToCall});
  if (sendWl) await go('Winlink', '/api/winlink_reply', {message: msg, subject: 'Message'});
  if (sendVarac) await go('VarAC', '/api/reply', {message: msg, subject: 'Message'});
  $('sendBtn').disabled = false;
  const allOk = results.every(r => r.endsWith('✓'));
  if (allOk){ $('replyText').value = ''; st.textContent = ''; toast('Sent: ' + results.join(', ')); closeReply(); dismissAll(); }
  else { st.textContent = results.join(' · '); st.style.color = 'var(--red)'; }
}

/* ---------- sitrep ---------- */
async function openSitrep(){
  const res = $('sitrepResult'); res.className = 'sr-result'; res.textContent = '';
  try {
    const r = await fetch('/api/sitrep/latest'); const d = await r.json();
    $('sitrepNum').textContent = String(d.ok ? (d.number || 0) + 1 : 1).padStart(3, '0');
    if (d.fields){
      const f = d.fields;
      $('srHouse').value = f.house || 'All OK'; $('srVehicles').value = f.vehicles || 'All OK'; $('srUtilities').value = f.utilities || 'All OK';
      $('srHealth').value = f.health || 'All OK'; $('srNeeds').value = f.needs || 'None'; $('srRelocation').value = f.relocation || 'No change';
      $('srLocation').value = f.location || ''; $('srRemarks').value = '';
    }
  } catch(e){}
  $('sitrepOverlay').classList.add('open');
}
function closeSitrep(){ $('sitrepOverlay').classList.remove('open'); }
async function sendSitrep(){
  const v = id => $(id).value.trim();
  const body = {house: v('srHouse') || 'All OK', vehicles: v('srVehicles') || 'All OK', utilities: v('srUtilities') || 'All OK',
    health: v('srHealth') || 'All OK', needs: v('srNeeds') || 'None', relocation: v('srRelocation') || 'No change',
    location: v('srLocation'), remarks: v('srRemarks')};
  const res = $('sitrepResult'); res.className = 'sr-result ok'; res.textContent = 'Posting…';
  try {
    const r = await cpost('/api/sitrep', body); const d = await r.json();
    if (d.ok){
      let m = 'SITREP #' + String(d.number).padStart(3, '0') + ' posted to BBS: ' + d.filename;
      if (d.varac_broadcast_sent) m += '\nVarAC broadcast sent: ' + d.varac_broadcast_msg; else if (d.varac_broadcast_msg) m += '\nVarAC broadcast failed (is VarAC running?)';
      res.className = 'sr-result ok'; res.textContent = m;
    } else { res.className = 'sr-result err'; res.textContent = 'Error: ' + (d.error || 'Unknown'); }
  } catch(e){ res.className = 'sr-result err'; res.textContent = 'Error: ' + e.message; }
}
async function sendQuickSitrep(){
  $('srHouse').value = 'All OK'; $('srVehicles').value = 'All OK'; $('srUtilities').value = 'All OK'; $('srHealth').value = 'All OK';
  $('srNeeds').value = 'None'; $('srRelocation').value = 'No change'; $('srLocation').value = ''; $('srRemarks').value = '';
  await sendSitrep();
}

/* ---------- settings ---------- */
const setOn = (id, on) => $(id).classList.toggle('on', !!on);
const isOn = id => $(id).classList.contains('on');
const num = (id, def) => { const n = parseInt($(id).value); return isNaN(n) ? def : n; };
const val = id => $(id).value.trim();

async function openSett(){ await fillForm(); loadPatConfig(); $('settOverlay').classList.add('open'); }
function closeSett(){ $('settOverlay').classList.remove('open'); $('fb').classList.remove('open'); }
document.addEventListener('keydown', e => { if (e.key === 'Escape'){ closeSett(); closeSitrep(); cancelNcRfSend(); if (!_updPollTimer) closeUpdate(); } });

function _stateBadges(c){
  const set = (id, on) => { const el = $(id); el.textContent = on ? 'On' : 'Off'; el.classList.toggle('on', !!on); };
  set('stVarac', !!c.varac_db_path); set('stPo', !!(c.pushover && c.pushover.enabled)); set('stAprs', !!(c.aprs && c.aprs.enabled));
  set('stSm', !!(c.soundmodem && c.soundmodem.enabled)); set('stBcn', !!(c.beacon && c.beacon.enabled));
  set('stRelay', !!(c.relay && c.relay.enabled)); set('stPat', !!(c.pat && c.pat.enabled));
  set('stUpd', !(c.updates && c.updates.auto_check === false));
  set('stMap', !!(c.map_file || c.map_state));
}
async function fillForm(){
  try { const r = await fetch('/api/status'); const d = await r.json(); cfg = d.config || cfg; } catch(e){}
  const c = cfg;
  $('cName').value = c.operator_name || ''; $('cHomeCall').value = c.home_callsign || ''; $('cWatch').value = (c.watch_callsigns || []).join(', ');
  $('cVaracExe').value = c.varac_exe_path || ''; $('cVaracProfile').value = c.varac_profile || ''; $('cBbsDir').value = c.bbs_directory || '';
  $('bbsResolved').textContent = c.bbs_directory_resolved ? 'Resolved: ' + c.bbs_directory_resolved : '';
  $('cDb').value = c.varac_db_path || ''; $('cPoll').value = c.poll_interval_seconds || 15;
  $('cSound').value = c.alert_sound || 'gentle';
  const vp = Math.round((typeof c.alert_volume === 'number' ? c.alert_volume : 0.3) * 100); $('cVol').value = vp; $('volL').textContent = vp + '%';
  $('cAlarmTimeout').value = typeof c.alarm_timeout_minutes === 'number' ? c.alarm_timeout_minutes : 15;
  $('cQuick').value = (c.quick_replies || []).join('\n');
  const po = c.pushover || {};
  setOn('cPoOn', po.enabled); $('cPoUser').value = po.user_key || ''; $('cPoToken').value = po.api_token || '';
  $('cPoPri').value = String(po.priority == null ? 1 : po.priority); $('cPoSnd').value = po.sound || 'pushover'; setOn('cPoQuickReplies', po.quick_replies);
  const ap = c.aprs || {};
  setOn('cAprsOn', ap.enabled); $('cAprsSsid').value = ap.home_ssid || '-5'; $('cAprsTravSsid').value = ap.traveler_ssids || ap.traveler_ssid || '-7';
  $('cAprsPass').value = ap.passcode || ''; $('cAprsSrv').value = ap.server || 'rotate.aprs2.net'; $('cAprsPort').value = ap.port || 14580;
  setOn('cAprsRfFallback', ap.rf_fallback); setOn('cAprsMailbox', ap.use_mailbox); $('cAprsFiKey').value = ap.aprs_fi_api_key || '';
  const b = c.beacon || {};
  if (!$('cMapLat').value && b.lat && b.lon){ $('cMapLat').value = b.lat; $('cMapLon').value = b.lon; }
  if (!$('cMapName').value) $('cMapName').value = 'home-area';
  loadMapList(); mapEstimate();
  document.querySelectorAll('.tr').forEach(t => { if (!/^(dbTr|poTr|updTr)$/.test(t.id)) t.className = 'tr'; });
  if (last){ $('cfgPath').textContent = last.config_path || 'config.json'; $('cfgSaved').textContent = last.config_saved_at ? 'Last saved ' + fmtTime(last.config_saved_at) : 'Loaded from disk at startup'; }
  show('saveWarnings', false);
  updateCallPreview();
  setOn('cBcnOn', b.enabled); $('cBcnLat').value = b.lat || ''; $('cBcnLon').value = b.lon || '';
  $('cBcnSymbol').value = (b.symbol_table || '/') + (b.symbol_code || '-') + ' ';
  $('cBcnInterval').value = b.interval_minutes || 30; $('cBcnComment').value = b.comment || 'HamLink Radio';
  setOn('cBcnAprsIs', b.via_aprsis !== false); setOn('cBcnRf', b.via_rf);
  const rl = c.relay || {};
  setOn('cRelayOn', rl.enabled); setOn('cRelayAutoRetrieve', rl.auto_retrieve); setOn('cRelayConfirm', rl.confirm_before_connect !== false);
  setOn('cRelayRouteReplies', rl.route_replies_via_relay !== false); $('cRelayCooldown').value = rl.cooldown_seconds || 300;
  $('cRelayDelay').value = rl.auto_retrieve_delay_seconds == null ? 10 : rl.auto_retrieve_delay_seconds; $('cRelayMaxRetries').value = rl.max_retries == null ? 2 : rl.max_retries;
  $('cRelayIgnore').value = (rl.ignore_stations || []).join(', ');
  const sm = c.soundmodem || {};
  setOn('cSmOn', sm.enabled); $('cSmPath').value = sm.exe_path || ''; $('cSmHost').value = sm.kiss_host || '127.0.0.1'; $('cSmPort').value = sm.kiss_port || 8100;
  updateBcnRfAvail();
  const pt = c.pat || {};
  setOn('cPatOn', pt.enabled); $('cPatPath').value = pt.exe_path || ''; $('cPatAddr').value = pt.http_addr || 'localhost:8080';
  $('cPatPoll').value = pt.poll_interval || 30; $('cPatHomeTac').value = pt.home_tactical || ''; $('cPatTravTac').value = pt.traveler_tactical || '';
  setOn('cPatPosReports', pt.position_reports); setOn('cPatRfFallback', pt.rf_fallback); $('cPatRfPoll').value = pt.rf_poll_interval || 10800;
  $('cPatRfGw').value = pt.rf_gateway || ''; $('cPatVaraAddr').value = pt.varafm_addr || 'localhost:8300'; $('cPatVaraExe').value = pt.varafm_exe_path || '';
  const up = c.updates || {};
  setOn('cUpdOn', up.auto_check !== false); $('cUpdHours').value = up.interval_hours || 6; $('cUpdToken').value = up.github_token || '';
  $('updTr').className = 'tr'; _updateInfoLine(last && last.update);
  _stateBadges(c);
}
async function saveSett(){
  const p = {
    operator_name: val('cName'), home_callsign: val('cHomeCall').toUpperCase(),
    watch_callsigns: val('cWatch').split(',').map(s => s.trim().toUpperCase()).filter(Boolean),
    varac_exe_path: val('cVaracExe'), varac_profile: val('cVaracProfile'), bbs_directory: val('cBbsDir'), varac_db_path: val('cDb'),
    poll_interval_seconds: num('cPoll', 15), alert_sound: $('cSound').value, alert_volume: num('cVol', 30) / 100, alarm_timeout_minutes: num('cAlarmTimeout', 15),
    quick_replies: $('cQuick').value.split('\n').map(s => s.trim()).filter(Boolean),
    pushover: {enabled: isOn('cPoOn'), user_key: val('cPoUser'), api_token: val('cPoToken'), priority: num('cPoPri', 1), sound: $('cPoSnd').value, quick_replies: isOn('cPoQuickReplies')},
    aprs: {enabled: isOn('cAprsOn'), home_ssid: val('cAprsSsid') || '-5', traveler_ssids: val('cAprsTravSsid') || '-7', passcode: val('cAprsPass'),
      server: val('cAprsSrv') || 'rotate.aprs2.net', port: num('cAprsPort', 14580), rf_fallback: isOn('cAprsRfFallback'), use_mailbox: isOn('cAprsMailbox'), aprs_fi_api_key: val('cAprsFiKey')},
    beacon: {enabled: isOn('cBcnOn'), lat: parseFloat($('cBcnLat').value) || 0, lon: parseFloat($('cBcnLon').value) || 0,
      symbol_table: $('cBcnSymbol').value.charAt(0), symbol_code: $('cBcnSymbol').value.charAt(1), comment: val('cBcnComment') || 'HamLink Radio',
      interval_minutes: num('cBcnInterval', 30), via_aprsis: isOn('cBcnAprsIs'), via_rf: isOn('cBcnRf')},
    relay: {enabled: isOn('cRelayOn'), auto_retrieve: isOn('cRelayAutoRetrieve'), confirm_before_connect: isOn('cRelayConfirm'), route_replies_via_relay: isOn('cRelayRouteReplies'),
      cooldown_seconds: num('cRelayCooldown', 300), auto_retrieve_delay_seconds: num('cRelayDelay', 10), max_retries: num('cRelayMaxRetries', 2),
      ignore_stations: val('cRelayIgnore').split(',').map(s => s.trim().toUpperCase()).filter(Boolean)},
    soundmodem: {enabled: isOn('cSmOn'), exe_path: val('cSmPath'), kiss_host: val('cSmHost') || '127.0.0.1', kiss_port: num('cSmPort', 8100), auto_launch: true},
    pat: {enabled: isOn('cPatOn'), exe_path: val('cPatPath'), http_addr: val('cPatAddr') || 'localhost:8080', auto_launch: true, poll_interval: num('cPatPoll', 30),
      home_tactical: val('cPatHomeTac').toUpperCase(), traveler_tactical: val('cPatTravTac').toUpperCase(), position_reports: isOn('cPatPosReports'),
      rf_fallback: isOn('cPatRfFallback'), rf_poll_interval: num('cPatRfPoll', 10800), rf_gateway: val('cPatRfGw').toUpperCase(),
      varafm_addr: val('cPatVaraAddr') || 'localhost:8300', varafm_exe_path: val('cPatVaraExe')},
    updates: {auto_check: isOn('cUpdOn'), interval_hours: num('cUpdHours', 6), github_token: val('cUpdToken')},
  };
  try {
    const r = await cpost('/api/config', p); const d = await r.json();
    if (!d.ok){ toast(d.error || 'Save failed', true); return; }
    closeSett(); poll();
    showSavedModal(d.path, d.saved_at, d.warnings || []);
  } catch(e){ toast('Save failed: ' + e, true); }
}
function showSavedModal(path, when, warnings){
  $('savedPath').textContent = path || 'config.json';
  $('savedTime').textContent = 'Saved ' + (when ? fmtTime(when) : 'just now') + '. The new settings are active now; VarAC, Pat, and Soundmodem are started if they were just enabled.';
  const ul = $('savedWarnList'); ul.innerHTML = '';
  (warnings || []).forEach(w => { const li = document.createElement('li'); li.className = w.level; li.textContent = (w.level === 'error' ? '⛔ ' : w.level === 'warn' ? '⚠️ ' : 'ℹ️ ') + w.message; ul.appendChild(li); });
  show('savedWarnWrap', warnings && warnings.length > 0); show('savedAllGood', !warnings || warnings.length === 0);
  $('savedModal').classList.add('open');
  toast('✓ Settings saved to ' + (path || 'config.json').split(/[\\/]/).pop());
}
async function checkSettings(){
  const box = $('saveWarnings');
  try {
    const r = await cpost('/api/test', {what: 'validate'}); const d = await r.json();
    const w = d.warnings || [];
    if (!w.length){ box.className = 'notice blue'; box.textContent = '✓ No problems found in the saved settings. (Unsaved edits on this page are not checked — press Save first.)'; }
    else { box.className = 'notice warn'; box.innerHTML = '<b>Saved settings — things worth checking:</b><ul style="margin:6px 0 0 18px">' + w.map(x => '<li>' + (x.level === 'error' ? '⛔ ' : x.level === 'warn' ? '⚠️ ' : 'ℹ️ ') + esc(x.message) + '</li>').join('') + '</ul>'; }
    show('saveWarnings', true); box.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  } catch(e){ toast('Check failed: ' + e, true); }
}
async function runTest(what, trId, fields){
  const el = $(trId); el.className = 'tr ok'; el.textContent = 'Testing…';
  try {
    const r = await cpost('/api/test', Object.assign({what}, fields || {})); const d = await r.json();
    el.className = d.ok ? 'tr ok' : 'tr err'; el.textContent = (d.ok ? '✓ ' : '✕ ') + (d.message || d.error || (d.ok ? 'OK' : 'Failed'));
  } catch(e){ el.className = 'tr err'; el.textContent = 'Test failed: ' + e; }
}
function copyCfgPath(){
  const p = $('cfgPath').textContent;
  if (navigator.clipboard) navigator.clipboard.writeText(p).then(() => toast('Path copied'), () => toast(p));
  else toast(p);
}
function openFolder(){ cpost('/api/open_folder').then(r => r.json()).then(d => { if (!d.ok) toast(d.error || 'Could not open folder', true); }); }

/* ---------- offline map downloader ---------- */
let _mapPollTimer = null;
function mapUseBeacon(){
  const la = $('cBcnLat').value, lo = $('cBcnLon').value;
  if (!la || !lo){ toast('Set the home position in the Position beacon section first (or use 📍 Detect there)', true); return; }
  $('cMapLat').value = la; $('cMapLon').value = lo; mapEstimate();
}
async function mapEstimate(){
  const lat = parseFloat($('cMapLat').value), lon = parseFloat($('cMapLon').value);
  if (isNaN(lat) || isNaN(lon)){ $('mapEst').textContent = 'Enter a center point to see the download size.'; return; }
  try {
    const r = await cpost('/api/map/estimate', {lat, lon, radius_km: num('cMapRadius', 150), min_zoom: 5, max_zoom: parseInt($('cMapZoom').value)});
    const d = await r.json();
    $('mapEst').textContent = d.ok ? 'About ' + d.tiles.toLocaleString() + ' tiles, roughly ' + d.mb + ' MB.' + (d.tiles > 40000 ? ' That is a lot — consider a smaller radius or lower detail.' : '') : (d.error || '');
  } catch(e){}
}
async function mapDownload(){
  const lat = parseFloat($('cMapLat').value), lon = parseFloat($('cMapLon').value);
  if (isNaN(lat) || isNaN(lon)){ toast('Enter a center point first', true); return; }
  const name = val('cMapName') || 'home-area';
  try {
    const r = await cpost('/api/map/download', {name, lat, lon, radius_km: num('cMapRadius', 150), min_zoom: 5, max_zoom: parseInt($('cMapZoom').value), source: $('cMapSource').value});
    const d = await r.json();
    if (!d.ok){ toast(d.error || 'Could not start download', true); return; }
    $('mapDlBtn').disabled = true; show('mapCancelBtn', true); show('mapProgress', true);
    if (!_mapPollTimer) _mapPollTimer = setInterval(mapPoll, 1000);
  } catch(e){ toast('Download failed: ' + e, true); }
}
function mapCancel(){ cpost('/api/map/cancel'); }
async function mapPoll(){
  try {
    const r = await fetch('/api/map/status'); const s = await r.json();
    const pct = s.total ? Math.round(s.done * 100 / s.total) : 0;
    $('mapProgress').firstElementChild.style.width = pct + '%';
    if (s.running){ $('mapDlStatus').textContent = 'Downloading ' + s.name + ': ' + s.done.toLocaleString() + ' / ' + s.total.toLocaleString() + ' tiles (' + pct + '%)' + (s.failed ? ', ' + s.failed + ' failed' : ''); return; }
    clearInterval(_mapPollTimer); _mapPollTimer = null;
    $('mapDlBtn').disabled = false; show('mapCancelBtn', false);
    if (s.error){ $('mapDlStatus').textContent = s.error; show('mapProgress', false); }
    else if (s.file){ $('mapDlStatus').textContent = '✓ ' + s.file + ' is ready and selected for the Offline Map tab.' + (s.failed ? ' (' + s.failed + ' tiles could not be fetched.)' : ''); toast('Map ready: ' + s.file); loadMapList(); poll(); }
  } catch(e){}
}
async function loadMapList(){
  const el = $('mapList');
  try {
    const r = await fetch('/api/map/list'); const d = await r.json();
    $('tilesDirText').textContent = d.tiles_dir || 'tiles/';
    if (!d.maps.length){ el.innerHTML = '<div class="fhint">No maps downloaded yet.</div>'; return; }
    el.innerHTML = d.maps.map(m => {
      const active = m.file === d.active;
      return '<div class="mapitem' + (active ? ' active' : '') + '"><div class="grow"><b>' + esc(m.file) + '</b> <span class="meta">' + m.size_mb + ' MB' + (m.maxzoom ? ' · zoom ' + esc(m.minzoom) + '–' + esc(m.maxzoom) : '') + (m.attribution ? ' · ' + esc(m.attribution) : '') + '</span></div>'
        + (active ? '<span class="chip new" title="Shown on the Offline Map tab">In use</span>' : '<button class="btn xs primary" data-map="' + esc(m.file) + '" data-map-act="select" title="Show this map on the Offline Map tab">Use</button>')
        + '<button class="btn xs danger" data-map="' + esc(m.file) + '" data-map-act="delete" title="Delete this map file from the tiles folder">Delete</button></div>';
    }).join('');
    el.querySelectorAll('[data-map-act]').forEach(b => b.onclick = () => b.dataset.mapAct === 'select' ? mapSelect(b.dataset.map) : mapDelete(b.dataset.map));
    if (_mapPollTimer === null){ fetch('/api/map/status').then(r => r.json()).then(s => { if (s.running){ $('mapDlBtn').disabled = true; show('mapCancelBtn', true); show('mapProgress', true); _mapPollTimer = setInterval(mapPoll, 1000); } }); }
  } catch(e){ el.innerHTML = '<div class="fhint">Could not list maps.</div>'; }
}
async function mapSelect(f){ const r = await cpost('/api/map/select', {file: f}); const d = await r.json(); if (d.ok){ toast('Offline map: ' + f); loadMapList(); poll(); } else toast(d.error, true); }
async function mapDelete(f){ if (!confirm('Delete ' + f + '? This removes the downloaded tiles.')) return; const r = await cpost('/api/map/delete', {file: f}); const d = await r.json(); if (d.ok){ toast('Deleted ' + f); loadMapList(); poll(); } else toast(d.error, true); }

/* ---------- live "how your callsign is used" preview in Settings ---------- */
function updateCallPreview(){
  const el = $('callPreview'); if (!el) return;
  const base = val('cHomeCall').toUpperCase();
  if (!base){ el.textContent = 'Enter the home callsign to see the addresses HamLink will use.'; return; }
  const bad = /[-\/]/.test(base);
  const b = base.split(/[-\/]/)[0];
  const hs = val('cAprsSsid') || '-5';
  const ts = (val('cAprsTravSsid') || '-7').split(',').map(s => s.trim()).filter(Boolean).map(s => s.startsWith('-') ? s : '-' + s);
  const ht = val('cPatHomeTac').toUpperCase() || '(home tactical)', tt = val('cPatTravTac').toUpperCase() || '(traveler tactical)';
  const watch = val('cWatch').toUpperCase() || '(everyone)';
  let s = (bad ? '⚠️ "' + esc(base) + '" contains an SSID or suffix — use just ' + esc(b) + '.\n' : '')
    + 'VarAC:   home ' + esc(b) + '  ·  alerts from ' + esc(watch) + '\n'
    + 'APRS:    home ' + esc(b + hs) + '  ·  traveler ' + ts.map(x => esc(b + x)).join(', ') + '\n'
    + 'Winlink: home ' + esc(ht) + '  ·  traveler ' + esc(tt) + '  (login as ' + esc(b) + ')';
  el.innerHTML = s.replace(/\n/g, '<br>');
}

/* ---------- tooltips: give every settings control a title from its label + hint ---------- */
function initTooltips(){
  document.querySelectorAll('.fg, .sr-field').forEach(fg => {
    const label = fg.querySelector('.fl, label'), hint = fg.querySelector('.fhint, .sr-hint');
    const text = [(label ? label.textContent.trim() : ''), (hint ? hint.textContent.trim() : '')].filter(Boolean).join(' — ');
    if (!text) return;
    fg.querySelectorAll('input, select, textarea, .tgl').forEach(el => { if (!el.title) el.title = text; });
  });
  document.querySelectorAll('.sec > summary').forEach(s => { if (!s.title) s.title = 'Click to expand or collapse this section'; });
}
initTooltips();
function updateBcnRfWarn(){ show('bcnRfWarn', isOn('cBcnRf')); }
function updateBcnRfAvail(){
  const smOn = isOn('cSmOn'), t = $('cBcnRf');
  if (!smOn) t.classList.remove('on');
  t.classList.toggle('disabled', !smOn); updateBcnRfWarn();
}

async function loadPatConfig(){
  try {
    const r = await fetch('/api/pat_config'); const d = await r.json();
    if (d.ok){ $('cPatCall').value = d.mycall || ''; $('cPatPass').value = d.secure_login_password || ''; $('cPatLoc').value = d.locator || ''; $('cPatVaraAddr').value = d.varafm_addr || 'localhost:8300'; }
  } catch(e){}
}
async function savePatConfig(){
  const p = {mycall: val('cPatCall').toUpperCase(), secure_login_password: $('cPatPass').value, locator: val('cPatLoc').toUpperCase(),
    home_tactical: val('cPatHomeTac').toUpperCase(), varafm_addr: val('cPatVaraAddr') || 'localhost:8300'};
  const st = $('patSaveStatus'); st.textContent = 'Saving…';
  try { const r = await cpost('/api/pat_config', p); const d = await r.json(); st.textContent = d.ok ? 'Saved — Pat restarted.' : 'Error saving'; }
  catch(e){ st.textContent = 'Error: ' + e; }
  setTimeout(() => { st.textContent = ''; }, 4000);
}
async function loadGateways(){
  const el = $('gwList'); let grid = val('cPatLoc').toUpperCase();
  if (!grid){
    el.textContent = 'Detecting your location…';
    try {
      const pos = await new Promise((res, rej) => { if (!navigator.geolocation) rej(new Error('No geolocation')); else navigator.geolocation.getCurrentPosition(res, rej, {timeout: 8000}); });
      grid = latLonToGrid(pos.coords.latitude, pos.coords.longitude).toUpperCase(); $('cPatLoc').value = grid; toast('Location detected: ' + grid);
    } catch(e){ el.textContent = 'Enter a grid locator or allow location access, then try again.'; return; }
  }
  el.textContent = 'Updating location and searching…';
  try { await cpost('/api/pat_config', {locator: grid}); } catch(e){}
  try {
    const r = await fetch('/api/pat_gateways'); const d = await r.json();
    if (d.ok && d.gateways.length){
      el.innerHTML = d.gateways.slice(0, 8).map(g => {
        let label = g.callsign || g.info || 'Unknown'; if (g.distance) label += ' (' + Math.round(g.distance) + ' km)';
        return g.callsign ? '<a href="#" data-call="' + esc(g.callsign) + '">' + esc(label) + '</a>' : '<div>' + esc(label) + '</div>';
      }).join('');
      el.querySelectorAll('a').forEach(a => a.onclick = ev => { ev.preventDefault(); $('cPatRfGw').value = a.dataset.call; });
    } else el.textContent = d.error || 'No VARA FM gateways found nearby.';
  } catch(e){ el.textContent = 'Error: ' + e; }
}
function latLonToGrid(lat, lon){   // 6-character Maidenhead locator
  const lo = lon + 180, la = lat + 90;
  return String.fromCharCode(65 + Math.floor(lo / 20)) + String.fromCharCode(65 + Math.floor(la / 10))
    + Math.floor((lo % 20) / 2) + Math.floor(la % 10)
    + String.fromCharCode(97 + Math.floor((lo % 2) * 12)) + String.fromCharCode(97 + Math.floor((la % 1) * 24));
}
function detectLocation(){
  if (!navigator.geolocation){ toast('This browser does not support geolocation', true); return; }
  const el = $('cPatLoc'); el.value = 'Detecting…';
  navigator.geolocation.getCurrentPosition(p => { el.value = latLonToGrid(p.coords.latitude, p.coords.longitude).toUpperCase(); toast('Location detected: ' + el.value); },
    err => { el.value = ''; toast('Location error: ' + err.message, true); }, {timeout: 10000});
}
function detectBeaconLocation(){
  if (!navigator.geolocation){ toast('This browser does not support geolocation', true); return; }
  toast('Detecting location…');
  navigator.geolocation.getCurrentPosition(p => {
    $('cBcnLat').value = p.coords.latitude.toFixed(4); $('cBcnLon').value = p.coords.longitude.toFixed(4);
    if (!$('cPatLoc').value) $('cPatLoc').value = latLonToGrid(p.coords.latitude, p.coords.longitude).toUpperCase();
    toast('Location set: ' + p.coords.latitude.toFixed(4) + ', ' + p.coords.longitude.toFixed(4));
  }, err => toast('Location error: ' + err.message, true), {timeout: 10000, enableHighAccuracy: true});
}
async function browse(path){
  const fb = $('fb');
  try {
    const r = await cpost('/api/browse_path', {path}); const d = await r.json();
    if (!d.ok){ toast(d.error || 'Cannot open folder', true); return; }
    let h = d.current ? '<div class="fc">📂 ' + esc(d.current) + '</div>' : '';
    for (const e of d.entries) h += '<div class="fe ' + (e.is_dir ? 'dir' : 'file') + '" data-path="' + esc(e.path) + '" data-dir="' + (e.is_dir ? 1 : 0) + '">' + (e.is_dir ? '📁' : '📄') + ' ' + esc(e.name) + '</div>';
    fb.innerHTML = h; fb.classList.add('open');
    fb.querySelectorAll('.fe').forEach(el => el.onclick = () => el.dataset.dir === '1' ? browse(el.dataset.path) : pickDb(el.dataset.path));
  } catch(e){ toast('Browse failed: ' + e, true); }
}
function pickDb(p){ $('cDb').value = p; $('fb').classList.remove('open'); testDb(); }
async function testDb(){
  const el = $('dbTr'), p = val('cDb');
  if (!p){ el.className = 'tr err'; el.textContent = 'Enter a path'; return; }
  const r = await cpost('/api/test_db', {path: p}); const d = await r.json();
  el.className = d.ok ? 'tr ok' : 'tr err'; el.textContent = d.ok ? '✓ Connected — ' + d.vmail_count + ' messages found.' : d.error;
}
async function testPo(){
  const el = $('poTr'), uk = val('cPoUser'), at = val('cPoToken');
  if (!uk || !at){ el.className = 'tr err'; el.textContent = 'Enter both keys'; return; }
  const r = await cpost('/api/test_pushover', {user_key: uk, api_token: at}); const d = await r.json();
  el.className = d.ok ? 'tr ok' : 'tr err'; el.textContent = d.ok ? '✓ Notification sent — check your phone.' : d.error;
}
async function genPasscode(){
  const call = val('cHomeCall'); if (!call){ toast('Enter a home callsign first', true); return; }
  const r = await cpost('/api/aprs_passcode', {callsign: call}); const d = await r.json();
  if (d.ok) $('cAprsPass').value = d.passcode;
}

/* ---------- message log ---------- */
function toggleLog(){
  const area = $('logArea'), btn = $('logToggleBtn');
  const open = area.classList.contains('hidden');
  area.classList.toggle('hidden', !open); btn.textContent = open ? 'Hide log' : 'View log';
  if (open) loadLog();
}
async function loadLog(){
  const el = $('logEntries'); el.innerHTML = '<div class="empty"><p>Loading…</p></div>';
  try {
    const r = await fetch('/api/message_log'); const d = await r.json();
    if (!d.ok || !d.entries.length){ el.innerHTML = '<div class="empty"><div class="icon">📋</div><p>No saved messages yet</p></div>'; return; }
    el.innerHTML = d.entries.map(e => {
      const out = e.direction === 'outgoing';
      const who = out ? 'To ' + esc(e.to_callsign) : 'From ' + esc(e.from_name || e.from_callsign);
      const type = out ? 'sent' : (e.type || '');
      return '<article class="card msg ch-' + esc(type) + '"><div class="msg-head"><div class="msg-from"><span class="chip ' + esc(type) + '">' + (out ? 'Sent' : (CHAN[e.type] || 'Received')) + '</span>' + who + '</div>'
        + '<div class="msg-time">' + esc(fmtTime(e.timestamp)) + '</div></div>'
        + (e.subject ? '<div class="msg-subject">' + esc(e.subject) + '</div>' : '')
        + (e.message ? '<div class="msg-body" style="max-height:72px;overflow:hidden">' + esc(e.message) + '</div>' : '') + '</article>';
    }).join('');
  } catch(e){ el.innerHTML = '<div class="empty"><p>Failed to load log</p></div>'; }
}

/* ---------- position ack ---------- */
function ackPosition(){
  const {pos} = currentPos(last || {}); if (pos) _ackedPosKey = posKey(pos);
  show('locNewBadge', false); show('locAckBtn', false); $('locCard').classList.remove('new');
}

/* ---------- shutdown ---------- */
async function confirmShutdown(){
  if (!confirm('Stop HamLink and close all associated programs (VarAC, Soundmodem, Pat, VARA FM)?')) return;
  try { await cpost('/api/shutdown'); } catch(e){}
  document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;font-family:system-ui;color:#64748b;font-size:18px">HamLink has been stopped. You can close this tab.</div>';
}

/* ---------- tabs + map ---------- */
function switchTab(tab){
  currentTab = tab;
  show('dashboardTab', tab === 'dashboard'); show('mapTab', tab === 'map');
  $('tabDashboard').classList.toggle('active', tab === 'dashboard'); $('tabMap').classList.toggle('active', tab === 'map');
  if (tab === 'map') initMap();
}
let _map = null, _marker = null, _lastMapPos = null;
function initMap(){
  const mapState = last && last.config ? (last.config.map_file || last.config.map_state) : '';
  show('mapNoConfig', !mapState); $('mapContainer').style.display = mapState ? 'block' : 'none';
  if (!mapState) return;
  if (!_map){
    L.Icon.Default.imagePath = '/static/';
    _map = L.map('mapContainer').setView([39.8, -98.5], 5);
    L.tileLayer('/tiles/{z}/{x}/{y}.png', {maxZoom: 16, minZoom: 3, attribution: 'Offline tiles · USGS The National Map'}).addTo(_map);
  }
  setTimeout(() => _map.invalidateSize(), 100);
  updateMapPosition();
}
function centerMap(){ const {pos} = currentPos(last || {}); if (_map && pos) _map.setView([pos.lat, pos.lon], 11); }
function updateMapPosition(){
  if (!_map || !last) return;
  const {pos, src} = currentPos(last);
  if (!pos){ $('mapPosInfo').style.display = 'none'; return; }
  const ll = [pos.lat, pos.lon], key = pos.lat.toFixed(5) + ',' + pos.lon.toFixed(5);
  if (_marker) _marker.setLatLng(ll); else _marker = L.marker(ll).addTo(_map);
  const name = last.config.operator_name || pos.callsign || 'Unknown';
  _marker.bindPopup('<b>' + esc(name) + '</b><br>' + src + ' position<br>' + pos.lat.toFixed(4) + ', ' + pos.lon.toFixed(4) + '<br>' + esc(fmtTime(pos.time)));
  if (key !== _lastMapPos){ _map.setView(ll, 10); _lastMapPos = key; }   // only recenter when the position changes
  $('mapPosInfo').style.display = 'block';
  $('mapPosTitle').textContent = name + ' — ' + src + ' position';
  $('mapPosTime').textContent = fmtTime(pos.time); $('mapPosDetails').textContent = posDetails(pos);
}
</script>
</body>
</html>"""

def _seed_demo_state():
    """Populate the dashboard with sample data (HAMLINK_DEMO=1). Used for
    screenshots and for trying the UI without any radio software installed.
    Nothing is transmitted and no external programs are launched."""
    now = datetime.now(timezone.utc)
    iso = lambda mins: (now - timedelta(minutes=mins)).isoformat()
    with cfglock:
        # In-memory only — never saved, and no real VarAC.db is ever opened.
        config["operator_name"] = "Alex"
        config["home_callsign"] = "W1AW"
        config["watch_callsigns"] = ["W1AW/P"]
        config["varac_db_path"] = "(demo) VarAC.db"
        config["aprs"]["enabled"] = True
        config["pat"]["enabled"] = True
        config["relay"]["enabled"] = True
    name = config["operator_name"]
    samples = [
        {"id": "vmail-demo-3", "type": "vmail", "time": iso(4), "from_call": "W1AW/P",
         "from_name": name, "to": "W1AW", "subject": "Made camp",
         "message": "Set up at the trailhead lot, 3 bars on VarAC. Weather clear, "
                    "temps dropping tonight. Will check in again 0800 local.",
         "urgent": False, "band": "40m", "snr": "-8"},
        {"id": "aprs-W1AW-7-demo", "type": "aprs", "time": iso(27), "from_call": "W1AW-7",
         "from_name": name, "to": "W1AW-5", "subject": "",
         "message": "Passing mile 212, all good. Fuel at next town.", "urgent": False},
        {"id": "winlink-demo-1", "type": "winlink", "time": iso(180), "from_call": "W1AW",
         "from_name": name, "to": "HOMEBASE", "subject": "Day 2 check-in",
         "message": "Left the cabin at sunrise. Roads open. Expect to lose cell "
                    "coverage after noon so I'll switch to APRS.", "urgent": False},
        {"id": "relay-demo-9", "type": "relay", "time": iso(65), "from_call": "K1XYZ",
         "from_name": "K1XYZ", "relay_station": "K1XYZ", "frequency_mhz": "7.1050", "urgent": False},
    ]
    for a in samples:
        a["friendly_time"] = _friendly_time(a["time"])
    with slock:
        state["history"] = [samples[2], samples[3], samples[1], samples[0]]
        state["history"].insert(3, {
            "id": "sent-aprs-demo", "type": "sent", "time": iso(20), "from_call": "",
            "from_name": "You", "to_callsign": "W1AW-7", "subject": "", "channel": "aprs",
            "message": "[APRS] Got it. Drive safe, call when you can.", "urgent": False,
            "delivered": True, "delivered_by": "W1AW-7", "delivered_time": iso(19),
        })
        state["pending_alerts"] = [samples[0]]
        state["last_checkin_time"] = samples[0]["time"]
        state["last_checkin_from"] = name
        state["db_connected"] = True
        state["aprs_connected"] = True
        state["pat_connected"] = True
        state["internet_up"] = True
        state["aprs_last_position"] = {
            "callsign": "W1AW-7", "lat": 44.2706, "lon": -71.3033, "time": iso(27),
            "altitude": 1917, "speed": 0, "comment": "Mt Washington Auto Rd", "source": "aprs",
        }
        state["relay_tracking"]["K1XYZ"] = {
            "relay_station": "K1XYZ", "frequency_mhz": "7.1050", "first_seen": iso(65),
            "last_seen": iso(65), "notification_count": 1, "urgent": False,
            "status": "confirmed_wait", "last_attempt": None, "attempts": 0, "error": None,
        }
        state["relay_pending_confirm"] = "K1XYZ"
        state["update"].update({
            "available": True, "latest": "v9.9.9", "current": _running_version(),
            "url": f"https://github.com/{UPDATE_REPO}/releases/latest",
            "notes": "## Demo release\n\nThis banner shows how an available update is announced. "
                     "Clicking Update now walks through the download and install steps without changing anything.",
            "checked_at": now.isoformat(), "install_kind": _install_kind(),
        })
    log.info("DEMO MODE: seeded sample dashboard data (nothing will be transmitted)")


def _deferred_launches():
    """Launch VarAC, APRS, Soundmodem, Pat, beacon AFTER Flask is listening,
    so the browser never sees 'connection refused' while helpers start."""
    time.sleep(1)  # Brief pause to let Flask bind the port
    with cfglock:
        cfg = copy.deepcopy(config)
    if cfg.get("varac_exe_path", ""):
        try:
            _launch_varac()
        except Exception as e:
            log.error("VarAC launch failed: %s", e)
    if cfg.get("aprs", {}).get("enabled", False):
        start_aprs()
    if cfg.get("soundmodem", {}).get("enabled", False):
        try:
            _launch_soundmodem()
        except Exception as e:
            log.error("Soundmodem launch failed: %s", e)
        start_kiss()
    if cfg.get("pat", {}).get("enabled", False):
        try:
            pcfg = _pat_read_config()
            dirty = False
            # Clear Pat's internal schedule — HamLink controls sync timing
            if pcfg.get("schedule"):
                log.info("Clearing Pat's internal schedule — HamLink handles sync timing")
                pcfg.pop("schedule", None)
                dirty = True
            # Fix auxiliary_addresses: must be strings, not objects
            aux = pcfg.get("auxiliary_addresses", [])
            if aux and any(isinstance(a, dict) for a in aux):
                log.info("Fixing Pat auxiliary_addresses: converting objects to strings")
                pcfg["auxiliary_addresses"] = [a.get("Address", str(a)) if isinstance(a, dict) else a for a in aux]
                dirty = True
            if dirty:
                _pat_write_config(pcfg)
            _launch_varafm()
            _launch_pat()
        except Exception as e:
            log.error("Pat/VARA FM launch failed: %s", e)
        start_pat()
    if cfg.get("beacon", {}).get("enabled", False):
        start_beacon()
    if cfg.get("relay", {}).get("enabled", False):
        start_relay()
    log.info("All external services launched")


def main():
    _init_log_file()
    port = int(config.get("web_port", 5000))
    log.info("HamLink Radio v%s (%s install) starting on port %d", _running_version(), _install_kind(), port)
    log.info("Open http://127.0.0.1:%d", port)

    # Check if port is already in use (previous instance still running?)
    if _port_open("127.0.0.1", port, timeout=1):
        log.error("Port %d is already in use! Is another instance of HamLink Radio running?", port)
        log.error("Stop the other instance first, or change web_port in config.json")
        sys.exit(1)

    _remove_old_exe()
    if DEMO_MODE:
        _seed_demo_state()
    else:
        threading.Thread(target=_deferred_launches, daemon=True).start()
        start_update_checker()

    # Start the DB poll loop (lightweight, non-blocking)
    threading.Thread(target=poll_loop, daemon=True).start()

    # Suppress Flask/Werkzeug development server banner but keep ERROR level visible
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    import flask.cli
    flask.cli.show_server_banner = lambda *_: None
    log.info("Web server ready on port %d", port)
    # The .bat launcher opens the browser itself; the standalone exe has to do
    # it here. --no-browser (used by the self-updater's relaunch) suppresses it.
    if getattr(sys, "frozen", False) and "--no-browser" not in sys.argv and not DEMO_MODE:
        def _open_browser():
            time.sleep(2)
            log.info("Opening http://127.0.0.1:%d in your browser", port)
            try:
                webbrowser.open(f"http://127.0.0.1:{port}")
            except Exception as e:
                log.warning("Could not open browser: %s", e)
        threading.Thread(target=_open_browser, daemon=True).start()
    try:
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)
    except OSError as e:
        if "Address already in use" in str(e) or "10048" in str(e):
            log.error("Port %d is already in use! Stop the other instance or change web_port in config.json", port)
        else:
            log.error("Failed to start web server: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
