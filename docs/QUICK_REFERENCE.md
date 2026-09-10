# HamLink Radio — Quick Reference

Two people, three ways to reach each other. Keep this near the radio and on the fridge.

| | Home station (Brecken) | Traveler (Facundo) |
|---|---|---|
| Station callsign | `KK4ODA` | `KK4ODA/P` (VarAC), `KK4ODA` (Winlink) |
| APRS address | `KK4ODA-5` | `KK4ODA-9` (primary), `KK4ODA-7` (backup) |
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
- Send **from `KK4ODA-9`** (or `KK4ODA-7`); home listens for both.
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
