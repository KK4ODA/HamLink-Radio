#!/usr/bin/env python3
"""
HomeLink Radio — Family Edition
-------------------------------------
A friendly web app for family members to see check-in messages
from a traveling ham operator, and reply back via VarAC VMail.

No ham radio knowledge required to use.
All configuration in the browser Settings panel.
"""

import json, os, sys, sqlite3, time, threading, logging, uuid, csv
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template_string, request

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
            os.system('osascript -e \'display notification "New message!" with title "HomeLink Radio" sound name "Submarine"\'')
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

def _speaker_alarm_loop():
    """Background loop that beeps every 8 seconds while alarm is active."""
    global _speaker_alarm_active
    while _speaker_alarm_active:
        _beep_system()
        for _ in range(80):  # 8 seconds in 0.1s increments, check flag
            if not _speaker_alarm_active:
                return
            time.sleep(0.1)

def start_speaker_alarm():
    global _speaker_alarm_active, _speaker_alarm_thread
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
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "message_log.csv")

def _init_log_file():
    """Create the CSV log file with headers if it doesn't exist."""
    if not os.path.isfile(LOG_FILE):
        with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "type", "from_callsign", "from_name",
                        "to_callsign", "subject", "message", "urgent",
                        "band", "snr", "direction"])

def log_message(alert_dict, direction="incoming"):
    """Append a message to the persistent CSV log."""
    try:
        _init_log_file()
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
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
    except Exception as e:
        log.warning("Failed to write message log: %s", e)

def log_reply(to_call, message):
    """Log an outgoing reply to the CSV."""
    try:
        _init_log_file()
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                datetime.now(timezone.utc).isoformat(),
                "reply", "", "", to_call,
                "Reply", message, False, "", "", "outgoing",
            ])
    except Exception as e:
        log.warning("Failed to log reply: %s", e)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
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
                 "priority": 1, "retry": 60, "expire": 3600, "sound": "pushover"},
    "aprs": {"enabled": False, "home_ssid": "-5", "traveler_ssids": "-7",
             "passcode": "", "server": "rotate.aprs2.net", "port": 14580,
             "use_mailbox": False, "rf_fallback": False},
    "soundmodem": {"enabled": False, "exe_path": "", "kiss_host": "127.0.0.1",
                   "kiss_port": 8100, "auto_launch": True},
    "pat": {"enabled": False, "exe_path": "", "http_addr": "localhost:8080",
            "auto_launch": True, "poll_interval": 30,
            "home_tactical": "", "traveler_tactical": "",
            "rf_fallback": False, "rf_gateway": "", "varafm_addr": "localhost:8300",
            "varafm_exe_path": ""},
    "beacon": {"enabled": False, "lat": 0.0, "lon": 0.0,
               "symbol_table": "/", "symbol_code": "-",
               "comment": "HomeLink Radio", "interval_minutes": 30,
               "via_aprsis": True, "via_rf": True},
    "web_port": 5000,
    "alert_sound": "gentle",
    "alert_volume": 0.3,
    "quick_replies": [
        "Got your message, all is well here!",
        "Please check in again soon.",
        "Call home when you can.",
        "We miss you, stay safe!",
    ],
}

def load_config():
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                s = json.load(f)
            m = {**DEFAULT_CONFIG, **s}
            m["pushover"] = {**DEFAULT_CONFIG["pushover"], **s.get("pushover", {})}
            m["aprs"] = {**DEFAULT_CONFIG["aprs"], **s.get("aprs", {})}
            m["soundmodem"] = {**DEFAULT_CONFIG["soundmodem"], **s.get("soundmodem", {})}
            m["pat"] = {**DEFAULT_CONFIG["pat"], **s.get("pat", {})}
            m["beacon"] = {**DEFAULT_CONFIG["beacon"], **s.get("beacon", {})}
            # Ensure watch_callsigns is a list
            if isinstance(m.get("watch_callsigns"), str):
                m["watch_callsigns"] = [c.strip() for c in m["watch_callsigns"].split(",") if c.strip()]
            print(f"[CONFIG] Loaded from {CONFIG_PATH}")
            return m
        except Exception as e:
            print(f"[CONFIG] Error loading {CONFIG_PATH}: {e} — using defaults")
    else:
        print(f"[CONFIG] No config found at {CONFIG_PATH} — using defaults")
    return dict(DEFAULT_CONFIG)

def save_config(c):
    with open(CONFIG_PATH, "w") as f:
        json.dump(c, f, indent=2)

config = load_config()
# Save defaults if config.json doesn't exist yet (first run)
if not os.path.isfile(CONFIG_PATH):
    save_config(config)
    print(f"[CONFIG] Created default config at {CONFIG_PATH}")
cfglock = threading.Lock()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("homelink-radio")

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
    "pat_connected": False, "pat_error": None, "pat_last_check": None,
    "aprs_last_position": None,
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
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(("8.8.8.8", 53))
        s.close()
        return True
    except Exception:
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
            ap = config.get("aprs", {})
            base = config.get("home_callsign", "").strip().upper()
        home = _aprs_callsign()
        pc = ap.get("passcode", "") or str(aprs_passcode(base))
        ais = aprslib.IS(home, passwd=pc,
                         host=ap.get("server", "rotate.aprs2.net"),
                         port=int(ap.get("port", 14580)))
        ais.connect()
        ais.sendall(packet_str)
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
    """Send an APRS message ACK immediately via both APRS-IS and RF."""
    home = _aprs_callsign()
    padded = to_call.ljust(9)
    ack_pkt = f"{home}>APRS,TCPIP*::{padded}:ack{msgno}"
    log.info("APRS ACK -> %s for msg# %s", to_call, msgno)
    _aprs_send_raw(ack_pkt)
    # Also send via RF if Soundmodem is connected
    rf_pkt = f"{home}>APRS,WIDE1-1::{padded}:ack{msgno}"
    _aprs_send_via_kiss(rf_pkt)

def _process_aprs_packet(packet):
    """Process a parsed APRS packet."""
    try:
        ptype = packet.get("format", "")
        from_call = packet.get("from", "")

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
                with slock:
                    state["aprs_last_position"] = {
                        "callsign": from_call, "lat": lat, "lon": lon,
                        "time": datetime.now(timezone.utc).isoformat(),
                        "altitude": packet.get("altitude"),
                        "speed": packet.get("speed"),
                        "comment": packet.get("comment", ""),
                    }

        # --- Message packets ---
        if ptype == "message" or "message_text" in packet:
            msg_text = packet.get("message_text", "")
            addresse = packet.get("addresse", "").strip()
            msgno = packet.get("msgNo", "") or packet.get("msgno", "")

            # Skip empty, ACKs, REJs
            if not msg_text or msg_text.lower().startswith("ack") or msg_text.lower().startswith("rej"):
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
                # Store as a system notification (no alarm, just confirmation)
                now_utc = datetime.now(timezone.utc).isoformat()
                sys_id = f"sys-{from_call}-{msgno or int(time.time())}"
                with slock:
                    state["history"].append({
                        "id": sys_id, "type": "system",
                        "time": now_utc, "from_call": from_call,
                        "from_name": from_call, "to": addresse,
                        "subject": "", "message": msg_text,
                        "urgent": False, "friendly_time": _friendly_time(now_utc),
                    })
                    state["reply_status"] = {"time": now_utc, "to": addresse,
                                             "message": msg_text[:100], "status": "confirmed"}
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
        log_reply(to_call, f"[APRS] {msg}")
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
            mail_pkt = f"{home}>APRS,TCPIP*::{mail_padded}:{mail_msg}{{{mail_msgno}"
            _aprs_send_raw(mail_pkt)
            log.info("APRS mailbox copy sent to MAIL for %s", mail_dest)
        return True, ""
    return False, "Failed to send"

def aprs_send_bulletin(bulletin_id, message):
    """Send an APRS bulletin (BLN#) via a dedicated APRS-IS connection and RF.
    Uses a fresh unfiltered connection so the server propagates the bulletin."""
    home = _aprs_callsign()
    if not home:
        return False, "Home callsign not configured"
    padded = f"BLN{bulletin_id}".ljust(9)
    msg = message[:67]
    pkt = f"{home}>APRS,TCPIP*::{padded}:{msg}"
    # Send via dedicated unfiltered connection (not the shared listener)
    ok_is = False
    try:
        import aprslib
        with cfglock:
            ap = config.get("aprs", {})
            base = config.get("home_callsign", "").strip().upper()
        pc = ap.get("passcode", "") or str(aprs_passcode(base))
        ais = aprslib.IS(home, passwd=pc,
                         host=ap.get("server", "rotate.aprs2.net"),
                         port=int(ap.get("port", 14580)))
        ais.connect()
        ais.sendall(pkt)
        time.sleep(2)  # Give server time to propagate before disconnect
        ais.close()
        ok_is = True
        log.info("APRS bulletin TX (APRS-IS): %s", pkt[:80])
    except Exception as e:
        log.warning("APRS bulletin APRS-IS send failed: %s", e)
    # Also send via RF
    rf_pkt = f"{home}>APRS,WIDE1-1,WIDE2-1::{padded}:{msg}"
    _aprs_send_via_kiss(rf_pkt)
    if ok_is:
        log.info("APRS bulletin BLN%s sent: %s", bulletin_id, msg)
    return ok_is, "" if ok_is else "APRS-IS send failed"


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
            filt = "b/" + "/".join(sorted(watch_bases))

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
            # immortal=False — we handle reconnection with backoff
            ais.consumer(_process_aprs_packet, immortal=False, raw=False)
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
        time.sleep(backoff)
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
        pid = frame[idx+1] if idx+1 < len(frame) else 0
        idx += 2

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
            import subprocess
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/NH"],
                capture_output=True, text=True, timeout=5
            )
            return exe_name.lower() in result.stdout.lower()
        else:
            import subprocess
            result = subprocess.run(["pgrep", "-f", exe_name],
                                    capture_output=True, timeout=5)
            return result.returncode == 0
    except Exception:
        return False

def get_bbs_directory():
    """Return the BBS directory path. Priority: HomeLink config override > VarAC .ini > None."""
    with cfglock:
        override = config.get("bbs_directory", "").strip()
    if override:
        return override
    ini_path = _varac_ini_path()
    if ini_path:
        try:
            import configparser
            cp = configparser.ConfigParser()
            cp.read(ini_path, encoding="utf-8")
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
        import configparser
        cp = configparser.ConfigParser()
        cp.read(ini_path, encoding="utf-8")
        raw = cp.get("RIG_CONTROL", "LastFrequency", fallback="")
        if raw:
            # Format is "7.105.000" — convert to "7.105"
            clean = raw.strip().replace(".", "", 1)  # "7105.000"
            # Actually parse: "7.105.000" -> 7105000 Hz -> 7.105 MHz
            hz = int(raw.replace(".", ""))  # 7105000
            mhz = hz / 1_000_000
            return f"{mhz:.3f}"
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
        import configparser
        cp = configparser.ConfigParser()
        cp.read(ini_path, encoding="utf-8")
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
        from datetime import datetime, timezone, timedelta
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
        import win32gui, win32con
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
        import re
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
        import subprocess
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
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect((kiss_host, kiss_port))
        s.close()
        log.info("Soundmodem already running on %s:%d (external instance) — skipping launch", kiss_host, kiss_port)
        return
    except (ConnectionRefusedError, OSError, socket.timeout):
        pass  # Port not in use, safe to launch
    try:
        import subprocess
        log.info("Launching Soundmodem: %s", exe)
        _soundmodem_proc = subprocess.Popen([exe], cwd=os.path.dirname(exe))
        log.info("Soundmodem launched (PID %d)", _soundmodem_proc.pid)
        time.sleep(3)
    except Exception as e:
        log.error("Failed to launch Soundmodem: %s", e)

