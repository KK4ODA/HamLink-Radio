# HomeLink Radio

A friendly web app that lets your family know when you've checked in via ham radio. Your wife (or any family member) sees a simple, jargon-free dashboard on her phone or the home PC — no ham radio knowledge needed.

## What It Does

- **Shows a clear "checked in" status** when you send a VMail from the field
- **Alerts with sound and phone notifications** so she knows immediately
- **Displays your messages** with your name instead of callsigns — no SNR, no bands, no jargon
- **Lets her reply** by typing a message or tapping a pre-written quick reply — the reply is queued as a VMail in VarAC's outbox
- **Monitors relay notifications** so she knows if a parked message is waiting
- **Works on her phone** — fully mobile-responsive, accessible from any browser on the local network

## Quick Start

1. `pip install flask`
2. `python monitor.py` (or double-click `start_monitor.bat` on Windows)
3. Open `http://127.0.0.1:5000` on the PC, or `http://<pc-ip>:5000` on her phone
4. Click **Start**, then open **Settings** (gear icon) to configure:
   - **Their Name** — your name, shown in alerts instead of callsigns
   - **Home Station Callsign** — used as the "from" address on her replies
   - **Watch Callsign(s)** — your traveling callsign(s) to monitor
   - **Database path** — browse to VarAC.db
   - **Quick Replies** — pre-written messages she can send with one tap
   - **Alert sound** — 6 options with volume control and preview
   - **Pushover** — phone push notifications

## How Replies Work

When your wife taps a quick reply or types a message and hits **Send Reply**, the app inserts a new VMail into VarAC's outbox. VarAC will send it on the next connection with your remote station.

**Important:** The home VarAC station must be running and connected to VARA modem for replies to be transmitted. The app queues the reply — VarAC handles the actual radio transmission.

## Setup for Your Wife

After initial configuration:
1. Bookmark `http://<pc-ip>:5000` on her phone
2. She clicks **Start** and allows notifications
3. When you send a VMail from the field, she sees your name and message with a chime
4. She taps **Got it** to acknowledge, or **Reply** to send a message back
5. Pushover sends a notification to her phone even if the browser tab is closed

## Files

- `monitor.py` — the app (Flask backend + embedded frontend)
- `config.json` — auto-saved settings (edited via the GUI)
- `start_monitor.bat` — Windows launcher with dependency checking

## Requirements

- Python 3.8+ with Flask
- VarAC V5+ running on the same machine
- A web browser (any modern browser on PC or phone)
