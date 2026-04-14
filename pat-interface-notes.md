# Pat Winlink — Technical Interface Notes

## Pat HTTP API

When Pat is running in HTTP server mode (`pat http --addr localhost:8080`), it exposes a full REST API at `http://localhost:8080/api/`:

### Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/connect` | POST | Trigger a mailbox sync. Form param: `url` (e.g., `telnet`, `varafm:///CALLSIGN`) |
| `/api/disconnect` | POST | Disconnect active session |
| `/api/mailbox/{box}` | GET | List messages in a mailbox (`in`, `out`, `sent`) |
| `/api/mailbox/{box}` | POST | Compose/send a message |
| `/api/mailbox/{box}/{mid}` | GET | Get individual message by ID |
| `/api/mailbox/{box}/{mid}` | DELETE | Delete a message |
| `/api/mailbox/{box}/{mid}/read` | POST | Mark message as read |
| `/api/mailbox/{box}/{mid}/{attachment}` | GET | Download attachment |
| `/api/posreport` | POST | Post a position report |
| `/api/status` | GET | Get Pat status |
| `/api/current_gps_position` | GET | Get current GPS position |
| `/api/coords_to_locator` | POST | Convert coordinates to grid locator |
| `/api/qsy` | POST | Change frequency |
| `/api/rmslist` | GET | List RMS gateways |
| `/api/config` | GET, PUT | Read/write Pat config |
| `/api/config/connect_aliases` | GET | List connect aliases |
| `/api/config/connect_aliases/{alias}` | GET, PUT, DELETE | Manage individual aliases |
| `/api/reload` | POST | Reload config |
| `/api/bandwidths` | GET | List available bandwidths |
| `/api/formcatalog` | GET | List Winlink form templates |
| `/api/form` | GET, POST | Get/submit forms |
| `/api/forms` | GET | List forms |
| `/api/formsUpdate` | POST | Update form catalog |
| `/api/template` | GET | Get form template |
| `/api/new-release-check` | GET | Check for Pat updates |
| `/api/winlink-account/password-recovery-email` | GET, PUT | Account recovery |
| `/api/winlink-account/registration` | GET, POST | Account registration |

### WebSocket

| Endpoint | Purpose |
|----------|---------|
| `/ws` | WebSocket for real-time status updates |

---

## Critical: Don't Spawn Separate Pat Processes

**Problem:** Running `pat connect telnet` as a subprocess while `pat http` is already running causes conflicts. Pat uses file locks on the mailbox directory, so two Pat instances can't run simultaneously on Windows. The sync silently fails.

**Solution:** Use the HTTP API instead. To trigger a sync:

```python
import urllib.request, urllib.parse, json

api_url = "http://localhost:8080/api/connect"
data = urllib.parse.urlencode({"url": "telnet"}).encode()
req = urllib.request.Request(api_url, data=data, method="POST")
resp = urllib.request.urlopen(req, timeout=120)
result = json.loads(resp.read().decode())
# result = {"NumReceived": 3}  — number of new messages downloaded
```

For VARA FM RF connection:
```python
data = urllib.parse.urlencode({"url": "varafm:///WD5EMA-10"}).encode()
```

### Connect Handler Details

The `/api/connect` POST endpoint:
- Accepts `url` as a **form value** (not JSON body) — use `urllib.parse.urlencode`, not `json.dumps`
- **Blocks** until the session completes (can take 2-5 seconds for telnet, up to 5 minutes for RF)
- Returns `{"NumReceived": N}` with count of newly downloaded messages
- Returns HTTP 500 if the session fails
- Connection URL examples:
  - `telnet` — Standard Winlink CMS telnet connection
  - `varafm:///WD5EMA-10` — VARA FM connection via specific gateway

---

## Pat CLI Commands — Safe vs Conflicting

### Safe to run alongside `pat http`:
- `pat compose --from TACTICAL --subject "Subject" RECIPIENT` — Writes to outbox locally, no network operation. Reads message body from stdin.
- `pat rmslist -s -m varafm` — Queries gateway list. Read-only network call.
- `pat position` — Posts a position report.

### CONFLICT with `pat http` (do NOT use as subprocess):
- `pat connect telnet` — Opens network session, conflicts with running HTTP server
- `pat connect varafm:///...` — Same conflict
- Any command that opens a mailbox session

---

## Pat Config File

### Location
- **Windows:** `%LOCALAPPDATA%\pat\config.json` (e.g., `C:\Users\USERNAME\AppData\Local\pat\config.json`)
- **Linux/Mac:** `~/.config/pat/config.json`

### Key Fields

```json
{
  "mycall": "KK4ODA",
  "secure_login_password": "your-winlink-password",
  "auxiliary_addresses": ["BRECKEN", "OTHERTACTICAL"],
  "locator": "EM73",
  "http_addr": "localhost:8080",
  "connect_aliases": {
    "telnet": "telnet://KK4ODA:CMSTelnet@cms.winlink.org:8772/wl2k"
  },
  "schedule": {}
}
```