def _kiss_listener_loop():
    """Background thread: connect to Soundmodem KISS TCP port, read frames."""
    global _kiss_running, _kiss_sock
    import socket
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
_PAT_SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pat_seen_ids")

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
    try:
        import socket
        with cfglock:
            addr = config.get("pat", {}).get("varafm_addr", "localhost:8300")
        _host, port_str = addr.rsplit(":", 1)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(("127.0.0.1", int(port_str)))  # Always use 127.0.0.1 to avoid IPv6 hangs
        s.close()
        log.info("VARA FM already running on %s (external instance) — skipping launch", addr)
        return
    except (ConnectionRefusedError, OSError, socket.timeout):
        pass
    try:
        import subprocess
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
    try:
        import socket
        host, port_str = http_addr.rsplit(":", 1)
        log.info("Checking if Pat is already on %s:%s...", host or "127.0.0.1", port_str)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(("127.0.0.1", int(port_str)))  # Always use 127.0.0.1 to avoid IPv6 hangs
        s.close()
        log.info("Pat already running on %s (external instance) — skipping launch", http_addr)
        return
    except (ConnectionRefusedError, OSError, socket.timeout):
        pass  # Port not in use, safe to launch
    try:
        import subprocess
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
        import urllib.request, urllib.parse
        url = _pat_api_url() + "/mailbox/in"
        req = urllib.request.Request(url)
        resp = urllib.request.urlopen(req, timeout=10)
        raw = resp.read().decode()
        data = json.loads(raw)

        if data:
            log.debug("Pat inbox: %d messages, first keys: %s", len(data), list(data[0].keys()) if data else "none")

        with cfglock:
            opname = config.get("operator_name", "") or ""

        for msg in data:
            mid = msg.get("MID", "") or msg.get("mid", "") or msg.get("Id", "") or msg.get("id", "")
            if not mid:
                # Try to generate a unique ID from subject+date
                mid = f"{msg.get('Subject', msg.get('subject', ''))}_{msg.get('Date', msg.get('date', ''))}"
            if mid in _pat_seen_ids:
                continue
            _pat_seen_ids.add(mid)
            _pat_save_seen()

            # Handle different field name casing Pat might use
            subject = msg.get("Subject", "") or msg.get("subject", "")
            from_field = msg.get("From", msg.get("from", ""))
            if isinstance(from_field, dict):
                from_addr = from_field.get("Addr", "") or from_field.get("addr", "")
            elif isinstance(from_field, str):
                from_addr = from_field
            else:
                from_addr = str(from_field) if from_field else ""
            body_text = msg.get("Body", "") or msg.get("body", "")
            t = msg.get("Date", "") or msg.get("date", "") or datetime.now(timezone.utc).isoformat()
            name = opname or from_addr

            log.info("Winlink inbox msg: MID=%s From=%s Subject=%s", mid[:20], from_addr, subject[:40])

            alert = {
                "id": f"winlink-{mid}",
                "type": "winlink",
                "time": t,
                "from_call": from_addr,
                "from_name": name,
                "to": "",
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
    try:
        import subprocess
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
        log_reply(to_addr, f"[Winlink] {subject}: {body[:100]}")
        return True, ""
    except subprocess.TimeoutExpired:
        proc.kill()
        return False, "Pat compose timed out"
    except Exception as e:
        log.error("Winlink send failed: %s", e)
        return False, str(e)

def pat_connect_telnet():
    """Trigger Pat to connect via telnet, or via VARA FM gateway if no internet."""
    try:
        import subprocess
        with cfglock:
            exe = config.get("pat", {}).get("exe_path", "")
            rf_fallback = config.get("pat", {}).get("rf_fallback", False)
            rf_gateway = config.get("pat", {}).get("rf_gateway", "")
        if not exe or not os.path.isfile(exe):
            return False

        # Check internet and decide connection method
        use_rf = False
        if rf_fallback and rf_gateway and not _check_internet(timeout=2):
            use_rf = True
            log.info("No internet — Pat connecting via VARA FM to %s", rf_gateway)

        if use_rf:
            connect_url = f"varafm:///{rf_gateway}"
            cmd = [exe, "connect", connect_url]
            timeout_secs = 300  # RF connections take longer
        else:
            cmd = [exe, "connect", "telnet"]
            timeout_secs = 120

        log.info("Pat sync: %s", " ".join(cmd))
        proc = subprocess.run(
            cmd, cwd=os.path.dirname(exe),
            capture_output=True, timeout=timeout_secs
        )
        if proc.returncode == 0:
            log.info("Pat session completed (%s)", "RF" if use_rf else "telnet")
            return True
        log.warning("Pat connect failed: %s", proc.stderr.decode()[:200])
        return False
    except subprocess.TimeoutExpired:
        log.warning("Pat session timed out")
        return False
    except Exception as e:
        log.warning("Pat connect failed: %s", e)
        return False

def _pat_poll_loop():
    """Background thread: sync with Winlink CMS and poll Pat inbox periodically.
    
    The poll_interval controls how often we trigger a full telnet sync.
    Between syncs, we check the local inbox every 15 seconds so messages
    arriving via Pat's own schedule or manual sync are detected quickly.
    """
    global _pat_running
    INBOX_CHECK_INTERVAL = 15  # seconds between local inbox checks
    while _pat_running:
        with cfglock:
            pt = config.get("pat", {})
            enabled = pt.get("enabled", False)
            sync_interval = max(pt.get("poll_interval", 300), 60)
        if not enabled:
            with slock:
                state["pat_connected"] = False
                state["pat_error"] = None
            time.sleep(5)
            continue
        # Full sync with CMS
        log.info("Pat: syncing with Winlink CMS (next sync in %ds)", sync_interval)
        pat_connect_telnet()
        _pat_check_inbox()
        # Between syncs, keep checking local inbox frequently
        elapsed = 0
        while elapsed < sync_interval and _pat_running:
            time.sleep(INBOX_CHECK_INTERVAL)
            elapsed += INBOX_CHECK_INTERVAL
            with cfglock:
                still_enabled = config.get("pat", {}).get("enabled", False)
            if not still_enabled:
                break
            _pat_check_inbox()

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
    comment = bcn.get("comment", "HomeLink Radio")
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
# Pushover
# ---------------------------------------------------------------------------
def send_pushover(title, message, reply_channel="varac"):
    with cfglock:
        po = config.get("pushover", {})
        quick_replies = config.get("quick_replies", [])
        web_port = config.get("web_port", 5000)
    if not po.get("enabled") or not po.get("user_key") or not po.get("api_token"):
        return
    try:
        import urllib.request, urllib.parse

        # Build HTML message with quick reply links
        # These link to the monitor's API on the local network
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            local_ip = "127.0.0.1"
        base_url = f"http://{local_ip}:{web_port}"
        html_body = message.replace("\n", "<br>")

        if quick_replies:
            html_body += "<br><br><b>Quick Replies:</b><br>"
            for qr in quick_replies[:4]:  # Max 4 quick replies
                qr_short = qr[:67] if reply_channel == "aprs" else qr
                encoded = urllib.parse.quote(qr_short)
                if reply_channel == "aprs":
                    link = f"{base_url}/api/pushover_reply?channel=aprs&message={encoded}"
                else:
                    link = f"{base_url}/api/pushover_reply?channel=varac&message={encoded}"
                html_body += f'→ <a href="{link}">{qr_short}</a><br>'

        p = {"token": po["api_token"], "user": po["user_key"],
             "title": title, "message": html_body, "html": "1",
             "priority": po.get("priority", 1), "sound": po.get("sound", "pushover"),
             "url": f"{base_url}", "url_title": "Open Monitor"}
        if p["priority"] == 2:
            p["retry"] = po.get("retry", 60)
            p["expire"] = po.get("expire", 3600)
        d = urllib.parse.urlencode(p).encode()
        r = urllib.request.Request("https://api.pushover.net/1/messages.json", data=d)
        urllib.request.urlopen(r, timeout=10)
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

def poll_once():
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
            # Skip messages FROM ourselves (outgoing copies VarAC may place in inbox)
            if home_call_upper and fr.upper().startswith(home_call_upper.split("-")[0]):
                log.debug("Skipping own outgoing vmail from %s to %s", fr, row["vmail_to"] or "")
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
            # (we just sent the message — no need to alert ourselves)
            if home_call_upper and fr.upper().startswith(home_call_upper.split("-")[0]):
                log.debug("Skipping own relay notification from %s", fr)
                continue
            if _match(fr):
                t = row["relay_notification_time"] or datetime.now(timezone.utc).isoformat()
                freq_mhz = (row["frequency"] or 0) / 1_000_000
                alerts.append({
                    "id": f"relay-{row['id']}", "type": "relay",
                    "time": t, "from_call": fr,
                    "from_name": opname or fr,
                    "relay_station": fr,
                    "frequency_mhz": f"{freq_mhz:.4f}" if freq_mhz else "",
                    "urgent": bool(row["urgent"]),
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
                    relay_info += ". If VarAC is running, it may retrieve the message. Otherwise, the message may need to be collected manually from VarAC."
                    send_pushover(f"Relay alert from {name}", relay_info)

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
        if p and os.path.isfile(p) and not _hwm_initialized:
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
# Flask
# ---------------------------------------------------------------------------
app = Flask(__name__)

# Simple CSRF protection: generate a token per session, validate on POST
_csrf_token = str(uuid.uuid4())

@app.before_request
def _csrf_check():
    """Validate CSRF token on state-changing POST requests."""
    if request.method == "POST":
        # Skip CSRF for pushover_reply (GET endpoint) and internal calls
        token = request.headers.get("X-CSRF-Token", "")
        if token != _csrf_token:
            # Also check JSON body as fallback
            try:
                body = request.get_json(silent=True) or {}
                token = body.get("_csrf", "")
            except Exception:
                pass
            if token != _csrf_token:
                return jsonify({"ok": False, "error": "Invalid CSRF token"}), 403

@app.route("/")
def index():
    return render_template_string(HTML_PAGE)

@app.route("/api/status")
def api_status():
    # Snapshot config under cfglock FIRST (never nest slock -> cfglock)
    with cfglock:
        cfg_snap = {
            "varac_db_path": config.get("varac_db_path", ""),
            "varac_exe_path": config.get("varac_exe_path", ""),
            "varac_profile": config.get("varac_profile", ""),
            "bbs_directory": config.get("bbs_directory", ""),
            "poll_interval_seconds": config.get("poll_interval_seconds", 15),
            "watch_callsigns": list(config.get("watch_callsigns", [])),
            "operator_name": config.get("operator_name", ""),
            "home_callsign": config.get("home_callsign", ""),
            "alert_sound": config.get("alert_sound", "gentle"),
            "alert_volume": config.get("alert_volume", 0.3),
            "quick_replies": list(config.get("quick_replies", [])),
            "pushover": {
                "enabled": config.get("pushover", {}).get("enabled", False),
                "user_key": config.get("pushover", {}).get("user_key", ""),
                "api_token": config.get("pushover", {}).get("api_token", ""),
                "priority": config.get("pushover", {}).get("priority", 1),
                "sound": config.get("pushover", {}).get("sound", "pushover"),
            },
            "aprs": {
                "enabled": config.get("aprs", {}).get("enabled", False),
                "home_ssid": config.get("aprs", {}).get("home_ssid", "-5"),
                "traveler_ssids": config.get("aprs", {}).get("traveler_ssids", "") or config.get("aprs", {}).get("traveler_ssid", "-7"),
                "passcode": config.get("aprs", {}).get("passcode", ""),
                "server": config.get("aprs", {}).get("server", "rotate.aprs2.net"),
                "port": config.get("aprs", {}).get("port", 14580),
                "rf_fallback": config.get("aprs", {}).get("rf_fallback", False),
                "use_mailbox": config.get("aprs", {}).get("use_mailbox", False),
            },
            "pat": {
                "enabled": config.get("pat", {}).get("enabled", False),
                "exe_path": config.get("pat", {}).get("exe_path", ""),
                "http_addr": config.get("pat", {}).get("http_addr", "localhost:8080"),
                "auto_launch": config.get("pat", {}).get("auto_launch", True),
                "poll_interval": config.get("pat", {}).get("poll_interval", 30),
                "home_tactical": config.get("pat", {}).get("home_tactical", ""),
                "traveler_tactical": config.get("pat", {}).get("traveler_tactical", ""),
                "rf_fallback": config.get("pat", {}).get("rf_fallback", False),
                "rf_gateway": config.get("pat", {}).get("rf_gateway", ""),
                "varafm_addr": config.get("pat", {}).get("varafm_addr", "localhost:8300"),
                "varafm_exe_path": config.get("pat", {}).get("varafm_exe_path", ""),
            },
            "soundmodem": {
                "enabled": config.get("soundmodem", {}).get("enabled", False),
                "exe_path": config.get("soundmodem", {}).get("exe_path", ""),
                "kiss_host": config.get("soundmodem", {}).get("kiss_host", "127.0.0.1"),
                "kiss_port": config.get("soundmodem", {}).get("kiss_port", 8100),
                "auto_launch": config.get("soundmodem", {}).get("auto_launch", True),
            },
            "beacon": {
                "enabled": config.get("beacon", {}).get("enabled", False),
                "lat": config.get("beacon", {}).get("lat", 0),
                "lon": config.get("beacon", {}).get("lon", 0),
                "symbol_table": config.get("beacon", {}).get("symbol_table", "/"),
                "symbol_code": config.get("beacon", {}).get("symbol_code", "-"),
                "comment": config.get("beacon", {}).get("comment", "HomeLink Radio"),
                "interval_minutes": config.get("beacon", {}).get("interval_minutes", 30),
                "via_aprsis": config.get("beacon", {}).get("via_aprsis", True),
                "via_rf": config.get("beacon", {}).get("via_rf", True),
            },
        }
    # Resolve BBS directory outside cfglock to avoid deadlock
    cfg_snap["bbs_directory_resolved"] = get_bbs_directory() or ""
    # Now snapshot state under slock (no cfglock held — no deadlock possible)
    with slock:
        for a in state["pending_alerts"] + state["history"]:
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
            "aprs_last_position": state["aprs_last_position"],
            "config": cfg_snap,
            "csrf_token": _csrf_token,
        })

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

@app.route("/api/stop_alarm", methods=["POST"])
def api_stop_alarm():
    """Stop the PC speaker alarm without removing alerts from pending."""
    stop_speaker_alarm()
    return jsonify({"ok": True})

@app.route("/api/pushover_reply")
def api_pushover_reply():
    """Handle quick reply links clicked from Pushover notifications.
    This is a GET endpoint so it works as a clickable URL in the notification."""
    channel = request.args.get("channel", "varac")
    msg = request.args.get("message", "").strip()
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
                    result = f"Sent via VarAC VMail to {to_call}"
                    log_reply(to_call, msg)
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

    # Return a simple confirmation page
    return f"""<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
    <style>body{{font-family:system-ui;text-align:center;padding:40px 20px;background:#f0fdf4;color:#166534}}
    h2{{font-size:20px}}p{{color:#64748b;margin-top:8px}}</style></head>
    <body><h2>✓ Reply Sent</h2><p>{result}</p><p>"{msg[:50]}"</p></body></html>"""


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
    # Clear Pat's internal schedule — HomeLink controls sync timing now
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
        import subprocess
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
            import re
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

        cur.execute("""INSERT INTO vmail
            (guid, creation_time, sent_time, received_time,
             folder_id, vmail_to, vmail_from, vmail_via,
             delivery_band, delivery_snr, subject, msg,
             read_status, is_deleted, frequency, has_attachment, urgent)
            VALUES (?, ?, NULL, NULL, ?, ?, ?, '', '', '', ?, ?, 0, 0, 0, 0, 0)""",
            (g, now_utc, outbox_id, to_call, home_call, subject, msg_text))

        conn.commit()
        conn.close()

        with slock:
            state["reply_status"] = {
                "time": now_utc, "to": to_call,
                "message": msg_text[:100], "status": "queued"
            }

        log.info("Reply queued to %s: %s", to_call, msg_text[:60])
        log_reply(to_call, msg_text)
        return jsonify({"ok": True, "to": to_call})

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
                   "alert_sound", "alert_volume", "operator_name", "home_callsign",
                   "quick_replies"]:
            if k in d:
                config[k] = d[k]
        if "pushover" in d:
            for pk in ["enabled", "user_key", "api_token", "priority", "sound"]:
                if pk in d["pushover"]:
                    config["pushover"][pk] = d["pushover"][pk]
        if "aprs" in d:
            for ak in ["enabled", "home_ssid", "traveler_ssids", "passcode", "server", "port", "use_mailbox", "rf_fallback"]:
                if ak in d["aprs"]:
                    config["aprs"][ak] = d["aprs"][ak]
        if "soundmodem" in d:
            for sk in ["enabled", "exe_path", "kiss_host", "kiss_port", "auto_launch"]:
                if sk in d["soundmodem"]:
                    config["soundmodem"][sk] = d["soundmodem"][sk]
        if "pat" in d:
            for pk2 in ["enabled", "exe_path", "http_addr", "auto_launch", "poll_interval", "home_tactical", "traveler_tactical", "rf_fallback", "rf_gateway", "varafm_addr", "varafm_exe_path"]:
                if pk2 in d["pat"]:
                    config["pat"][pk2] = d["pat"][pk2]
        if "beacon" in d:
            for bk in ["enabled", "lat", "lon", "symbol_table", "symbol_code", "comment", "interval_minutes", "via_aprsis", "via_rf"]:
                if bk in d["beacon"]:
                    config["beacon"][bk] = d["beacon"][bk]
        save_config(config)
        new_db = config.get("varac_db_path", "")
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
    if beacon_on:
        start_beacon()
    return jsonify({"ok": True})


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
    import re
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
    from datetime import datetime, timezone
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

    # Send APRS bulletin announcement
    bulletin_sent = False
    bulletin_msg = ""
    with cfglock:
        aprs_on = config.get("aprs", {}).get("enabled", False)
    if aprs_on:
        freq = get_varac_frequency()
        qsy = get_varac_next_qsy()
        # Build bulletin — keep it tight for the 67 char APRS limit
        parts = [f"SITREP#{num:03d} {callsign} BBS"]
        if freq:
            parts.append(f"{freq}MHz")
        if qsy:
            parts.append(f"QSY {qsy[0]}Z {qsy[1]}")
        parts.append("pls relay")
        bulletin_msg = " ".join(parts)[:67]
        ok, err = aprs_send_bulletin("1", bulletin_msg)
        bulletin_sent = ok
        if not ok:
            log.warning("Sitrep APRS bulletin failed: %s", err)

    # Send VarAC broadcast via UI automation
    varac_broadcast_sent = False
    varac_broadcast_msg = ""
    freq = get_varac_frequency()
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
                    "bulletin_sent": bulletin_sent, "bulletin_msg": bulletin_msg,
                    "varac_broadcast_sent": varac_broadcast_sent,
                    "varac_broadcast_msg": varac_broadcast_msg})


