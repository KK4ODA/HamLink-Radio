# HamLink Radio — Quick Reference

Two people, three ways to reach each other. Fill in your own callsigns and addresses (examples shown), print it, and keep it near the radio and on the fridge.

| | Home station | Traveler |
|---|---|---|
| Station callsign | `HOMECALL` (e.g. `KK4ODA`) | `HOMECALL/P` for VarAC (e.g. `KK4ODA/P`), `HOMECALL` for Winlink |
| APRS address | `HOMECALL-5` (e.g. `KK4ODA-5`) | `HOMECALL-9` mobile / `HOMECALL-7` handheld (e.g. `KK4ODA-9`, `KK4ODA-7`) |
| Winlink address | home tactical (e.g. `BRECKEN`) | traveler tactical (e.g. `FACUNDO`) |
| Winlink RF gateway (no internet) | callsign-SSID via VARA FM (e.g. `WD5EMA-10`) | any nearby gateway |
| Home location | lat, lon (e.g. `33.8404, -84.2743`) | shown on the dashboard when you beacon |
| VarAC frequency / schedule | see VarAC frequency schedule | same |

Pick the first channel that works, top to bottom:

1. **APRS** — quickest, works anywhere with an iGate or cell data (aprs.fi app). Short messages only.
2. **Winlink** — email-like, works with cell data or through a Winlink RF gateway.
3. **VarAC VMail** — HF radio to radio, no internet needed on either end. Slowest, most reliable when everything else is down.

---

## For the traveler (on the road)

**Check in by APRS** (phone app or APRS radio)
- Send a message **to the home APRS address** (e.g. `KK4ODA-5`). Keep it under **67 characters**.
- Send **from one of your traveler SSIDs** (e.g. `KK4ODA-9` mobile or `KK4ODA-7` handheld); home listens for all of them.
- Beacon your position now and then; home sees it on the dashboard and map.
- You get an ACK back when HamLink receives it. If not, try again or use another channel.
- No signal? Send anyway. Some iGates and the APRS MAIL bot will hold it.

**Check in by Winlink** (Pat, Winlink Express, or the Winlink web/phone app)
- Send **to the home tactical address** (e.g. `BRECKEN`), not the bare callsign.
- Send **from your traveler tactical address** (e.g. `FACUNDO`). Winlink refuses mail from a callsign to itself.
- Optional: post a Winlink position report. Home sees it.
- Home checks Winlink every few minutes; over the RF gateway, every few hours.

**Check in by VarAC**
- Connect to the **home callsign** (e.g. `KK4ODA`) and send a VMail. Sign as `HOMECALL/P` (e.g. `KK4ODA/P`).
- Can't reach home directly? Send the VMail to any relay station. Home is alerted that a relay is holding it and can fetch it.
- Read the **BBS** on the home station: the newest `$$SITREP_nnn` file is the family status report.
- Hear a broadcast like `SITREP#005 on BBS QSY 13:30Z 14.105 pls connect & relay`? That's home. Connect and read it, or ask another ham to relay.

**Getting replies**
- APRS: replies go to whichever traveler SSID last sent a message. Retrieve missed ones from the MAIL bot by sending `APRSM` to `MAIL`.
- Winlink: sync your inbox; look for mail from the home tactical address.
- VarAC: connect to home again; queued replies are delivered on connect.

**Message tips**
- Lead with the essentials: *where you are, that you're OK, next check-in time*.
- Example: `Mile 212 OK. Next check 0800. Fuel low but fine.`

---

## For the person at home

**When a message arrives**
- The dashboard turns red, the PC beeps, your phone gets a Pushover alert (if enabled).
- **🔕 Silence alarm** (or the bell in the header, or Esc) stops the sound. **Close** files the message. **Reply** answers it.
- Green banner "checked in" = all quiet. The position card shows where the traveler was last heard; the Offline Map tab shows it on a map.

**Replying** (tap Reply, or Send Message)
- Tick the channels to use. Ticking all of them is fine; the message goes out on each.
- **APRS** goes to the traveler's last-used SSID over the internet. Keep it under 67 characters.
- **Winlink** goes to the traveler tactical address over the internet.
- **VarAC** is queued in the outbox and delivered when the traveler next connects. Always safe to use.
- Quick-reply buttons send a pre-written message in one tap.

**If the internet is down**
- APRS can go out by radio through Soundmodem, and Winlink through the VARA FM gateway. You will see an amber "RF" label and a warning.
- Only proceed if a licensed operator is present, or it is a genuine emergency involving safety of life or property.
- VarAC keeps working without internet.

**Post a family status report (Sitrep)**
- Tap **Post Sitrep to BBS**. Change only what's different; **Quick All-OK** sends an all-clear in one tap.
- HamLink saves it to the VarAC BBS and announces it on the air so the traveler, or any ham nearby, can pick it up.

