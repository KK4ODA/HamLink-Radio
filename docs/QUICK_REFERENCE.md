# HamLink Radio — Quick Reference

Two people, three ways to reach each other. Keep this near the radio and on the fridge.

| | Home station (Brecken) | Traveler (Facundo) |
|---|---|---|
| Station callsign | `KK4ODA` | `KK4ODA/P` (VarAC), `KK4ODA` (Winlink) |
| APRS address | `KK4ODA-5` | `KK4ODA-9` (add `-7` in Settings → APRS if the handheld is used too) |
| Winlink address | `BRECKEN` | `FACUNDO` |
| Winlink RF gateway (no internet) | `WD5EMA-10` via VARA FM | any nearby gateway |
| Home location | 33.8404, -84.2743 (Atlanta area) | shown on the dashboard when you beacon |
| VarAC frequency / schedule | see VarAC frequency schedule | same |

Pick the first channel that works, top to bottom:

1. **APRS** — quickest, works anywhere with an iGate or cell data (aprs.fi app). Short messages only.
2. **Winlink** — email-like, works with cell data or through a Winlink RF gateway.
3. **VarAC VMail** — HF radio to radio, no internet needed on either end. Slowest, most reliable when everything else is down.

---

## For Facundo (on the road)

**Check in by APRS** (phone app or APRS radio)
- Send a message **to `KK4ODA-5`**. Keep it under **67 characters**.
- Send **from `KK4ODA-9`** (the mobile). `KK4ODA-7` only works if it is added to *Traveler APRS SSIDs* in Settings.
- Beacon your position now and then; home sees it on the dashboard and map.
- You get an ACK back when HamLink receives it. If not, try again or use another channel.
- No signal? Send anyway. Some iGates and the APRS MAIL bot will hold it.

**Check in by Winlink** (Pat, Winlink Express, or the Winlink web/phone app)
- Send **to `BRECKEN`** (the home tactical address, not `KK4ODA`).
- Send **from `FACUNDO`**, your tactical address. Winlink refuses mail from a callsign to itself.
- Optional: post a Winlink position report. Home sees it.
- Home checks Winlink every few minutes; over the RF gateway, every few hours.

**Check in by VarAC**
- Connect to **`KK4ODA`** and send a VMail. Sign as `KK4ODA/P`.
- Can't reach home directly? Send the VMail to any relay station. Home is alerted that a relay is holding it and can fetch it.
- Read the **BBS** on the home station: the newest `$$SITREP_nnn` file is the family status report.
- Hear a broadcast like `SITREP#005 on BBS QSY 13:30Z 14.105 pls connect & relay`? That's home. Connect and read it, or ask another ham to relay.

**Getting replies**
- APRS: replies arrive as messages to `KK4ODA-9`. Retrieve missed ones from the MAIL bot by sending `APRSM` to `MAIL`.
- Winlink: sync your inbox; look for mail from `BRECKEN`.
- VarAC: connect to `KK4ODA` again; queued replies are delivered on connect.

**Message tips**
- Lead with the essentials: *where you are, that you're OK, next check-in time*.
- Example: `Mile 212 OK. Next check 0800. Fuel low but fine.`

---

## For Brecken (at home)

**When a message arrives**
- The dashboard turns red, the PC beeps, your phone gets a Pushover alert (if enabled).
- **Dismiss alert** silences the sound. **Close** files the message. **Reply** answers it.
- Green banner "checked in" = all quiet. The position card shows where Facundo was last heard; the Offline Map tab shows it on a map.

**Replying** (tap Reply, or Send Message)
- Tick the channels to use. Ticking all of them is fine; the message goes out on each.
- **APRS** goes to `KK4ODA-9` over the internet. Keep it under 67 characters.
- **Winlink** goes to `FACUNDO` over the internet.
- **VarAC** is queued in the outbox and delivered when Facundo next connects. Always safe to use.
- Quick-reply buttons send a pre-written message in one tap.

**If the internet is down**
- APRS can go out by radio through Soundmodem, and Winlink through the `WD5EMA-10` gateway with VARA FM. You will see an amber "RF" label and a warning.
- Only proceed if a licensed operator is present, or it is a genuine emergency involving safety of life or property.
- VarAC keeps working without internet.

**Post a family status report (Sitrep)**
- Tap **Post Sitrep to BBS**. Change only what's different; **Quick All-OK** sends an all-clear in one tap.
- HamLink saves it to the VarAC BBS and announces it on the air so Facundo, or any ham nearby, can pick it up.