@app.route("/api/sitrep/latest")
def api_sitrep_latest():
    """Return the latest sitrep content for pre-filling the form."""
    bbs_dir = get_bbs_directory()
    if not bbs_dir or not os.path.isdir(bbs_dir):
        return jsonify({"ok": False})
    import re
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
        import urllib.request, urllib.parse
        p = {"token": at, "user": uk, "title": "HomeLink Radio",
             "message": "Notifications are working!", "priority": 0, "sound": "pushover"}
        data = urllib.parse.urlencode(p).encode()
        r = urllib.request.Request("https://api.pushover.net/1/messages.json", data=data)
        urllib.request.urlopen(r, timeout=10)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/browse_path", methods=["POST"])
def api_browse():
    d = request.get_json(force=True)
    p = d.get("path", "")
    if not p:
        if sys.platform == "win32":
            import string
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
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>HomeLink Radio</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#f8f9fb;--surface:#fff;--surface2:#f1f3f7;
  --border:#e2e5eb;--accent:#2563eb;--accent-light:#dbeafe;
  --green:#16a34a;--green-light:#dcfce7;--green-bg:#f0fdf4;
  --red:#dc2626;--red-light:#fee2e2;--red-bg:#fef2f2;
  --orange:#ea580c;--orange-light:#ffedd5;
  --text:#1e293b;--text2:#475569;--text3:#94a3b8;
  --shadow:0 1px 3px rgba(0,0,0,0.08),0 1px 2px rgba(0,0,0,0.04);
  --shadow-lg:0 4px 16px rgba(0,0,0,0.1);
  --radius:14px;
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',system-ui,sans-serif;background:var(--bg);color:var(--text);
  min-height:100vh;min-height:100dvh;-webkit-font-smoothing:antialiased}

/* HEADER */
.header{background:var(--surface);border-bottom:1px solid var(--border);padding:16px 20px;
  display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;
  box-shadow:var(--shadow)}
.header-left{display:flex;align-items:center;gap:12px}
.logo{width:42px;height:42px;background:var(--green);border-radius:12px;
  display:flex;align-items:center;justify-content:center;font-size:22px;color:#fff;
  box-shadow:0 2px 8px rgba(22,163,74,0.3)}
.header h1{font-size:18px;font-weight:800;color:var(--text)}
.header .sub{font-size:12px;color:var(--text3);font-weight:500;margin-top:1px}
.btn-gear{background:none;border:1px solid var(--border);width:38px;height:38px;border-radius:10px;
  cursor:pointer;font-size:18px;display:flex;align-items:center;justify-content:center;
  color:var(--text2);transition:all .2s}
.btn-gear:hover{background:var(--surface2);color:var(--accent)}

/* MAIN */
.main{max-width:640px;margin:0 auto;padding:16px}

/* STATUS CARD */
.status-card{background:var(--surface);border-radius:var(--radius);padding:24px;
  margin-bottom:16px;box-shadow:var(--shadow);text-align:center}
.status-icon{font-size:56px;margin-bottom:8px}
.status-title{font-size:22px;font-weight:800;margin-bottom:4px}
.status-sub{font-size:14px;color:var(--text2);line-height:1.4}
.status-card.ok{background:var(--green-bg);border:2px solid var(--green)}
.status-card.ok .status-title{color:var(--green)}
.status-card.waiting{background:var(--surface);border:2px solid var(--border)}
.status-card.alert{background:var(--red-bg);border:2px solid var(--red);
  animation:pulse-border 2s ease-in-out infinite}
@keyframes pulse-border{0%,100%{box-shadow:0 0 0 0 rgba(220,38,38,0.2)}50%{box-shadow:0 0 0 12px rgba(220,38,38,0)}}

/* CONNECTION DOT */
.conn{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--text3);
  font-weight:500;margin-top:12px}
.conn-dot{width:8px;height:8px;border-radius:50%;background:var(--green);
  box-shadow:0 0 6px rgba(22,163,74,0.5);animation:blink 2s infinite}
.conn-dot.off{background:var(--text3);box-shadow:none;animation:none}
.conn-dot.err{background:var(--red);box-shadow:0 0 6px rgba(220,38,38,0.5)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.4}}

/* MESSAGE CARD */
.msg-card{background:var(--surface);border-radius:var(--radius);padding:20px;
  margin-bottom:12px;box-shadow:var(--shadow);border-left:4px solid var(--accent);
  transition:all .2s}
.msg-card.unread{border-left-color:var(--green);background:var(--green-bg)}
.msg-card.urgent{border-left-color:var(--red);background:var(--red-bg)}
.msg-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.msg-from{font-weight:700;font-size:16px}
.msg-time{font-size:12px;color:var(--text3);font-weight:500}
.msg-subject{font-size:14px;font-weight:600;color:var(--text);margin-bottom:6px}
.msg-body{font-size:15px;color:var(--text2);line-height:1.6;white-space:pre-wrap;word-wrap:break-word}
.msg-badge{display:inline-block;font-size:10px;font-weight:700;text-transform:uppercase;
  letter-spacing:.5px;padding:2px 8px;border-radius:6px;margin-right:6px}
.badge-new{background:var(--green-light);color:var(--green)}
.badge-urgent{background:var(--red-light);color:var(--red)}
.badge-relay{background:var(--orange-light);color:var(--orange)}

.msg-actions{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}
.btn-small{background:var(--surface2);border:1px solid var(--border);color:var(--text);
  font-size:13px;font-weight:600;padding:8px 16px;border-radius:10px;cursor:pointer;transition:all .15s}