- `mycall` — Station callsign
- `secure_login_password` — Winlink account password
- `auxiliary_addresses` — Array of tactical addresses this station can receive mail for
- `locator` — Maidenhead grid square
- `http_addr` — Address for the HTTP server (default `localhost:8080`)
- `connect_aliases` — Named connection strings; the `telnet` alias must include credentials in the format `telnet://CALLSIGN:CMSTelnet@cms.winlink.org:8772/wl2k`
- `schedule` — Pat's internal sync scheduler. Remove or empty this key if your application controls sync timing to avoid duplicate syncs.

---

## Pat Mailbox API Response Format

### List Messages: `GET /api/mailbox/in`

Returns a JSON array. **Field names can vary in casing** — always check for both:

```json
[
  {
    "MID": "unique-message-id",
    "Subject": "Hello from the field",
    "From": {"Addr": "FACUNDO"},
    "To": [{"Addr": "BRECKEN"}],
    "Date": "2026-04-13T20:00:00Z",
    "Body": ""
  }
]
```

**Important notes:**
- `From` can be a dict `{"Addr": "..."}` or a plain string — handle both
- `To` can be an array of dicts, array of strings, or a single string — handle all cases
- `Body` is **often empty** in the list endpoint — you must fetch the individual message to get the body
- Field casing varies across Pat versions: check for `"MID"/"mid"/"Id"/"id"`, `"Subject"/"subject"`, `"From"/"from"`, `"To"/"to"`, `"Body"/"body"`, `"Date"/"date"`

### Get Individual Message: `GET /api/mailbox/in/{MID}`

Returns a single message object with the `Body` field populated.

### Compose a Message via CLI (safe alongside `pat http`)

```python
import subprocess

exe = "C:\\Pat\\pat.exe"
cmd = [exe, "compose", "--from", "BRECKEN", "--subject", "Reply", "FACUNDO@winlink.org"]
proc = subprocess.Popen(
    cmd, stdin=subprocess.PIPE,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    cwd=os.path.dirname(exe)
)
stdout, stderr = proc.communicate(input="Message body here".encode(), timeout=15)
```

- `--from` — Tactical address to send from (must be in `auxiliary_addresses`)
- Last argument is the recipient (append `@winlink.org` if no domain)
- Message body is piped via stdin
- This only queues the message locally — you must trigger a sync via `/api/connect` to actually send it

---

## Winlink Position Reports

### Posting a Position Report
- Via Pat CLI: `pat position`
- Via HTTP API: `POST /api/posreport`

### Querying Position Reports from Winlink CMS

**RSS Feed:**
```
https://cms.winlink.org:444/rss/rsspositionreports.aspx?callsign=KK4ODA
```

**KML (Google Earth):**
```
https://cms.winlink.org:444/kml/KmlPositionReports.aspx?callsign=KK4ODA
```

**Important notes:**
- Uses **base callsign**, NOT tactical address
- Coordinates are in the `<title>` tag, NOT in standard georss tags
- Title format: `"Position report for KK4ODA is 33.83900 / -84.27533"`
- Parse with regex: `r'(-?[\d.]+)\s*/\s*(-?[\d.]+)'` on the title text
- `<description>` field may contain operational context (e.g., "FACUNDO,DEKALB,80M,E") or be empty
- CMS retains reports for approximately 12 months
- `<pubDate>` is in standard RFC 822 format

### Python Example — Fetching Latest Position

```python
import urllib.request, xml.etree.ElementTree as ET, re

callsign = "KK4ODA"
url = f"https://cms.winlink.org:444/rss/rsspositionreports.aspx?callsign={callsign}"
req = urllib.request.Request(url, headers={"User-Agent": "MyApp"})
resp = urllib.request.urlopen(req, timeout=15)
root = ET.fromstring(resp.read().decode())

item = root.find(".//item")
if item is not None:
    title = item.findtext("title", "")
    pub_date = item.findtext("pubDate", "")
    m = re.search(r'(-?[\d.]+)\s*/\s*(-?[\d.]+)', title)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        print(f"Position: {lat}, {lon} at {pub_date}")
```

---

## Launching Pat

### Start HTTP Server
```python
cmd = [exe, "http", "--addr", "localhost:8080"]
proc = subprocess.Popen(cmd, cwd=os.path.dirname(exe),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
```

### Check if Pat is Already Running
```python
import socket
try:
    s = socket.create_connection(("localhost", 8080), timeout=2)
    s.close()
    # Pat is already running
except (ConnectionRefusedError, OSError, socket.timeout):
    # Safe to launch
    pass
```

### Stopping Pat
```python
if proc and proc.poll() is None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except:
        proc.kill()
```
