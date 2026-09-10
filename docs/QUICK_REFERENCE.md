# HamLink Radio — Quick Reference

Two people, three ways to reach each other. Fill in the blanks once and keep this near the radio and on the fridge.

| | Home station | Traveler |
|---|---|---|
| Callsign | `HOMECALL` (VarAC / Winlink) | `HOMECALL/P` (VarAC), `HOMECALL-7` (APRS) |
| APRS address | `HOMECALL-5` | `HOMECALL-7` |
| Winlink address | `HOMEBASE` (tactical) | `TRAVELER` (tactical) |
| VarAC frequency / schedule | see VarAC frequency schedule | same |

Pick the first channel that works, top to bottom:

1. **APRS** — quickest, works anywhere with an iGate or cell data (aprs.fi app). Short messages only.
2. **Winlink** — email-like, works with cell data or through a Winlink RF gateway.
3. **VarAC VMail** — HF radio to radio, no internet needed on either end. Slowest, most reliable when everything else is down.

---

## For the traveler (on the road)

**Check in by APRS** (phone app or APRS radio)
- Send a message **to `HOMECALL-5`**. Keep it under **67 characters**.
- Beacon your position now and then; home sees it on the dashboard and map.
- You get an ACK back when HamLink receives it. If not, try again or use another channel.
- No signal? Send anyway. Some iGates and the APRS MAIL bot will hold it.

**Check in by Winlink** (Pat, Winlink Express, or the Winlink web/phone app)
- Send **to `HOMEBASE`** (the home tactical address, not `HOMECALL`).
- Send **from `TRAVELER`**, your tactical address. Winlink refuses mail from a callsign to itself.
- Optional: post a Winlink position report. Home sees it.
- Home checks Winlink every few minutes; over RF gateway, every few hours.

**Check in by VarAC**
- Connect to **`HOMECALL`** and send a VMail. Sign as `HOMECALL/P`.
- Can't reach home directly? Send the VMail to any relay station. Home is alerted that a relay is holding it.
- Read the **BBS** on the home station: the newest `$$SITREP_nnn` file is the family status report.
- Hear a broadcast like `SITREP#005 on BBS QSY 13:30Z 14.105 pls connect & relay`? That's home. Connect and read it, or ask another ham to relay.

**Getting replies**
- APRS: replies arrive as messages to `HOMECALL-7`. Retrieve missed ones from the MAIL bot with `APRSM` sent to `MAIL`.
- Winlink: sync your inbox; look for mail from `HOMEBASE`.
- VarAC: connect to `HOMECALL` again; queued replies are delivered on connect.

**Message tips**
- Lead with the essentials: *where you are, that you're OK, next check-in time*.
- Example: `Mile 212 OK. Next check 0800. Fuel low but fine.`

---

## For the person at home

**When a message arrives**
- The dashboard turns red, the PC beeps, your phone gets a Pushover alert.
- **Dismiss alert** silences the sound. **Close** files the message. **Reply** answers it.
- Green banner "checked in" = all quiet. Check the position card to see where they were last heard.

**Replying** (tap Reply, or Send Message)
- Tick the channels to use. Ticking all of them is fine; the message goes out on each.
- **APRS** goes out over the internet. Keep it under 67 characters.
- **Winlink** goes out over the internet to `TRAVELER`.
- **VarAC** is queued in the outbox and delivered when the traveler next connects. Always safe to use.
- Quick-reply buttons send a pre-written message in one tap.

**If the internet is down**
- APRS and Winlink can fall back to radio (Soundmodem, VARA FM). You will see an amber "RF" label and a warning.
- Only proceed if a licensed operator is present, or it is a genuine emergency involving safety of life or property.
- VarAC keeps working without internet.

**Post a family status report (Sitrep)**
- Tap **Post Sitrep to BBS**. Change only what's different; **Quick All-OK** sends an all-clear in one tap.
- HamLink saves it to the VarAC BBS and announces it on the air so the traveler, or any ham nearby, can pick it up.

**From your phone**
- The Pushover alert has a **Dismiss** link, and optional quick-reply links that confirm before sending.
- Phone must be on the same Wi-Fi as the HamLink PC for the links to work.

**Relay alerts**
- "Station K1XYZ is holding a message" means the traveler couldn't reach you directly. Tap **Retrieve now** (or approve auto-retrieval) and VarAC fetches it.

**Keep it running**
- Leave the HamLink window and browser tab open. Tap Start Monitoring after a reboot so alert sounds work.
- Stop HamLink only from the button at the bottom of the page.