**From your phone**
- The Pushover alert has a **Dismiss** link, and optional quick-reply links that confirm before sending.
- Your phone must be on the home Wi-Fi for the links to work.

**Relay alerts**
- "Station XXXX is holding a message" means Facundo couldn't reach you directly. Tap **Retrieve now** (or approve auto-retrieval) and VarAC fetches it.

**Keep it running**
- Leave the HamLink window and browser tab open. Tap Start Monitoring after a reboot so alert sounds work.
- Settings live in `config.json` next to `HamLink_Radio.exe` (the path is shown at the bottom of the page and in Settings).
- Stop HamLink only from the button at the bottom of the page.

---

## HamLink settings cheat sheet (current values)

Everything below lives in `config.json` next to `HamLink_Radio.exe` (the path is shown at the bottom of the dashboard). Secrets are not listed here — they are in the file and in Settings.

**People & callsigns**

| Setting | Value | Why |
|---|---|---|
| Their name | `Facundo` | Shown in alerts and on the Send Message button |
| Home station callsign | `KK4ODA` | Base call only. VarAC replies come from it; APRS and the beacon add the SSID below |
| Watch for callsign(s) | `KK4ODA, KK4ODA/P` | VarAC and Winlink senders that trigger alerts (no APRS SSIDs here) |

**VarAC**

| Setting | Value |
|---|---|
| Database path | `C:\VarAC\VarAC.db` |
| Executable path | `C:\VarAC\VarAC.exe` (auto-launched) |
| Profile (.ini) | `VarAC.ini` |
| BBS directory override | blank (read from VarAC.ini) |
| Check every | 15 s |

**Alert sound**: Gentle chime, volume 30 %, alarm stops by itself after 15 min. Quick replies: the four defaults.

**Phone notifications (Pushover)**: enabled, priority High, sound *pushover*, quick-reply links on. Keys are in Settings.

**APRS messaging (APRS-IS)**

| Setting | Value | Why |
|---|---|---|
| Enable APRS-IS | **off** | Turn on to receive/send APRS over the internet. RF APRS via Soundmodem works regardless |
| Home station SSID | `-5` | → home APRS address `KK4ODA-5` |
| Traveler SSID(s) | `-9` | → traveler address `KK4ODA-9` (add `, -7` for the handheld) |
| Server / port | `rotate.aprs2.net` / `14580` | |
| Passcode | set (Auto-generate recreates it) | |
| RF fallback | on | Send via Soundmodem when internet is down (licensed-operator confirmation) |
| APRS mailbox copy | on | Also sends replies to the MAIL bot for later pickup |
| aprs.fi API key | set | Backfills the last position on startup |

**APRS RF monitor (Soundmodem)**

| Setting | Value |
|---|---|
| Enable | on |
| Soundmodem path | `C:\Users\Facundo\Desktop\soundmodem114 -packet D710\soundmodem.exe` |
| KISS TCP host / port | `127.0.0.1` / `8101` (must match Soundmodem's KISS server port) |

**APRS position beacon**: on, `33.8404, -84.2743`, symbol House, every 30 min, comment *HamLink Radio*, via APRS-IS and via RF.

**VMail relay automation**: on, auto-retrieve on, confirm before connecting **off**, route replies via relay on, cooldown 300 s, delay 10 s, max retries 2, no ignored stations.

**Winlink (via Pat)**

| Setting | Value | Why |
|---|---|---|
| Enable | on | |
| Pat executable | `C:\Pat\pat.exe` (auto-launched) | |
| Pat HTTP address | `localhost:8080` | |
| Check Winlink every | 30 s | |
| Position reports | on | Reads the traveler's Winlink position reports |
| Winlink callsign | `KK4ODA` | Pat account login (password in Settings) |
| Home tactical address | `BRECKEN` | Home sends FROM this; Facundo sends TO it |
| Traveler tactical address | `FACUNDO` | Home sends TO this; alerts for mail FROM it |
| RF fallback | on | Use the VARA FM gateway when internet is down |
| VARA FM gateway | `WD5EMA-10` | Callsign **with** SSID, dash not = |
| RF gateway poll interval | 10800 s (3 h) | |
| VARA FM executable | `C:\VARA FM_9700\VARAFM.exe` | |
| VARA FM modem address | `localhost:8200` | |

**Offline map**: maps live in `tiles/` — `home-area.mbtiles` (around home) plus trip maps downloaded from the Map tab. All are shown together.

**Updates**: automatic check every 6 h, no GitHub token needed (the repository is public).

**Web port**: 5000 → `http://127.0.0.1:5000` on the PC, `http://<PC-IP>:5000` from a phone on the home Wi-Fi.