**From your phone**
- The Pushover alert has a **Dismiss** link, and optional quick-reply links that confirm before sending.
- Your phone must be on the home Wi-Fi for the links to work.

**Relay alerts**
- "Station XXXX is holding a message" means the traveler couldn't reach you directly. Tap **Retrieve now** (or approve auto-retrieval) and VarAC fetches it.

**Keep it running**
- Leave the HamLink window and browser tab open. Tap Start Monitoring after a reboot so alert sounds work.
- Settings live in `config.json` next to `HamLink_Radio.exe` (the path is shown at the bottom of the page and in Settings).
- Stop HamLink only from the button at the bottom of the page.

---

## HamLink settings cheat sheet

Every setting, with a typical value as an example. Everything lives in `config.json` next to `HamLink_Radio.exe` (the path is shown at the bottom of the dashboard). Write your own values in the blank column and keep this sheet with the printout.

**People & callsigns**

| Setting | Example | Yours | Why |
|---|---|---|---|
| Their name | `Facundo` | | Shown in alerts and on the Send Message button |
| Home station callsign | `KK4ODA` | | Base call only. VarAC replies come from it; APRS and the beacon add the SSID below |
| Watch for callsign(s) | `KK4ODA, KK4ODA/P` | | VarAC and Winlink senders that trigger alerts (no APRS SSIDs here) |

**VarAC**

| Setting | Example | Yours |
|---|---|---|
| Database path | `C:\VarAC\VarAC.db` | |
| Executable path | `C:\VarAC\VarAC.exe` (auto-launched) | |
| Profile (.ini) | `VarAC.ini` | |
| BBS directory override | blank (read from VarAC.ini) | |
| Check every | 15 s | |

**Alert sound**: e.g. Gentle chime, volume 30 %, alarm stops by itself after 15 min. Quick replies: four one-tap messages.

**Phone notifications (Pushover)**: enable, paste your user key and app token, priority High, quick-reply links on.

**APRS messaging (APRS-IS)**

| Setting | Example | Yours | Why |
|---|---|---|---|
| Enable APRS-IS | on | | Receives and sends APRS over the internet; RF via Soundmodem is the fallback |
| Home station SSID | `-5` | | → home APRS address `KK4ODA-5` |
| Traveler SSID(s) | `-9, -7` | | → traveler addresses `KK4ODA-9` (mobile) and `KK4ODA-7` (handheld) |
| Server / port | `rotate.aprs2.net` / `14580` | | |
| Passcode | Auto-generate | | Derived from the home callsign |
| RF fallback | on | | Send via Soundmodem when internet is down (licensed-operator confirmation) |
| APRS mailbox copy | on | | Also sends replies to the MAIL bot for later pickup |
| aprs.fi API key | from aprs.fi → My account | | Backfills the last position on startup |

**APRS RF monitor (Soundmodem)**

| Setting | Example | Yours |
|---|---|---|
| Enable | on | |
| Soundmodem path | `C:\Soundmodem\soundmodem.exe` | |
| KISS TCP host / port | `127.0.0.1` / `8100` (must match Soundmodem's KISS server port) | |

**APRS position beacon**: on, home lat/lon (e.g. `33.8404, -84.2743`), symbol House, every 30 min, via APRS-IS and via RF.

**VMail relay automation**: on, auto-retrieve on, confirm before connecting as you prefer, route replies via relay on, cooldown 300 s, delay 10 s, max retries 2.

**Winlink (via Pat)**

| Setting | Example | Yours | Why |
|---|---|---|---|
| Enable | on | | |
| Pat executable | `C:\Pat\pat.exe` (auto-launched) | | |
| Pat HTTP address | `localhost:8080` | | |
| Check Winlink every | 30 s | | |
| Position reports | on | | Reads the traveler's Winlink position reports |
| Winlink callsign | `KK4ODA` | | Pat account login (password in Settings) |
| Home tactical address | `BRECKEN` | | Home sends FROM this; the traveler sends TO it |
| Traveler tactical address | `FACUNDO` | | Home sends TO this; alerts for mail FROM it |
| RF fallback | on | | Use the VARA FM gateway when internet is down |
| VARA FM gateway | `WD5EMA-10` | | Callsign **with** SSID, dash not = |
| RF gateway poll interval | 10800 s (3 h) | | |
| VARA FM executable | `C:\VARA FM\VARAFM.exe` | | |
| VARA FM modem address | `localhost:8300` | | |

**Offline map**: maps live in `tiles/` — a home-area map plus trip maps downloaded from the Map tab. All are shown together.

**Updates**: automatic check every 6 h; no GitHub token needed.

**Web port**: 5000 → `http://127.0.0.1:5000` on the PC, `http://<PC-IP>:5000` from a phone on the home Wi-Fi.