.btn-small:hover{background:var(--accent-light);color:var(--accent);border-color:var(--accent)}
.btn-small.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
.btn-small.primary:hover{background:#1d4ed8}
.btn-small.green{background:var(--green);color:#fff;border-color:var(--green)}

/* REPLY BOX */
.reply-box{background:var(--surface);border-radius:var(--radius);padding:20px;
  margin-bottom:16px;box-shadow:var(--shadow)}
.reply-box h3{font-size:16px;font-weight:700;margin-bottom:12px}
.reply-quick{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px}
.reply-quick button{background:var(--accent-light);color:var(--accent);border:1px solid transparent;
  font-size:13px;font-weight:600;padding:8px 14px;border-radius:20px;cursor:pointer;
  transition:all .15s}
.reply-quick button:hover{background:var(--accent);color:#fff}
.reply-textarea{width:100%;border:1px solid var(--border);border-radius:10px;padding:12px;
  font-size:15px;font-family:inherit;resize:vertical;min-height:80px;outline:none;
  transition:border-color .2s}
.reply-textarea:focus{border-color:var(--accent)}
.reply-send-row{display:flex;justify-content:space-between;align-items:center;margin-top:10px}
.reply-status{font-size:12px;color:var(--text3)}
.btn-send{background:var(--green);color:#fff;border:none;font-size:14px;font-weight:700;
  padding:12px 28px;border-radius:10px;cursor:pointer;transition:all .15s}
.btn-send:hover{background:#15803d;transform:translateY(-1px);box-shadow:0 4px 12px rgba(22,163,74,0.3)}
.btn-send:disabled{background:var(--text3);cursor:not-allowed;transform:none;box-shadow:none}

.section-label{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:1px;
  color:var(--text3);margin:24px 0 12px;display:flex;align-items:center;gap:8px}
.section-label::after{content:'';flex:1;height:1px;background:var(--border)}

.empty{text-align:center;padding:40px 20px;color:var(--text3)}
.empty .icon{font-size:36px;margin-bottom:8px;opacity:.5}
.empty p{font-size:13px}

.error-bar{background:var(--red-light);border:1px solid rgba(220,38,38,.2);color:var(--red);
  border-radius:10px;padding:12px 16px;margin-bottom:12px;font-size:13px;display:none}

/* TOAST */
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(20px);
  background:var(--green);color:#fff;font-weight:700;font-size:14px;
  padding:12px 28px;border-radius:12px;z-index:300;opacity:0;transition:all .3s;pointer-events:none;
  box-shadow:var(--shadow-lg)}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

/* SOUND SPLASH */
.splash{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(255,255,255,.95);
  display:flex;align-items:center;justify-content:center;z-index:10000}
.splash-inner{text-align:center;max-width:380px;padding:40px}

/* Compliance notice */
.compliance{position:fixed;top:0;left:0;right:0;bottom:0;background:var(--bg);
  display:flex;align-items:center;justify-content:center;z-index:10001;overflow-y:auto}
.compliance-inner{max-width:600px;padding:32px;margin:20px}
.compliance h2{font-size:22px;font-weight:800;color:var(--text);margin-bottom:4px}
.compliance .sub{font-size:13px;color:var(--text3);margin-bottom:20px}
.compliance-body{text-align:left;background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);padding:24px;margin-bottom:20px;max-height:55vh;overflow-y:auto;
  font-size:13.5px;line-height:1.7;color:var(--text2)}
.compliance-body h3{font-size:14px;font-weight:700;color:var(--text);margin:18px 0 8px;padding-top:12px;border-top:1px solid var(--border)}
.compliance-body h3:first-child{margin-top:0;padding-top:0;border-top:none}
.compliance-body strong{color:var(--text)}
.compliance-body .ref{font-size:12px;color:var(--text3);font-style:italic}
.compliance-check{display:flex;align-items:flex-start;gap:10px;margin-bottom:16px;
  font-size:13px;color:var(--text);text-align:left;cursor:pointer;user-select:none}
.chk-box{font-size:22px;line-height:1;flex-shrink:0;width:24px;text-align:center}
.chk-box.checked{color:var(--green)}
.btn-accept{width:100%;background:var(--text3);color:#fff;border:none;font-size:15px;font-weight:700;
  padding:14px;border-radius:12px;cursor:not-allowed;transition:all .2s}
.btn-accept.ready{background:var(--green);cursor:pointer}
.btn-accept.ready:hover{background:#15803d}
.splash .icon{font-size:64px;margin-bottom:16px}
.splash h2{font-size:24px;font-weight:800;margin-bottom:8px;color:var(--text)}
.splash p{font-size:15px;color:var(--text2);margin-bottom:28px;line-height:1.5}
.btn-start{background:var(--green);color:#fff;border:none;font-size:16px;font-weight:700;
  padding:16px 40px;border-radius:14px;cursor:pointer;transition:all .2s;
  box-shadow:0 4px 14px rgba(22,163,74,0.3)}
.btn-start:hover{transform:translateY(-2px);box-shadow:0 6px 20px rgba(22,163,74,0.4)}

/* SETTINGS SLIDE-OVER */
.sett-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.3);
  z-index:200;display:none;backdrop-filter:blur(4px)}
.sett-overlay.open{display:block}
.sett-panel{position:fixed;top:0;right:0;bottom:0;width:440px;max-width:100vw;
  background:var(--bg);z-index:201;overflow-y:auto;transform:translateX(100%);
  transition:transform .3s}
.sett-overlay.open .sett-panel{transform:translateX(0)}
.sett-head{padding:20px;border-bottom:1px solid var(--border);display:flex;
  align-items:center;justify-content:space-between;background:var(--surface);
  position:sticky;top:0;z-index:1}
.sett-head h2{font-size:18px;font-weight:800}
.btn-x{background:none;border:1px solid var(--border);width:32px;height:32px;border-radius:8px;
  cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center;color:var(--text2)}
.sett-body{padding:20px}
.sett-section{background:var(--surface);border-radius:var(--radius);padding:20px;
  margin-bottom:16px;box-shadow:var(--shadow)}
.sett-section h3{font-size:14px;font-weight:700;color:var(--accent);margin-bottom:14px}
.fg{margin-bottom:14px}
.fl{display:block;font-size:12px;font-weight:600;color:var(--text2);margin-bottom:5px}
.fi{width:100%;border:1px solid var(--border);border-radius:8px;padding:10px 12px;
  font-size:14px;font-family:inherit;outline:none;transition:border .2s;background:var(--bg)}
.fi:focus{border-color:var(--accent)}
.fsel{width:100%;border:1px solid var(--border);border-radius:8px;padding:10px 12px;
  font-size:14px;background:var(--bg);cursor:pointer;outline:none}
.fhint{font-size:11px;color:var(--text3);margin-top:3px}
.frow{display:flex;gap:8px;align-items:flex-end}
.frow .fg{flex:1;margin-bottom:0}
.btn-a{background:var(--surface2);border:1px solid var(--border);color:var(--text);
  font-size:12px;font-weight:600;padding:10px 14px;border-radius:8px;cursor:pointer;white-space:nowrap}
.btn-a:hover{border-color:var(--accent);color:var(--accent)}
.btn-a.pri{background:var(--accent);color:#fff;border-color:var(--accent)}
.btn-save{width:100%;background:var(--accent);color:#fff;border:none;font-size:15px;font-weight:700;
  padding:14px;border-radius:12px;cursor:pointer;transition:all .2s;margin-top:4px}
.btn-save:hover{background:#1d4ed8}

.tgl-row{display:flex;align-items:center;justify-content:space-between}
.tgl{width:44px;height:24px;background:var(--border);border-radius:12px;position:relative;
  cursor:pointer;transition:background .2s;flex-shrink:0}
.tgl.on{background:var(--green)}
.tgl::after{content:'';position:absolute;top:2px;left:2px;width:20px;height:20px;
  background:#fff;border-radius:50%;transition:transform .2s;box-shadow:0 1px 3px rgba(0,0,0,.15)}
.tgl.on::after{transform:translateX(20px)}

.fb{background:var(--bg);border:1px solid var(--border);border-radius:8px;
  max-height:180px;overflow-y:auto;margin-top:6px;display:none}
.fb.open{display:block}
.fe{padding:8px 12px;font-size:13px;cursor:pointer;display:flex;align-items:center;gap:8px;
  border-bottom:1px solid var(--border);transition:background .1s}
.fe:last-child{border-bottom:none}
.fe:hover{background:var(--surface2)}
.fe.dir{color:var(--accent);font-weight:600}
.fe.file{color:var(--orange);font-weight:600}
.fc{padding:5px 10px;font-size:11px;color:var(--text3);background:var(--surface2);
  border-bottom:1px solid var(--border)}

.tr{margin-top:6px;padding:10px 12px;border-radius:8px;font-size:12px;display:none}
.tr.ok{display:block;background:var(--green-light);color:var(--green)}
.tr.err{display:block;background:var(--red-light);color:var(--red)}

input[type=range]{-webkit-appearance:none;width:100%;height:6px;background:var(--border);
  border-radius:3px;outline:none}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:20px;height:20px;
  background:var(--accent);border-radius:50%;cursor:pointer;box-shadow:0 1px 4px rgba(0,0,0,.15)}

.hidden{display:none!important}
@media(max-width:500px){
  .header h1{font-size:16px}
  .status-icon{font-size:44px}
  .status-title{font-size:18px}
  .msg-from{font-size:15px}
  .sett-panel{width:100vw}
}
/* SITREP OVERLAY */
.sitrep-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.3);
  display:none;z-index:300}
.sitrep-overlay.open{display:block}
.sitrep-panel{position:fixed;top:0;right:0;bottom:0;width:460px;background:var(--surface);
  overflow-y:auto;transform:translateX(100%);transition:transform .3s ease;
  box-shadow:-4px 0 20px rgba(0,0,0,.1);padding:24px}
.sitrep-overlay.open .sitrep-panel{transform:translateX(0)}
.sitrep-field{margin-bottom:14px}
.sitrep-field label{display:block;font-size:13px;font-weight:700;color:var(--text);margin-bottom:4px}
.sitrep-field input,.sitrep-field textarea{width:100%;padding:10px 12px;border:1px solid var(--border);
  border-radius:8px;font-size:14px;font-family:inherit;background:var(--surface)}
.sitrep-field textarea{resize:vertical;min-height:60px}
.sitrep-field .sitrep-hint{font-size:11px;color:var(--text3);margin-top:2px}
.sitrep-actions{display:flex;gap:10px;margin-top:20px}
.sitrep-actions button{flex:1;padding:14px;border-radius:10px;font-size:15px;font-weight:700;
  border:none;cursor:pointer;transition:all .2s}
.btn-sitrep-ok{background:var(--green);color:#fff}
.btn-sitrep-ok:hover{background:#15803d}
.btn-sitrep-send{background:var(--accent);color:#fff}
.btn-sitrep-send:hover{background:#1d4ed8}
.btn-sitrep-cancel{background:var(--surface2);color:var(--text2);flex:0.5!important}
.sitrep-result{margin-top:12px;padding:10px;border-radius:8px;font-size:13px;font-weight:600;display:none;white-space:pre-line}
.sitrep-result.ok{display:block;background:var(--green-light);color:var(--green)}
.sitrep-result.err{display:block;background:var(--red-light);color:var(--red)}
@media(max-width:520px){.sitrep-panel{width:100vw}}
</style>
</head>
<body>

<!-- REGULATORY COMPLIANCE NOTICE -->
<div class="compliance" id="complianceScreen">
  <div class="compliance-inner">
    <div style="text-align:center;font-size:40px;margin-bottom:12px">⚖️</div>
    <h2 style="text-align:center">Regulatory Notice</h2>
    <div class="sub" style="text-align:center">Please read and acknowledge before using this application.</div>
    <div class="compliance-body">
      <h3>Amateur Radio License Required</h3>
      This application interfaces with amateur (ham) radio equipment operating under FCC Part 97 rules.
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

      <br><br><strong>2. Data emission exception.</strong>
      FCC 97.115(c) states: "No station may transmit third party communications while being automatically
      controlled except a station transmitting a RTTY or data emission." Both VarAC (VARA data mode)
      and APRS are classified as data emissions, which qualifies for this exception. This is the same
      model used by Winlink, packet BBS, and APRS messaging systems.

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

      <br><br><span class="ref">References: 47 CFR §97.3, §97.109, §97.113, §97.115, §97.119, §97.403</span>
    </div>

    <div class="compliance-check" id="chk1" onclick="toggleCheck(1)">
      <div class="chk-box" id="chkBox1">&#x2610;</div>
      <span>I am (or am acting on behalf of) a licensed amateur radio operator who is the designated control operator of this station.</span>
    </div>
    <div class="compliance-check" id="chk2" onclick="toggleCheck(2)">
      <div class="chk-box" id="chkBox2">&#x2610;</div>
      <span>I understand the third-party communication rules described above and will ensure all use of this application complies with FCC Part 97.</span>
    </div>
    <div class="compliance-check" id="chk3" onclick="toggleCheck(3)">
      <div class="chk-box" id="chkBox3">&#x2610;</div>
      <span>I understand that the station licensee is responsible for all transmissions initiated through this application.</span>
    </div>

    <button class="btn-accept" id="btnAccept" onclick="acceptCompliance()">I Understand &amp; Accept</button>
  </div>
</div>

<!-- SPLASH -->
<div class="splash hidden" id="splash">
  <div class="splash-inner">
    <div class="icon">📡</div>
    <h2>HomeLink Radio</h2>
    <p>This app watches for check-in messages from your loved one (who is traveling) sent to this home radio. You'll get alerts when they check in, and you can send replies and post sitreps.</p>
    <button class="btn-start" onclick="start()">Start</button>
  </div>
</div>

<div class="toast" id="toast"></div>

<!-- SETTINGS -->
<div class="sett-overlay" id="settOverlay" onclick="if(event.target===this)closeSett()">
  <div class="sett-panel">
    <div class="sett-head"><h2>Settings</h2><button class="btn-x" onclick="closeSett()">✕</button></div>
    <div class="sett-body">

      <div class="sett-section">
        <h3>Operator Info</h3>
        <div class="fg"><label class="fl">Their Name (shown in alerts)</label>
          <input class="fi" id="cName" placeholder="e.g. John"></div>
        <div class="fg"><label class="fl">Home Station Callsign (for replies)</label>
          <input class="fi" id="cHomeCall" placeholder="e.g. W1AW"></div>
        <div class="fg"><label class="fl">Watch For Callsign(s)</label>
          <input class="fi" id="cWatch" placeholder="e.g. W1AW/P, W1AW">
          <div class="fhint">Comma-separated. Leave empty to watch all.</div></div>
      </div>

      <div class="sett-section">
        <h3>VarAC</h3>
        <div class="fg"><label class="fl">VarAC Executable Path</label>
          <input class="fi" id="cVaracExe" placeholder="C:\VarAC\VarAC.exe">
          <div class="fhint">Full path to VarAC — launched automatically on startup if not already running. Leave blank to skip.</div></div>
        <div class="fg"><label class="fl">VarAC Profile (.ini file name)</label>
          <input class="fi" id="cVaracProfile" placeholder="e.g. varac_7300.ini" style="width:220px">
          <div class="fhint">Optional — the .ini profile filename to use (e.g. varac_7300.ini). Leave blank for the default profile.</div></div>
        <div class="fg"><label class="fl">BBS Directory Override</label>
          <input class="fi" id="cBbsDir" placeholder="Auto-read from VarAC .ini">
          <div class="fhint">Override the BBS folder path. Leave blank to auto-read from the VarAC profile .ini file.</div>
          <div class="fhint" id="bbsResolved" style="color:#16a34a"></div></div>
        <div class="fg"><label class="fl">VarAC Database Path</label>
          <div class="frow">
            <div class="fg"><input class="fi" id="cDb" placeholder="C:\VarAC\VarAC.db"></div>
            <button class="btn-a" onclick="browse('')">Browse</button>
            <button class="btn-a pri" onclick="testDb()">Test</button>
          </div>
          <div class="fb" id="fb"></div><div class="tr" id="dbTr"></div></div>
        <div class="fg"><label class="fl">Check every (seconds)</label>
          <input class="fi" id="cPoll" type="number" min="5" max="300" value="15" style="width:120px"></div>
      </div>

      <div class="sett-section">
        <h3>Alert Sound</h3>
        <div class="fg"><label class="fl">Sound</label>
          <select class="fsel" id="cSound">
            <option value="gentle">Gentle Chime</option>
            <option value="two_tone">Two-Tone Beep</option>
            <option value="sonar">Sonar Ping</option>
            <option value="siren">Rising Siren</option>
            <option value="klaxon">Klaxon Horn</option>
            <option value="telegraph">Telegraph</option>
            <option value="voice">Voice Alert</option>
          </select></div>
        <div class="fg"><label class="fl">Volume: <span id="volL">30%</span></label>
          <input type="range" id="cVol" min="0" max="100" value="30"
            oninput="document.getElementById('volL').textContent=this.value+'%'"></div>
        <button class="btn-a" onclick="preview()">▶ Preview</button>
      </div>

      <div class="sett-section">
        <h3>Quick Replies</h3>
        <div class="fg"><label class="fl">Pre-written messages (one per line)</label>
          <textarea class="fi" id="cQuick" rows="5" style="resize:vertical;min-height:80px"></textarea>
          <div class="fhint">These appear as one-tap reply buttons.</div></div>
      </div>

      <div class="sett-section">
        <h3>Phone Notifications (Pushover)</h3>
        <div class="fg"><div class="tgl-row"><label class="fl" style="margin:0">Enable</label>
          <div class="tgl" id="cPoOn" onclick="this.classList.toggle('on')"></div></div></div>
        <div class="fg"><label class="fl">User Key</label><input class="fi" id="cPoUser" placeholder="Pushover user key"></div>
        <div class="fg"><label class="fl">API Token</label><input class="fi" id="cPoToken" placeholder="Pushover app token"></div>
        <div class="fg"><label class="fl">Priority</label>
          <select class="fsel" id="cPoPri">
            <option value="0">Normal</option><option value="1">High</option>
            <option value="2">Emergency (repeats until acknowledged)</option></select></div>
        <div class="fg"><label class="fl">Sound</label>
          <select class="fsel" id="cPoSnd">
            <option value="pushover">Pushover</option><option value="bike">Bike</option>
            <option value="bugle">Bugle</option><option value="cosmic">Cosmic</option>
            <option value="falling">Falling</option><option value="incoming">Incoming</option>
            <option value="magic">Magic</option><option value="persistent">Persistent</option>
            <option value="siren">Siren</option><option value="spacealarm">Space Alarm</option>
            <option value="none">Silent</option></select></div>
        <div class="fg"><button class="btn-a" onclick="testPo()">Send Test</button>
          <div class="tr" id="poTr"></div></div>
      </div>

      <div class="sett-section">
        <h3>APRS Messaging (via iGates)</h3>
        <div class="fg"><div class="tgl-row"><label class="fl" style="margin:0">Enable APRS-IS</label>
          <div class="tgl" id="cAprsOn" onclick="this.classList.toggle('on')"></div></div>
          <div class="fhint">Connects to APRS-IS for two-way short messages and position tracking</div></div>
        <div class="fg"><label class="fl">Home Station SSID</label>
          <input class="fi" id="cAprsSsid" placeholder="-5" style="width:100px">
          <div class="fhint">Appended to your home callsign (e.g., W1AW-5). The app listens for messages to this.</div></div>
        <div class="fg"><label class="fl">Traveler SSID(s)</label>
          <input class="fi" id="cAprsTravSsid" placeholder="-7, -9" style="width:160px">
          <div class="fhint">Comma-separated SSIDs to monitor (e.g., -7, -9). Messages addressed to any of these will trigger alerts. Replies go to the sender.</div></div>
        <div class="fg"><label class="fl">APRS-IS Passcode</label>
          <div class="frow">
            <div class="fg"><input class="fi" id="cAprsPass" placeholder="12345"></div>
            <button class="btn-a" onclick="genPasscode()">Auto-Generate</button>
          </div>
          <div class="fhint">Derived from your callsign. Click Auto-Generate to calculate it.</div></div>
        <div class="fg"><label class="fl">APRS-IS Server</label>
          <input class="fi" id="cAprsSrv" placeholder="rotate.aprs2.net"></div>
        <div class="fg"><label class="fl">APRS-IS Port</label>
          <input class="fi" id="cAprsPort" placeholder="14580" style="width:120px"></div>
        <div class="fg"><div class="tgl-row"><label class="fl" style="margin:0">Send APRS via RF if no internet (requires Soundmodem)</label>
          <div class="tgl" id="cAprsRfFallback" onclick="this.classList.toggle('on')"></div></div>
          <div class="fhint">Automatically switches to sending via Soundmodem when internet is unavailable</div></div>
        <div class="fg"><div class="tgl-row"><label class="fl" style="margin:0">Copy messages to APRS Mailbox (NA7Q store &amp; forward)</label>
          <div class="tgl" id="cAprsMailbox" onclick="this.classList.toggle('on')"></div></div>
          <div class="fhint">Also sends a copy to the MAIL bot so the traveler can retrieve it later with APRSM, even with spotty coverage</div></div>
      </div>

      <div class="sett-section">
        <h3>Position Beacon</h3>
        <div class="fg"><div class="tgl-row"><label class="fl" style="margin:0">Enable Position Beacon</label>
          <div class="tgl" id="cBcnOn" onclick="this.classList.toggle('on')"></div></div>
          <div class="fhint">Periodically beacons your home station position so nearby iGates know you exist. Critical for receiving RF APRS messages when internet is down.</div></div>
        <div class="fg"><label class="fl">Latitude / Longitude</label>
          <div style="display:flex;gap:8px;align-items:center">
            <input class="fi" id="cBcnLat" type="number" step="0.0001" placeholder="33.7490" style="width:130px">
            <input class="fi" id="cBcnLon" type="number" step="0.0001" placeholder="-84.3880" style="width:130px">
            <button class="btn-small" onclick="detectBeaconLocation()" style="font-size:11px">📍 Detect</button>
          </div>
          <div class="fhint">Your home station coordinates. Click 📍 to auto-detect from browser.</div></div>
        <div class="fg"><label class="fl">Symbol</label>
          <div style="display:flex;gap:8px;align-items:center">
            <select class="fi" id="cBcnSymbol" style="width:200px">
              <option value="/- ">House (-)</option>
              <option value="/r ">Antenna (/r)</option>
              <option value="\\- ">Diamond (-)</option>
              <option value="/y ">House w/ Yagi (/y)</option>
              <option value="/# ">Digipeater (#)</option>
              <option value="/& ">Gateway (&)</option>
              <option value="/I ">TCP/IP Station (I)</option>
            </select>
          </div>
          <div class="fhint">APRS map symbol for your home station</div></div>
        <div class="fg"><label class="fl">Beacon Interval (minutes)</label>
          <input class="fi" id="cBcnInterval" type="number" min="5" max="120" value="30" style="width:100px">
          <div class="fhint">How often to transmit your position (5-120 minutes). 30 is typical for a fixed station.</div></div>
        <div class="fg"><label class="fl">Beacon Comment</label>
          <input class="fi" id="cBcnComment" placeholder="HomeLink Radio" style="width:250px">
          <div class="fhint">Short text appended to your beacon (visible on APRS maps)</div></div>
        <div class="fg"><div class="tgl-row"><label class="fl" style="margin:0">Beacon via APRS-IS (internet)</label>
          <div class="tgl" id="cBcnAprsIs" onclick="this.classList.toggle('on')"></div></div></div>
        <div class="fg"><div class="tgl-row"><label class="fl" style="margin:0">Beacon via RF (Soundmodem)</label>
          <div class="tgl" id="cBcnRf" onclick="this.classList.toggle('on')"></div></div>
          <div class="fhint">RF beaconing is essential for iGates to relay messages to you when internet is down</div></div>
      </div>

      <div class="sett-section">
        <h3>Winlink (via Pat)</h3>
        <div class="fg"><div class="tgl-row"><label class="fl" style="margin:0">Enable Winlink via Pat</label>
          <div class="tgl" id="cPatOn" onclick="this.classList.toggle('on')"></div></div>
          <div class="fhint">Connects to Pat Winlink client for email-like messaging through ham radio gateways</div></div>
        <div class="fg"><label class="fl">Pat Executable Path</label>
          <input class="fi" id="cPatPath" placeholder="C:\Pat\pat.exe">
          <div class="fhint">Full path to pat.exe — launched automatically on startup</div></div>
        <div class="fg"><label class="fl">Pat HTTP Address</label>
          <input class="fi" id="cPatAddr" placeholder="localhost:8080" style="width:200px">
          <div class="fhint">Default: localhost:8080</div></div>
        <div class="fg"><label class="fl">Check Winlink every (seconds)</label>
          <input class="fi" id="cPatPoll" type="number" min="60" max="900" value="300" style="width:120px">
          <div class="fhint">How often to sync with Winlink CMS and check for new messages. Minimum 60 seconds.</div></div>
        <div style="border-top:1px solid var(--border);margin-top:14px;padding-top:14px">
          <div style="font-size:13px;font-weight:600;color:var(--text2);margin-bottom:10px">Pat Winlink Account</div>
          <div class="fg"><label class="fl">Winlink Callsign</label>
            <input class="fi" id="cPatCall" placeholder="KK4ODA" style="width:160px">
            <div class="fhint">Your Winlink registered callsign</div></div>
          <div class="fg"><label class="fl">Winlink Password</label>
            <input class="fi" id="cPatPass" type="password" placeholder="Winlink password" style="width:200px">
            <div class="fhint">Secure login password from your Winlink account</div></div>
          <div class="fg"><label class="fl">Grid Locator</label>
            <div style="display:flex;gap:8px;align-items:center">
              <input class="fi" id="cPatLoc" placeholder="EM73" style="width:120px">
              <button class="btn-small" onclick="detectLocation()" style="font-size:11px">📍 Use My Location</button>
            </div>
            <div class="fhint">Maidenhead grid square — used to find nearby gateways. Click 📍 to auto-detect from your browser.</div></div>
          <div style="border-top:1px solid var(--border);margin-top:14px;padding-top:14px">
            <div style="font-size:13px;font-weight:600;color:var(--text2);margin-bottom:10px">Tactical Addresses</div>
            <div class="fhint" style="margin-bottom:10px">Winlink does not allow sending to yourself. Use tactical addresses so the home station and traveler have separate Winlink identities.</div>
            <div class="fg"><label class="fl">Home Station Address</label>
              <input class="fi" id="cPatHomeTac" placeholder="e.g. BRECKEN" style="width:180px">
              <div class="fhint">Your wife sends FROM this address (3-12 alpha chars, dash+numbers OK)</div></div>
            <div class="fg"><label class="fl">Traveler Address</label>
              <input class="fi" id="cPatTravTac" placeholder="e.g. FACUNDO" style="width:180px">
              <div class="fhint">Messages will be sent TO this address by default</div></div>
          </div>
        <div class="fg"><div class="tgl-row"><label class="fl" style="margin:0">Use VARA FM gateway if no internet</label>
          <div class="tgl" id="cPatRfFallback" onclick="this.classList.toggle('on')"></div></div>
          <div class="fhint">When internet is unavailable, Pat will connect to a nearby VARA FM gateway instead of telnet</div></div>
        <div class="fg"><label class="fl">VARA FM Gateway</label>
          <div style="display:flex;gap:8px;align-items:center">
            <input class="fi" id="cPatRfGw" placeholder="e.g. W3ADO-10" style="width:160px">
            <button class="btn-small" onclick="loadGateways()" style="font-size:11px">Find Nearby</button>
          </div>
          <div id="gwList" style="font-size:12px;color:var(--text2);margin-top:6px"></div>
          <div class="fhint">The VARA FM RMS gateway callsign to connect to when internet is down. Click Find Nearby to discover gateways.</div></div>
        <div class="fg"><label class="fl">VARA FM Executable Path</label>
          <input class="fi" id="cPatVaraExe" placeholder="C:\VARA FM\VARAFM.exe">
          <div class="fhint">Full path to VARA FM — launched automatically when Pat is enabled. Leave blank if VARA FM is already running.</div></div>
        <div class="fg"><label class="fl">VARA FM Modem Address</label>
          <input class="fi" id="cPatVaraAddr" placeholder="localhost:8300" style="width:200px">
          <div class="fhint">Default: localhost:8300. Must match VARA FM modem's TCP port.</div></div>
          <button class="btn-small primary" onclick="savePatConfig()" style="margin-top:8px">Save Pat Configuration</button>
          <span id="patSaveStatus" style="font-size:12px;color:var(--text3);margin-left:10px"></span>
        </div>
      </div>

      <div class="sett-section">
        <h3>APRS RF Monitor (Soundmodem)</h3>
        <div class="fg"><div class="tgl-row"><label class="fl" style="margin:0">Enable RF APRS via Soundmodem</label>
          <div class="tgl" id="cSmOn" onclick="this.classList.toggle('on')"></div></div>
          <div class="fhint">Connects to UZ7HO Soundmodem KISS port for RF APRS receive and transmit</div></div>
        <div class="fg"><label class="fl">Soundmodem Path</label>
          <input class="fi" id="cSmPath" placeholder="C:\Soundmodem\soundmodem.exe">
          <div class="fhint">Full path to soundmodem.exe — launched automatically on startup</div></div>
        <div class="fg"><label class="fl">KISS TCP Host</label>
          <input class="fi" id="cSmHost" placeholder="127.0.0.1" style="width:180px"></div>
        <div class="fg"><label class="fl">KISS TCP Port</label>
          <input class="fi" id="cSmPort" placeholder="8100" type="number" style="width:120px">
          <div class="fhint">Default: 8100. Must match Soundmodem KISS Server Port setting.</div></div>
      </div>

      <button class="btn-save" onclick="saveSett()">Save Settings</button>
      <div style="text-align:center;margin-top:16px">
        <button class="btn-a" onclick="showCompliance()" style="font-size:11px;color:var(--text3)">⚖️ View Regulatory Notice</button>
      </div>
    </div>
  </div>
</div>

<!-- SITREP OVERLAY -->
<div class="sitrep-overlay" id="sitrepOverlay" onclick="if(event.target===this)closeSitrep()">
  <div class="sitrep-panel">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
      <h2 style="font-size:20px;font-weight:800">📋 Post Sitrep</h2>
      <button class="btn-sitrep-cancel" onclick="closeSitrep()" style="flex:unset!important;padding:6px 14px;font-size:12px">✕ Close</button>
    </div>
    <div style="font-size:13px;color:var(--text2);margin-bottom:16px">
      Situation report posted to the BBS for relay. Next: <strong>#<span id="sitrepNum">001</span></strong>
    </div>
    <div class="sitrep-field"><label>House</label>
      <input id="srHouse" value="All OK"><div class="sitrep-hint">Status of the home — damage, flooding, structural issues</div></div>
    <div class="sitrep-field"><label>Vehicles</label>
      <input id="srVehicles" value="All OK"><div class="sitrep-hint">Car availability, fuel, damage</div></div>
    <div class="sitrep-field"><label>Utilities</label>
      <input id="srUtilities" value="All OK"><div class="sitrep-hint">Power, water, gas, internet status</div></div>
    <div class="sitrep-field"><label>Health</label>
      <input id="srHealth" value="All OK"><div class="sitrep-hint">Health status of family members</div></div>
    <div class="sitrep-field"><label>Immediate Needs</label>
      <input id="srNeeds" value="None"><div class="sitrep-hint">Anything urgently needed — fuel, medicine, supplies</div></div>
    <div class="sitrep-field"><label>Relocation</label>
      <input id="srRelocation" value="No change"><div class="sitrep-hint">Plans to move — staying put, evacuating, relocated</div></div>
    <div class="sitrep-field"><label>Location</label>
      <input id="srLocation" placeholder="GPS coords, address, or What3Words"><div class="sitrep-hint">Current location if relocated — leave blank if staying home</div></div>
    <div class="sitrep-field"><label>Remarks</label>
      <textarea id="srRemarks" placeholder="Any additional details..."></textarea></div>
    <div class="sitrep-actions">
      <button class="btn-sitrep-ok" onclick="sendQuickSitrep()">✅ Quick All-OK</button>
      <button class="btn-sitrep-send" onclick="sendSitrep()">📋 Post Sitrep</button>
    </div>
    <div class="sitrep-result" id="sitrepResult"></div>
  </div>
</div>

<!-- HEADER -->
<div class="header">
  <div class="header-left">
    <div class="logo">📡</div>
    <div><h1>HomeLink Radio</h1><div class="sub" id="headerSub">Waiting for messages...</div></div>
  </div>
  <button class="btn-gear" onclick="openSett()">⚙</button>
</div>

<!-- MAIN -->
<div class="main">
  <div class="error-bar" id="errBar"></div>

  <!-- STATUS -->
  <div class="status-card waiting" id="statusCard">
    <div class="status-icon" id="statusIcon">📻</div>
    <div class="status-title" id="statusTitle">Waiting for check-in...</div>
    <div class="status-sub" id="statusSub">The monitor is watching for messages.</div>
    <div class="conn"><div class="conn-dot off" id="connDot"></div><span id="connText">Connecting...</span></div>
    <div class="conn" id="aprsConn" style="margin-top:4px"><div class="conn-dot off" id="aprsDot"></div><span id="aprsText">APRS off</span></div>
    <div class="conn" id="kissConn" style="margin-top:4px"><div class="conn-dot off" id="kissDot"></div><span id="kissText">RF off</span></div>
    <div class="conn" id="patConn" style="margin-top:4px"><div class="conn-dot off" id="patDot"></div><span id="patText">Winlink off</span></div>
  </div>

  <!-- LOCATION CARD -->
  <div class="status-card waiting" id="locCard" style="display:none;text-align:left;padding:16px 20px">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
      <span style="font-size:24px">📍</span>
      <div>
        <div style="font-size:14px;font-weight:700" id="locTitle">Last Known Position</div>
        <div style="font-size:12px;color:var(--text3)" id="locTime"></div>
      </div>
    </div>
    <div style="font-size:13px;color:var(--text2)" id="locDetails"></div>
    <a id="locLink" href="#" target="_blank" style="display:inline-block;margin-top:8px;font-size:12px;font-weight:600;color:var(--accent);text-decoration:none">View on map →</a>
  </div>

  <!-- NEW MESSAGE BUTTONS -->
  <!-- SEND MESSAGE BUTTON -->
  <div id="newMsgBtns">
    <button class="btn-send" onclick="openCompose('multi')" id="btnSendMsg" style="width:100%;font-size:16px;padding:16px;border-radius:12px;display:none">
      ✉️ Send Message
    </button>
    <button class="btn-send" onclick="openSitrep()" id="btnSitrep" style="width:100%;font-size:16px;padding:16px;border-radius:12px;margin-top:8px;background:var(--accent);display:none">
      📋 Post Sitrep to BBS
    </button>
  </div>

  <!-- ACTIVE -->
  <div class="section-label">New Messages</div>
  <div id="activeArea"><div class="empty"><div class="icon">📭</div><p>No new messages</p></div></div>

  <!-- COMPOSE BOX (hidden until Send Message or Reply is tapped) -->
  <div class="reply-box hidden" id="replyBox">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h3 style="margin:0" id="composeTitle">✉️ Send Message</h3>
      <button class="btn-small" onclick="closeReply()" style="font-size:11px;padding:4px 12px">✕ Cancel</button>
    </div>
    <div class="reply-quick" id="quickBtns"></div>
    <textarea class="reply-textarea" id="replyText" placeholder="Type your message..." oninput="onComposeInput()"></textarea>
    <div id="aprsLengthWarn" style="font-size:11px;color:var(--red);margin-top:4px;display:none">Message too long for APRS (67 char limit)</div>
    <div style="margin-top:10px;font-size:12px;color:var(--text2)">
      <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:center">
        <label style="cursor:pointer;display:flex;align-items:center;gap:4px" id="chkAprsLabel">
          <input type="checkbox" id="chkAprs" checked> APRS <span style="color:var(--text3)">(short message)</span></label>
        <label style="cursor:pointer;display:flex;align-items:center;gap:4px" id="chkWinlinkLabel">
          <input type="checkbox" id="chkWinlink" checked> Winlink <span style="color:var(--text3)">(radio email)</span></label>
        <label style="cursor:pointer;display:flex;align-items:center;gap:4px" id="chkVaracLabel">
          <input type="checkbox" id="chkVarac" checked> VarAC <span style="color:var(--text3)">(peer to peer)</span></label>
      </div>
    </div>
    <div class="reply-send-row">
      <div class="reply-status" id="replyStatus"></div>
      <button class="btn-send" id="sendBtn" onclick="sendMulti()">Send</button>
    </div>
  </div>

  <!-- HISTORY -->
  <div class="section-label">Previous Messages</div>
  <div id="histArea"><div class="empty"><div class="icon">📋</div><p>No messages yet</p></div></div>

  <!-- MESSAGE LOG -->
  <div class="section-label" style="margin-top:32px">
    Saved Message Log
    <button class="btn-small" id="logToggleBtn" onclick="toggleLog()" style="margin-left:auto;font-size:11px">View Log</button>
  </div>
  <div id="logArea" class="hidden">
    <div id="logEntries"></div>
  </div>
</div>

<script>
let audioCtx=null,soundOn=false,alarmInt=null,dismissedIds=new Set(),cfg={};
let replyChan='varac';
let csrfToken='';

/* --- CSRF-safe POST helper --- */
function cpost(url,body){
  return fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:JSON.stringify(body||{})});
}

/* --- Compliance notice --- */
var compChecked={1:false,2:false,3:false};
function toggleCheck(n){
  compChecked[n]=!compChecked[n];
  var box=document.getElementById('chkBox'+n);
  if(compChecked[n]){box.innerHTML='\u2611';box.classList.add('checked')}
  else{box.innerHTML='\u2610';box.classList.remove('checked')}
  var btn=document.getElementById('btnAccept');
  if(compChecked[1]&&compChecked[2]&&compChecked[3]){btn.classList.add('ready')}
  else{btn.classList.remove('ready')}
}
function acceptCompliance(){
  if(!compChecked[1]||!compChecked[2]||!compChecked[3])return;
  try{localStorage.setItem('homelink_compliance_accepted','1')}catch(e){}
  document.getElementById('complianceScreen').classList.add('hidden');
  document.getElementById('splash').classList.remove('hidden');
}
function initCompliance(){
  let accepted=false;
  try{accepted=localStorage.getItem('homelink_compliance_accepted')==='1'}catch(e){}
  if(accepted){
    document.getElementById('complianceScreen').classList.add('hidden');
    document.getElementById('splash').classList.remove('hidden');
  }
}
function showCompliance(){
  closeSett();
  compChecked={1:true,2:true,3:true};
  for(var i=1;i<=3;i++){
    var box=document.getElementById('chkBox'+i);
    box.innerHTML='\u2611';box.classList.add('checked');
  }
  document.getElementById('btnAccept').classList.add('ready');
  document.getElementById('complianceScreen').classList.remove('hidden');
}
initCompliance();

let aprsManualUncheck=false;

let _replyToCall='';

function openReply(chan,replyTo){
  _replyToCall=replyTo||'';
  document.getElementById('composeTitle').textContent='💬 Reply';
  _openComposeBox(chan);
}

function openCompose(chan){
  _replyToCall='';
  var name=cfg.operator_name||'';
  document.getElementById('composeTitle').textContent=name?('✉️ Send Message to '+name):'✉️ Send Message';
  _openComposeBox(chan);
}

function _openComposeBox(chan){
  aprsManualUncheck=false;
  document.getElementById('replyText').value='';
  document.getElementById('replyStatus').textContent='';
  document.getElementById('aprsLengthWarn').style.display='none';
  // Set checkboxes based on what's available
  document.getElementById('chkAprs').checked=true;
  document.getElementById('chkWinlink').checked=true;
  document.getElementById('chkVarac').checked=true;
  // If opening from a specific reply, only check that channel
  if(chan==='aprs'){document.getElementById('chkWinlink').checked=false;document.getElementById('chkVarac').checked=false}
  else if(chan==='winlink'){document.getElementById('chkAprs').checked=false;document.getElementById('chkVarac').checked=false}
  else if(chan==='varac'){document.getElementById('chkAprs').checked=false;document.getElementById('chkWinlink').checked=false}
  // Hide unavailable channels
  var d=window._d||{};
  var aprsOn=d.config&&d.config.aprs&&d.config.aprs.enabled&&(d.aprs_connected||d.kiss_connected);
  var wlOn=d.config&&d.config.pat&&d.config.pat.enabled;
  var varacOn=!!d.config&&!!d.config.varac_db_path&&d.db_connected;
  document.getElementById('chkAprsLabel').style.display=aprsOn?'':'none';
  document.getElementById('chkWinlinkLabel').style.display=wlOn?'':'none';
  document.getElementById('chkVaracLabel').style.display=varacOn?'':'none';
  if(!aprsOn)document.getElementById('chkAprs').checked=false;
  if(!wlOn)document.getElementById('chkWinlink').checked=false;
  if(!varacOn)document.getElementById('chkVarac').checked=false;
  // Manual uncheck listener for APRS
  document.getElementById('chkAprs').onchange=function(){if(!this.checked)aprsManualUncheck=true;else aprsManualUncheck=false};
  document.getElementById('replyBox').classList.remove('hidden');
  // Quick replies
  var qb=document.getElementById('quickBtns');
  var qr=(cfg.quick_replies||[]);
  qb.innerHTML=qr.map(function(q){return '<button onclick="setQuickReply(\''+esc(q).replace(/'/g,"\\'")+'\')">'+esc(q)+'</button>'}).join('');
  setTimeout(function(){
    document.getElementById('replyBox').scrollIntoView({behavior:'smooth',block:'center'});
    document.getElementById('replyText').focus();
  },100);
}

function onComposeInput(){
  var t=document.getElementById('replyText').value;
  var warn=document.getElementById('aprsLengthWarn');
  var chk=document.getElementById('chkAprs');
  if(t.length>67){
    warn.style.display='block';
    if(!aprsManualUncheck)chk.checked=false;
  }else{
    warn.style.display='none';
    if(!aprsManualUncheck)chk.checked=true;
    // Re-show if APRS is available
    var d=window._d||{};
    var aprsOn=d.config&&d.config.aprs&&d.config.aprs.enabled&&(d.aprs_connected||d.kiss_connected);
    if(!aprsOn)chk.checked=false;
  }
}

async function sendMulti(){
  var msg=document.getElementById('replyText').value.trim();
  if(!msg)return;
  var sendAprs=document.getElementById('chkAprs').checked;
  var sendWl=document.getElementById('chkWinlink').checked;
  var sendVarac=document.getElementById('chkVarac').checked;
  if(!sendAprs&&!sendWl&&!sendVarac){
    document.getElementById('replyStatus').textContent='Select at least one method';
    document.getElementById('replyStatus').style.color='var(--red)';
    return;
  }
  document.getElementById('sendBtn').disabled=true;
  document.getElementById('replyStatus').textContent='Sending...';
  document.getElementById('replyStatus').style.color='var(--text3)';
  var results=[];
  if(sendAprs){
    try{
      var r=await cpost('/api/aprs_reply',{message:msg.substring(0,67),to_callsign:_replyToCall});
      var d=await r.json();
      results.push(d.ok?'APRS ✓':'APRS: '+(d.error||'failed'));
    }catch(e){results.push('APRS: error')}
  }
  if(sendWl){
    try{
      var r=await cpost('/api/winlink_reply',{message:msg,subject:'Message'});
      var d=await r.json();
      results.push(d.ok?'Winlink ✓':'Winlink: '+(d.error||'failed'));
    }catch(e){results.push('Winlink: error')}
  }
  if(sendVarac){
    try{
      var r=await cpost('/api/reply',{message:msg,subject:'Message'});
      var d=await r.json();
      results.push(d.ok?'VarAC ✓':'VarAC: '+(d.error||'failed'));
    }catch(e){results.push('VarAC: error')}
  }
  document.getElementById('sendBtn').disabled=false;
  var allOk=results.every(function(r){return r.indexOf('✓')>=0});
  if(allOk){
    document.getElementById('replyText').value='';
    document.getElementById('replyStatus').textContent='';
    toast('Message sent: '+results.join(', '));
    closeReply();
    dismissAll();
  }else{
    document.getElementById('replyStatus').textContent=results.join(' | ');
    document.getElementById('replyStatus').style.color=allOk?'var(--green)':'var(--red)';
  }
}

function closeReply(){
  document.getElementById('replyBox').classList.add('hidden');
}

function start(){
  audioCtx=new(window.AudioContext||window.webkitAudioContext)();
  soundOn=true;
  document.getElementById('splash').classList.add('hidden');
  if('Notification'in window&&Notification.permission==='default')Notification.requestPermission();
  poll();setInterval(poll,3000);
}

/* --- Sound --- */
function vol(){return cfg.alert_volume||.3}
function speak(text,v){
  if(!('speechSynthesis' in window))return;
  window.speechSynthesis.cancel();
  const u=new SpeechSynthesisUtterance(text);
  u.volume=v!==undefined?v:vol();u.rate=0.95;u.pitch=1.0;
  // Prefer a natural-sounding voice
  const voices=window.speechSynthesis.getVoices();
  const preferred=voices.find(v=>v.lang.startsWith('en')&&v.name.includes('Female'))||
                  voices.find(v=>v.lang.startsWith('en')&&!v.name.includes('Google'))||
                  voices.find(v=>v.lang.startsWith('en'));
  if(preferred)u.voice=preferred;
  window.speechSynthesis.speak(u);
}
let _lastAlertName='';
function play(type,v,fromName){
  if(type==='voice'){
    const name=fromName||_lastAlertName||cfg.operator_name||'someone';
    speak('New message from '+name,v!==undefined?v:vol());
    return;
  }
  if(!audioCtx||!soundOn)return;const V=v!==undefined?v:vol(),t=audioCtx.currentTime;
  const S={
    gentle(){[523,659,784].forEach((f,i)=>{const o=audioCtx.createOscillator(),g=audioCtx.createGain();o.connect(g);g.connect(audioCtx.destination);o.type='sine';o.frequency.setValueAtTime(f,t+i*.22);g.gain.setValueAtTime(V*.6,t+i*.22);g.gain.exponentialRampToValueAtTime(.001,t+i*.22+.5);o.start(t+i*.22);o.stop(t+i*.22+.55)})},
    two_tone(){for(let i=0;i<3;i++){const o=audioCtx.createOscillator(),g=audioCtx.createGain();o.connect(g);g.connect(audioCtx.destination);o.type='square';o.frequency.setValueAtTime(800,t+i*.3);o.frequency.setValueAtTime(1200,t+i*.3+.15);g.gain.setValueAtTime(V,t+i*.3);g.gain.exponentialRampToValueAtTime(.001,t+i*.3+.28);o.start(t+i*.3);o.stop(t+i*.3+.3)}},
    sonar(){for(let i=0;i<2;i++){const o=audioCtx.createOscillator(),g=audioCtx.createGain();o.connect(g);g.connect(audioCtx.destination);o.type='sine';o.frequency.setValueAtTime(1200,t+i*.8);g.gain.setValueAtTime(V,t+i*.8);g.gain.exponentialRampToValueAtTime(.001,t+i*.8+.6);o.start(t+i*.8);o.stop(t+i*.8+.7)}},
    siren(){const o=audioCtx.createOscillator(),g=audioCtx.createGain();o.connect(g);g.connect(audioCtx.destination);o.type='sawtooth';o.frequency.setValueAtTime(400,t);o.frequency.exponentialRampToValueAtTime(1400,t+1.5);g.gain.setValueAtTime(V,t);g.gain.exponentialRampToValueAtTime(.001,t+1.8);o.start(t);o.stop(t+2)},
    klaxon(){for(let i=0;i<4;i++){const o=audioCtx.createOscillator(),g=audioCtx.createGain();o.connect(g);g.connect(audioCtx.destination);o.type='sawtooth';o.frequency.setValueAtTime(i%2?380:440,t+i*.25);g.gain.setValueAtTime(V,t+i*.25);g.gain.exponentialRampToValueAtTime(.001,t+i*.25+.22);o.start(t+i*.25);o.stop(t+i*.25+.24)}},
    telegraph(){let o2=0;[.08,.08,.08,.24,.08].forEach(d=>{const o=audioCtx.createOscillator(),g=audioCtx.createGain();o.connect(g);g.connect(audioCtx.destination);o.type='sine';o.frequency.setValueAtTime(700,t+o2);g.gain.setValueAtTime(V,t+o2);g.gain.setValueAtTime(0,t+o2+d);o.start(t+o2);o.stop(t+o2+d+.01);o2+=d+.08})},
  };(S[type]||S.gentle)();
}
function startAlarm(fromName){
  if(fromName)_lastAlertName=fromName;
  if(alarmInt)return;
  const s=cfg.alert_sound||'gentle';
  play(s,undefined,fromName);
  alarmInt=setInterval(()=>play(s,undefined,fromName),s==='voice'?8000:5000);
}
function stopAlarm(){if(alarmInt){clearInterval(alarmInt);alarmInt=null;if('speechSynthesis' in window)window.speechSynthesis.cancel()}}
function preview(){
  const snd=document.getElementById('cSound').value;
  const v=parseInt(document.getElementById('cVol').value)/100;
  play(snd,v,cfg.operator_name||'Facundo');
}

/* --- Settings --- */
async function openSett(){await fillForm();loadPatConfig();document.getElementById('settOverlay').classList.add('open')}
function closeSett(){document.getElementById('settOverlay').classList.remove('open');document.getElementById('fb').classList.remove('open')}

/* --- Sitrep --- */
async function openSitrep(){
  var res=document.getElementById('sitrepResult');res.className='sitrep-result';res.textContent='';
  try{
    var r=await fetch('/api/sitrep/latest');var d=await r.json();
    var next=d.ok?(d.number||0)+1:1;
    document.getElementById('sitrepNum').textContent=String(next).padStart(3,'0');
    if(d.fields){
      document.getElementById('srHouse').value=d.fields.house||'All OK';
      document.getElementById('srVehicles').value=d.fields.vehicles||'All OK';
      document.getElementById('srUtilities').value=d.fields.utilities||'All OK';
      document.getElementById('srHealth').value=d.fields.health||'All OK';
      document.getElementById('srNeeds').value=d.fields.needs||'None';
      document.getElementById('srRelocation').value=d.fields.relocation||'No change';
      document.getElementById('srLocation').value=d.fields.location||'';
      document.getElementById('srRemarks').value='';
    }
  }catch(e){}
  document.getElementById('sitrepOverlay').classList.add('open');
}
function closeSitrep(){document.getElementById('sitrepOverlay').classList.remove('open')}

async function sendSitrep(){
  var body={
    house:document.getElementById('srHouse').value.trim()||'All OK',
    vehicles:document.getElementById('srVehicles').value.trim()||'All OK',
    utilities:document.getElementById('srUtilities').value.trim()||'All OK',
    health:document.getElementById('srHealth').value.trim()||'All OK',
    needs:document.getElementById('srNeeds').value.trim()||'None',
    relocation:document.getElementById('srRelocation').value.trim()||'No change',
    location:document.getElementById('srLocation').value.trim(),
    remarks:document.getElementById('srRemarks').value.trim()
  };
  var res=document.getElementById('sitrepResult');
  res.className='sitrep-result';res.textContent='Posting...';res.style.display='block';
  try{
    var r=await cpost('/api/sitrep',body);var d=await r.json();
    if(d.ok){
      res.className='sitrep-result ok';
      var msg='SITREP #'+String(d.number).padStart(3,'0')+' posted to BBS: '+d.filename;
      if(d.bulletin_sent)msg+='\nAPRS bulletin sent: '+d.bulletin_msg;
      else if(d.bulletin_msg)msg+='\nAPRS bulletin failed to send';
      if(d.varac_broadcast_sent)msg+='\nVarAC broadcast sent: '+d.varac_broadcast_msg;
      else if(d.varac_broadcast_msg)msg+='\nVarAC broadcast failed';
      res.textContent=msg;
    }else{
      res.className='sitrep-result err';res.textContent='Error: '+(d.error||'Unknown');
    }
  }catch(e){
    res.className='sitrep-result err';res.textContent='Error: '+e.message;
  }
}

async function sendQuickSitrep(){
  document.getElementById('srHouse').value='All OK';
  document.getElementById('srVehicles').value='All OK';
  document.getElementById('srUtilities').value='All OK';
  document.getElementById('srHealth').value='All OK';
  document.getElementById('srNeeds').value='None';
  document.getElementById('srRelocation').value='No change';
  document.getElementById('srLocation').value='';
  document.getElementById('srRemarks').value='';
  await sendSitrep();
}

async function fillForm(){
  try{const r=await fetch('/api/status');const d=await r.json();cfg=d.config||cfg}catch(e){}
  const c=cfg;
  document.getElementById('cName').value=c.operator_name||'';
  document.getElementById('cHomeCall').value=c.home_callsign||'';
  document.getElementById('cWatch').value=(c.watch_callsigns||[]).join(', ');
  document.getElementById('cVaracExe').value=c.varac_exe_path||'';
  document.getElementById('cVaracProfile').value=c.varac_profile||'';
  document.getElementById('cBbsDir').value=c.bbs_directory||'';
  var bbsR=c.bbs_directory_resolved||'';
  document.getElementById('bbsResolved').textContent=bbsR?'Resolved: '+bbsR:'';
  document.getElementById('cDb').value=c.varac_db_path||'';
  document.getElementById('cPoll').value=c.poll_interval_seconds||15;
  document.getElementById('cSound').value=c.alert_sound||'gentle';
  const vp=Math.round((c.alert_volume||.3)*100);
  document.getElementById('cVol').value=vp;
  document.getElementById('volL').textContent=vp+'%';
  document.getElementById('cQuick').value=(c.quick_replies||[]).join('\n');
  const po=c.pushover||{};
  document.getElementById('cPoOn').classList.toggle('on',!!po.enabled);
  document.getElementById('cPoUser').value=po.user_key||'';
  document.getElementById('cPoToken').value=po.api_token||'';
  document.getElementById('cPoPri').value=String(po.priority||1);
  document.getElementById('cPoSnd').value=po.sound||'pushover';
  const ap=c.aprs||{};
  document.getElementById('cAprsOn').classList.toggle('on',!!ap.enabled);
  document.getElementById('cAprsSsid').value=ap.home_ssid||'-5';
  document.getElementById('cAprsTravSsid').value=ap.traveler_ssids||ap.traveler_ssid||'-7';
  document.getElementById('cAprsPass').value=ap.passcode||'';
  document.getElementById('cAprsSrv').value=ap.server||'rotate.aprs2.net';
  document.getElementById('cAprsPort').value=ap.port||14580;
  document.getElementById('cAprsRfFallback').classList.toggle('on',!!ap.rf_fallback);
  document.getElementById('cAprsMailbox').classList.toggle('on',!!ap.use_mailbox);
  const bcn=c.beacon||{};
  document.getElementById('cBcnOn').classList.toggle('on',!!bcn.enabled);
  document.getElementById('cBcnLat').value=bcn.lat||'';
  document.getElementById('cBcnLon').value=bcn.lon||'';
  var symVal=(bcn.symbol_table||'/')+(bcn.symbol_code||'-')+' ';
  var symSel=document.getElementById('cBcnSymbol');
  for(var i=0;i<symSel.options.length;i++){if(symSel.options[i].value===symVal){symSel.selectedIndex=i;break}}
  document.getElementById('cBcnInterval').value=bcn.interval_minutes||30;
  document.getElementById('cBcnComment').value=bcn.comment||'HomeLink Radio';
  document.getElementById('cBcnAprsIs').classList.toggle('on',bcn.via_aprsis!==false);
  document.getElementById('cBcnRf').classList.toggle('on',bcn.via_rf!==false);
  const sm=c.soundmodem||{};
  document.getElementById('cSmOn').classList.toggle('on',!!sm.enabled);
  document.getElementById('cSmPath').value=sm.exe_path||'';
  document.getElementById('cSmHost').value=sm.kiss_host||'127.0.0.1';
  document.getElementById('cSmPort').value=sm.kiss_port||8100;
  const pt=c.pat||{};
  document.getElementById('cPatOn').classList.toggle('on',!!pt.enabled);
  document.getElementById('cPatPath').value=pt.exe_path||'';
  document.getElementById('cPatAddr').value=pt.http_addr||'localhost:8080';
  document.getElementById('cPatPoll').value=pt.poll_interval||30;
  document.getElementById('cPatHomeTac').value=pt.home_tactical||'';
  document.getElementById('cPatRfFallback').classList.toggle('on',!!pt.rf_fallback);
  document.getElementById('cPatRfGw').value=pt.rf_gateway||'';
  document.getElementById('cPatVaraAddr').value=pt.varafm_addr||'localhost:8300';
  document.getElementById('cPatVaraExe').value=pt.varafm_exe_path||'';
  document.getElementById('cPatTravTac').value=pt.traveler_tactical||'';
}

async function saveSett(){
  const qr=document.getElementById('cQuick').value.split('\n').map(s=>s.trim()).filter(Boolean);
  const cs=document.getElementById('cWatch').value.split(',').map(s=>s.trim().toUpperCase()).filter(Boolean);
  const p={
    operator_name:document.getElementById('cName').value.trim(),
    home_callsign:document.getElementById('cHomeCall').value.trim().toUpperCase(),
    watch_callsigns:cs,
    varac_exe_path:document.getElementById('cVaracExe').value.trim(),
    varac_profile:document.getElementById('cVaracProfile').value.trim(),
    bbs_directory:document.getElementById('cBbsDir').value.trim(),
    varac_db_path:document.getElementById('cDb').value.trim(),
    poll_interval_seconds:parseInt(document.getElementById('cPoll').value)||15,
    alert_sound:document.getElementById('cSound').value,
    alert_volume:parseInt(document.getElementById('cVol').value)/100,
    quick_replies:qr,
    pushover:{
      enabled:document.getElementById('cPoOn').classList.contains('on'),
      user_key:document.getElementById('cPoUser').value.trim(),
      api_token:document.getElementById('cPoToken').value.trim(),
      priority:parseInt(document.getElementById('cPoPri').value),
      sound:document.getElementById('cPoSnd').value,
    },
    aprs:{
      enabled:document.getElementById('cAprsOn').classList.contains('on'),
      home_ssid:document.getElementById('cAprsSsid').value.trim()||'-5',
      traveler_ssids:document.getElementById('cAprsTravSsid').value.trim()||'-7',
      passcode:document.getElementById('cAprsPass').value.trim(),
      server:document.getElementById('cAprsSrv').value.trim()||'rotate.aprs2.net',
      port:parseInt(document.getElementById('cAprsPort').value)||14580,
      rf_fallback:document.getElementById('cAprsRfFallback').classList.contains('on'),
      use_mailbox:document.getElementById('cAprsMailbox').classList.contains('on'),
    },
    beacon:{
      enabled:document.getElementById('cBcnOn').classList.contains('on'),
      lat:parseFloat(document.getElementById('cBcnLat').value)||0,
      lon:parseFloat(document.getElementById('cBcnLon').value)||0,
      symbol_table:document.getElementById('cBcnSymbol').value.charAt(0),
      symbol_code:document.getElementById('cBcnSymbol').value.charAt(1),
      comment:document.getElementById('cBcnComment').value.trim()||'HomeLink Radio',
      interval_minutes:parseInt(document.getElementById('cBcnInterval').value)||30,
      via_aprsis:document.getElementById('cBcnAprsIs').classList.contains('on'),
      via_rf:document.getElementById('cBcnRf').classList.contains('on'),
    },
    soundmodem:{
      enabled:document.getElementById('cSmOn').classList.contains('on'),
      exe_path:document.getElementById('cSmPath').value.trim(),
      kiss_host:document.getElementById('cSmHost').value.trim()||'127.0.0.1',
      kiss_port:parseInt(document.getElementById('cSmPort').value)||8100,
      auto_launch:true,
    },
    pat:{
      enabled:document.getElementById('cPatOn').classList.contains('on'),
      exe_path:document.getElementById('cPatPath').value.trim(),
      http_addr:document.getElementById('cPatAddr').value.trim()||'localhost:8080',
      auto_launch:true,
      poll_interval:parseInt(document.getElementById('cPatPoll').value)||30,
      home_tactical:document.getElementById('cPatHomeTac').value.trim().toUpperCase(),
      traveler_tactical:document.getElementById('cPatTravTac').value.trim().toUpperCase(),
      rf_fallback:document.getElementById('cPatRfFallback').classList.contains('on'),
      rf_gateway:document.getElementById('cPatRfGw').value.trim().toUpperCase(),
      varafm_addr:document.getElementById('cPatVaraAddr').value.trim()||'localhost:8300',
      varafm_exe_path:document.getElementById('cPatVaraExe').value.trim(),
    },
  };
  const r=await cpost('/api/config',p);
  const d=await r.json();
  if(d.ok){cfg=p;closeSett();toast('Settings saved')}
}

function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),msg.length>40?4500:2500)}

async function loadPatConfig(){
  try{
    const r=await fetch('/api/pat_config');
    const d=await r.json();
    if(d.ok){
      document.getElementById('cPatCall').value=d.mycall||'';
      document.getElementById('cPatPass').value=d.secure_login_password||'';
      document.getElementById('cPatLoc').value=d.locator||'';
      document.getElementById('cPatVaraAddr').value=d.varafm_addr||'localhost:8300';
    }
  }catch(e){}
}

async function loadGateways(){
  const el=document.getElementById('gwList');
  var grid=document.getElementById('cPatLoc').value.trim().toUpperCase();

  // If no grid set, try browser geolocation first
  if(!grid){
    el.textContent='Detecting your location...';
    try{
      var pos=await new Promise(function(resolve,reject){
        if(!navigator.geolocation){reject(new Error('No geolocation'));return}
        navigator.geolocation.getCurrentPosition(resolve,reject,{timeout:8000,enableHighAccuracy:false});
      });
      grid=latLonToGrid(pos.coords.latitude,pos.coords.longitude).toUpperCase();
      document.getElementById('cPatLoc').value=grid;
      toast('Location detected: '+grid);
    }catch(e){
      el.textContent='Enter a Grid Locator or allow location access, then try again.';
      return;
    }
  }

  // Save grid to Pat's config so rmslist -s uses it
  el.textContent='Updating location and searching...';
  try{
    await cpost('/api/pat_config',{locator:grid});
  }catch(e){}

  // Now fetch gateways
  el.textContent='Searching for nearby gateways...';
  try{
    const r=await fetch('/api/pat_gateways');
    const d=await r.json();
    if(d.ok&&d.gateways.length>0){
      el.innerHTML=d.gateways.slice(0,8).map(function(g){
        var label=g.callsign||g.info||'Unknown';
        if(g.distance)label+=' ('+Math.round(g.distance)+' km)';
        if(g.frequency)label+=' '+g.frequency;
        var call=g.callsign||'';
        return call?'<a href="#" onclick="document.getElementById(\'cPatRfGw\').value=\''+call+'\';return false" style="display:block;color:var(--accent);margin:2px 0">'+label+'</a>'
                    :'<div style="margin:2px 0">'+label+'</div>';
      }).join('');
    }else{
      el.textContent=d.error||'No VARA FM gateways found nearby.';
    }
  }catch(e){el.textContent='Error: '+e}
}

async function savePatConfig(){
  const p={
    mycall:document.getElementById('cPatCall').value.trim().toUpperCase(),
    secure_login_password:document.getElementById('cPatPass').value,
    locator:document.getElementById('cPatLoc').value.trim().toUpperCase(),
    home_tactical:document.getElementById('cPatHomeTac').value.trim().toUpperCase(),
    varafm_addr:document.getElementById('cPatVaraAddr').value.trim()||'localhost:8300',
  };
  const st=document.getElementById('patSaveStatus');
  st.textContent='Saving...';st.style.color='var(--text3)';
  try{
    const r=await cpost('/api/pat_config',p);
    const d=await r.json();
    if(d.ok){st.textContent='Saved!';st.style.color='var(--green)'}
    else{st.textContent='Error saving';st.style.color='var(--red)'}
  }catch(e){st.textContent='Error: '+e;st.style.color='var(--red)'}
  setTimeout(()=>{st.textContent=''},3000);
}

/* --- Location detection --- */
function latLonToGrid(lat,lon){
  // Convert lat/lon to 6-char Maidenhead grid locator
  var lo=lon+180;var la=lat+90;
  var g='';
  g+=String.fromCharCode(65+Math.floor(lo/20));
  g+=String.fromCharCode(65+Math.floor(la/10));
  g+=String.fromCharCode(48+Math.floor((lo%20)/2));
  g+=String.fromCharCode(48+Math.floor(la%10));
  g+=String.fromCharCode(97+Math.floor((lo%2)*12));
  g+=String.fromCharCode(97+Math.floor((la%1)*24));
  return g;
}

function detectLocation(){
  if(!navigator.geolocation){
    toast('Browser does not support geolocation');return;
  }
  var el=document.getElementById('cPatLoc');
  el.value='Detecting...';
  navigator.geolocation.getCurrentPosition(
    function(pos){
      var grid=latLonToGrid(pos.coords.latitude,pos.coords.longitude);
      el.value=grid.toUpperCase();
      toast('Location detected: '+grid.toUpperCase());
    },
    function(err){
      el.value='';
      toast('Location error: '+err.message);
    },
    {timeout:10000,enableHighAccuracy:false}
  );
}

function detectBeaconLocation(){
  if(!navigator.geolocation){
    toast('Browser does not support geolocation');return;
  }
  toast('Detecting location...');
  navigator.geolocation.getCurrentPosition(
    function(pos){
      document.getElementById('cBcnLat').value=pos.coords.latitude.toFixed(4);
      document.getElementById('cBcnLon').value=pos.coords.longitude.toFixed(4);
      // Also update grid locator if empty
      var gridEl=document.getElementById('cPatLoc');
      if(!gridEl.value){
        gridEl.value=latLonToGrid(pos.coords.latitude,pos.coords.longitude).toUpperCase();
      }
      toast('Location set: '+pos.coords.latitude.toFixed(4)+', '+pos.coords.longitude.toFixed(4));
    },
    function(err){
      toast('Location error: '+err.message);
    },
    {timeout:10000,enableHighAccuracy:true}
  );
}

async function browse(path){
  const fb=document.getElementById('fb');
  const r=await cpost('/api/browse_path',{path});
  const d=await r.json();
  if(!d.ok)return;
  let h='';
  if(d.current)h+=`<div class="fc">📂 ${esc(d.current)}</div>`;
  for(const e of d.entries){
    const p=e.path.replace(/\\/g,'\\\\').replace(/'/g,"\\'");
    h+=e.is_dir?`<div class="fe dir" onclick="browse('${p}')">📁 ${esc(e.name)}</div>`
               :`<div class="fe file" onclick="pickDb('${p}')">📄 ${esc(e.name)}</div>`;
  }
  fb.innerHTML=h;fb.classList.add('open');
}
function pickDb(p){document.getElementById('cDb').value=p;document.getElementById('fb').classList.remove('open');testDb()}

async function testDb(){
  const el=document.getElementById('dbTr'),p=document.getElementById('cDb').value.trim();
  if(!p){el.className='tr err';el.textContent='Enter a path';return}
  const r=await cpost('/api/test_db',{path:p});
  const d=await r.json();
  el.className=d.ok?'tr ok':'tr err';
  el.textContent=d.ok?`✓ Connected! ${d.vmail_count} messages found.`:d.error;
}

async function testPo(){
  const el=document.getElementById('poTr');
  const uk=document.getElementById('cPoUser').value.trim(),at=document.getElementById('cPoToken').value.trim();
  if(!uk||!at){el.className='tr err';el.textContent='Enter both keys';return}
  const r=await cpost('/api/test_pushover',{user_key:uk,api_token:at});
  const d=await r.json();
  el.className=d.ok?'tr ok':'tr err';
  el.textContent=d.ok?'✓ Notification sent! Check your phone.':d.error;
}

async function genPasscode(){
  const call=document.getElementById('cHomeCall').value.trim();
  if(!call){alert('Enter a Home Callsign first');return}
  const r=await cpost('/api/aprs_passcode',{callsign:call});
  const d=await r.json();
  if(d.ok)document.getElementById('cAprsPass').value=d.passcode;
}

/* --- Reply --- */
function setQuickReply(txt){document.getElementById('replyText').value=txt;onComposeInput()}

/* --- Poll --- */
let seenIds=new Set();
async function poll(){
  try{
    const r=await fetch('/api/status');const d=await r.json();cfg=d.config;if(d.csrf_token)csrfToken=d.csrf_token;ui(d);
  }catch(e){
    document.getElementById('connDot').className='conn-dot err';
    document.getElementById('connText').textContent='Connection lost';
  }
}

function ui(d){
  const dot=document.getElementById('connDot'),ct=document.getElementById('connText');
  if(!d.config.varac_db_path){dot.className='conn-dot off';ct.textContent='Not configured — open Settings'}
  else if(d.db_connected){dot.className='conn-dot';ct.textContent='Connected & watching'}
  else{dot.className='conn-dot err';ct.textContent='Connection error'}

  const eb=document.getElementById('errBar');
  if(d.error&&d.config.varac_db_path){eb.textContent=d.error;eb.style.display='block'}else{eb.style.display='none'}

  // APRS status
  const ad=document.getElementById('aprsDot'),at2=document.getElementById('aprsText');
  const aprsOn=d.config.aprs&&d.config.aprs.enabled;
  if(!aprsOn){ad.className='conn-dot off';at2.textContent='APRS off'}
  else if(!d.config.home_callsign){ad.className='conn-dot off';at2.textContent='APRS: set callsign in Settings'}
  else if(d.aprs_connected){ad.className='conn-dot';at2.textContent='APRS connected'}
  else{ad.className='conn-dot err';at2.textContent='APRS connecting...'}
  document.getElementById('aprsConn').style.display=aprsOn?'':'none';

  // KISS/Soundmodem RF status
  const kd=document.getElementById('kissDot'),kt=document.getElementById('kissText');
  const kissOn=d.config.soundmodem&&d.config.soundmodem.enabled;
  if(!kissOn){kd.className='conn-dot off';kt.textContent='RF off'}
  else if(d.kiss_connected){kd.className='conn-dot';kt.textContent='RF connected'}
  else{kd.className='conn-dot err';kt.textContent='RF connecting...'}
  document.getElementById('kissConn').style.display=kissOn?'':'none';

  // Pat/Winlink status
  const pd2=document.getElementById('patDot'),pt2=document.getElementById('patText');
  const patOn=d.config.pat&&d.config.pat.enabled;
  if(!patOn){pd2.className='conn-dot off';pt2.textContent='Winlink off'}
  else if(d.pat_connected){pd2.className='conn-dot';pt2.textContent='Winlink connected'}
  else{pd2.className='conn-dot off';pt2.textContent='Winlink: waiting for Pat'}
  document.getElementById('patConn').style.display=patOn?'':'none';

  // New message buttons visibility
  const aprsAvail=aprsOn&&d.aprs_connected;
  const kissAvail=kissOn&&d.kiss_connected;
  const varacAvail=!!d.config.varac_db_path&&d.db_connected;
  const wlAvail=patOn;
  const anyAvail=aprsAvail||kissAvail||varacAvail||wlAvail;
  const btn=document.getElementById('btnSendMsg');
  const opName=d.config.operator_name||'';
  btn.textContent=opName?('✉️ Send Message to '+opName):'✉️ Send Message';
  btn.style.display=anyAvail?'':'none';
  // Show Sitrep button if BBS is available
  const sitBtn=document.getElementById('btnSitrep');
  if(sitBtn)sitBtn.style.display=(d.config.bbs_directory_resolved)?'':'none';

  // Position card
  const lc=document.getElementById('locCard');
  if(d.aprs_last_position){
    const p=d.aprs_last_position;
    lc.style.display='block';
    document.getElementById('locTitle').textContent=`${d.config.operator_name||p.callsign} — Last Position`;
    try{document.getElementById('locTime').textContent=new Date(p.time).toLocaleString()}catch(x){}
    let det=`${p.lat.toFixed(4)}, ${p.lon.toFixed(4)}`;
    if(p.altitude)det+=` · ${Math.round(p.altitude)}m alt`;
    if(p.speed)det+=` · ${Math.round(p.speed)} km/h`;
    if(p.comment)det+=` · ${p.comment}`;
    document.getElementById('locDetails').textContent=det;
    document.getElementById('locLink').href=`https://www.google.com/maps?q=${p.lat},${p.lon}`;
  }else{lc.style.display='none'}

  // Status card
  const sc=document.getElementById('statusCard'),si=document.getElementById('statusIcon'),
        st=document.getElementById('statusTitle'),ss=document.getElementById('statusSub');
  const active=d.pending.filter(a=>!dismissedIds.has(a.id));
  const name=d.config.operator_name||'Someone';

  if(active.length>0){
    sc.className='status-card alert';si.textContent='💌';
    st.textContent=`New message from ${name}!`;
    ss.textContent=active.length===1?'Scroll down to read and reply.':'You have '+active.length+' new messages below.';
  }else if(d.last_checkin_time){
    sc.className='status-card ok';si.textContent='✅';
    st.textContent=`${name} checked in`;
    ss.textContent=`Last heard: ${d.last_checkin_friendly}`;
  }else{
    sc.className='status-card waiting';si.textContent='📻';
    st.textContent='Waiting for check-in...';
    ss.textContent='The monitor is watching for messages.';
  }

  // Header sub
  document.getElementById('headerSub').textContent=
    d.last_checkin_time?`Last heard: ${d.last_checkin_friendly}`:'Waiting for messages...';

  // New alerts
  for(const a of d.pending){
    if(!seenIds.has(a.id)&&!dismissedIds.has(a.id)){
      seenIds.add(a.id);
      if('Notification'in window&&Notification.permission==='granted'){
        const n=a.from_name||'Someone';
        new Notification(`Message from ${n}`,{body:a.message||a.subject||'New check-in',requireInteraction:true});
      }
    }
  }

  // Show toast for new system confirmations (delivery receipts from bots)
  for(const a of d.history){
    if(a.type==='system'&&!seenIds.has(a.id)){
      seenIds.add(a.id);
      toast('✓ '+esc(a.message||'Message delivered'));
    }
  }

  window._d=d;render();
}

function dismiss(id){
  dismissedIds.add(id);
  const d=window._d;
  if(d){
    const remaining=d.pending.filter(a=>!dismissedIds.has(a.id));
    if(remaining.length===0){
      stopAlarm();
      cpost('/api/stop_alarm');
    }
  }
  render();
}
function dismissAll(){
  const d=window._d;if(!d)return;
  d.pending.forEach(a=>dismissedIds.add(a.id));
  stopAlarm();
  cpost('/api/stop_alarm');
  render();
}

function render(){
  const d=window._d;if(!d)return;
  // "alerting" = pending and not dismissed (alarm should sound)
  const alerting=d.pending.filter(a=>!dismissedIds.has(a.id));
  if(alerting.length>0){const newest=alerting[alerting.length-1];startAlarm(newest.from_name||'')}else{stopAlarm()}

  // "active" = all pending messages — shown newest first
  const active=[...d.pending].reverse();
  const aa=document.getElementById('activeArea');
  if(!active.length){aa.innerHTML='<div class="empty"><div class="icon">📭</div><p>No new messages</p></div>'}
  else{aa.innerHTML=active.map(a=>card(a,true)).join('')}

  const ha=document.getElementById('histArea');
  // Only show in history if NOT still in pending (active) area
  const pendingIds=new Set(d.pending.map(a=>a.id));
  const h=[...d.history].filter(a=>!pendingIds.has(a.id)).reverse();
  if(!h.length){ha.innerHTML='<div class="empty"><div class="icon">📋</div><p>No messages yet</p></div>'}
  else{ha.innerHTML=h.slice(0,50).map(a=>card(a,false)).join('')}
}

function closeMessage(id){
  // Dismiss alarm + acknowledge server-side (removes from active, keeps in history)
  dismissedIds.add(id);
  cpost('/api/acknowledge',{id:id});
  const d=window._d;
  if(d){
    // Remove from cached pending immediately so render() hides it
    d.pending=d.pending.filter(a=>a.id!==id);
    const remaining=d.pending.filter(a=>!dismissedIds.has(a.id));
    if(remaining.length===0){
      stopAlarm();
      cpost('/api/stop_alarm');
    }
  }
  render();
}

function card(a,isActive){
  const dismissed=dismissedIds.has(a.id);
  const name=a.from_name||a.from_call||'Unknown';

  // System messages (bot confirmations) — render as a subtle info card
  if(a.type==='system'){
    return `<div class="msg-card" style="border-left-color:var(--text3);background:var(--surface);opacity:0.85">
      <div class="msg-header"><div class="msg-from"><span class="msg-badge" style="background:#e2e8f0;color:#64748b">✓ Delivery Confirmation</span></div>
      <div class="msg-time">${a.friendly_time||''}</div></div>
      <div class="msg-body" style="color:var(--text3);font-size:13px">${esc(a.message)}</div></div>`;
  }

  let badges='';
  if(isActive&&!dismissed)badges+='<span class="msg-badge badge-new">New</span>';
  if(a.urgent)badges+='<span class="msg-badge badge-urgent">Urgent</span>';
  if(a.type==='relay')badges+='<span class="msg-badge badge-relay">Relay Alert</span>';
  if(a.type==='aprs')badges+='<span class="msg-badge" style="background:#dbeafe;color:#2563eb">APRS</span>';
  if(a.type==='winlink')badges+='<span class="msg-badge" style="background:#fef3c7;color:#b45309">Winlink</span>';

  const subj=a.subject?`<div class="msg-subject">${esc(a.subject)}</div>`:'';
  const body=(a.type==='vmail'||a.type==='aprs'||a.type==='winlink')&&a.message
    ?`<div class="msg-body">${esc(a.message)}</div>`
    :a.type==='relay'?'<div class="msg-body">Station <b>'+(a.relay_station||'unknown')+'</b> is holding a message for you'+(a.frequency_mhz?' on '+a.frequency_mhz+' MHz':'')+'.<br>If VarAC is running, it may retrieve the message automatically. Otherwise, you may need to connect to that station manually from VarAC to collect it.</div>':'';

  const replyChan=a.type==='aprs'?'aprs':a.type==='winlink'?'winlink':'varac';
  const replyTo=a.from_call||'';
  const cls=isActive&&!dismissed?(a.urgent?'msg-card unread urgent':'msg-card unread'):'msg-card';

  let actions='';
  if(isActive){
    const dismissBtn=!dismissed?`<button class="btn-small" onclick="dismiss('${a.id}')" style="color:var(--text2)">🔕 Dismiss Alert</button>`:'';
    const closeBtn=`<button class="btn-small" onclick="closeMessage('${a.id}')" style="color:var(--text3)">✕ Close</button>`;
    actions=`<div class="msg-actions">
        ${dismissBtn}
        <button class="btn-small primary" onclick="openReply('${replyChan}','${esc(replyTo)}')">Reply</button>
        ${closeBtn}
      </div>`;
  }

  return `<div class="${cls}">
    <div class="msg-header"><div class="msg-from">${badges}${esc(name)}</div>
    <div class="msg-time">${a.friendly_time||''}</div></div>
    ${subj}${body}${actions}</div>`;
}

function esc(s){const d=document.createElement('div');d.textContent=s||'';return d.innerHTML}

/* --- Message Log --- */
function toggleLog(){
  const area=document.getElementById('logArea');
  const btn=document.getElementById('logToggleBtn');
  if(area.classList.contains('hidden')){
    area.classList.remove('hidden');
    btn.textContent='Hide Log';
    loadLog();
  }else{
    area.classList.add('hidden');
    btn.textContent='View Log';
  }
}
async function loadLog(){
  const el=document.getElementById('logEntries');
  el.innerHTML='<div class="empty"><p>Loading...</p></div>';
  try{
    const r=await fetch('/api/message_log');const d=await r.json();
    if(!d.ok||!d.entries.length){el.innerHTML='<div class="empty"><div class="icon">📋</div><p>No saved messages yet</p></div>';return}
    el.innerHTML=d.entries.map(e=>{
      const isOut=e.direction==='outgoing';
      const dir=isOut?'Sent':'Received';
      const dirIcon=isOut?'➡️':'📨';
      const who=isOut?'To: '+esc(e.to_callsign):'From: '+esc(e.from_name||e.from_callsign);
      const subj=e.subject?'<div class="msg-subject">'+esc(e.subject)+'</div>':'';
      const body=e.message?'<div class="msg-body" style="max-height:60px;overflow:hidden">'+esc(e.message)+'</div>':'';
      const borderColor=isOut?'var(--orange)':'var(--accent)';
      const badgeBg=isOut?'var(--orange-light)':'var(--accent-light)';
      const badgeColor=isOut?'var(--orange)':'var(--accent)';
      let ts='';
      try{ts=new Date(e.timestamp).toLocaleString()}catch(x){ts=e.timestamp}
      return '<div class="msg-card" style="border-left-color:'+borderColor+'">'
        +'<div class="msg-header"><div class="msg-from"><span class="msg-badge" style="background:'+badgeBg+';color:'+badgeColor+'">'+dirIcon+' '+dir+'</span> '+who+'</div>'
        +'<div class="msg-time">'+ts+'</div></div>'+subj+body+'</div>';
    }).join('');
  }catch(e){el.innerHTML='<div class="empty"><p>Failed to load log</p></div>'}
}
</script>
</body>
</html>"""

if __name__ == "__main__":
    _init_log_file()
    port = config.get("web_port", 5000)
    log.info("HomeLink Radio starting on port %d", port)
    log.info("Open http://127.0.0.1:%d", port)

    # Check if port is already in use (previous instance still running?)
    import socket as _sock
    try:
        _test = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        _test.settimeout(1)
        _test.connect(("127.0.0.1", port))
        _test.close()
        log.error("Port %d is already in use! Is another instance of HomeLink Radio running?", port)
        log.error("Stop the other instance first, or change web_port in config.json")
        sys.exit(1)
    except (ConnectionRefusedError, OSError, _sock.timeout):
        pass  # Port is free, good to go

    # Start the DB poll loop (lightweight, non-blocking)
    threading.Thread(target=poll_loop, daemon=True).start()

    # Launch all external programs in a background thread so Flask can
    # start serving immediately — the browser won't get "connection refused"
    def _deferred_launches():
        """Launch VarAC, APRS, Soundmodem, Pat, beacon AFTER Flask is listening."""
        time.sleep(1)  # Brief pause to let Flask bind the port
        # Launch VarAC if configured
        if config.get("varac_exe_path", ""):
            try:
                _launch_varac()
            except Exception as e:
                log.error("VarAC launch failed: %s", e)
        if config.get("aprs", {}).get("enabled", False):
            start_aprs()
        if config.get("soundmodem", {}).get("enabled", False):
            try:
                _launch_soundmodem()
            except Exception as e:
                log.error("Soundmodem launch failed: %s", e)
            start_kiss()
        if config.get("pat", {}).get("enabled", False):
            try:
                pcfg = _pat_read_config()
                dirty = False
                # Clear Pat's internal schedule — HomeLink controls sync timing
                if pcfg.get("schedule"):
                    log.info("Clearing Pat's internal schedule — HomeLink handles sync timing")
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
        # Start beacon if enabled
        if config.get("beacon", {}).get("enabled", False):
            start_beacon()
        log.info("All external services launched")

    threading.Thread(target=_deferred_launches, daemon=True).start()

    # Suppress Flask/Werkzeug development server banner but keep ERROR level visible
    import logging as _logging
    _logging.getLogger("werkzeug").setLevel(_logging.ERROR)
    import flask.cli
    flask.cli.show_server_banner = lambda *_: None
    log.info("Web server ready on port %d", port)
    try:
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    except OSError as e:
        if "Address already in use" in str(e) or "10048" in str(e):
            log.error("Port %d is already in use! Stop the other instance or change web_port in config.json", port)
        else:
            log.error("Failed to start web server: %s", e)
        sys.exit(1)
