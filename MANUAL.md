# HomeLink Radio — User Manual

**Version 1.0 | April 2026**

HomeLink Radio is a family emergency communications application for licensed amateur radio operators. It monitors multiple radio channels (VarAC, APRS, and Winlink) for check-in messages from a traveling family member and enables the home station operator to send replies, post situation reports, and receive phone notifications — all from a simple web interface.

---

## Table of Contents

1. [Who Is This For?](#1-who-is-this-for)
2. [System Requirements](#2-system-requirements)
3. [Installation](#3-installation)
4. [First Launch](#4-first-launch)
5. [Configuration Guide](#5-configuration-guide)
6. [Using HomeLink Radio](#6-using-homelink-radio)
7. [Sitrep System](#7-sitrep-system)
8. [Use Cases](#8-use-cases)
9. [FCC Compliance](#9-fcc-compliance)
10. [Troubleshooting](#10-troubleshooting)
11. [Building a Standalone EXE](#11-building-a-standalone-exe)

---

## 1. Who Is This For?

HomeLink Radio is designed for two people:

- **The traveler** — A licensed ham radio operator who is away from home (road trip, backcountry, deployment, disaster response). They send check-in messages from their portable or mobile radio using VarAC, APRS, or Winlink.

- **The home station operator** — A family member at home (spouse, parent, etc.) who may or may not hold a ham license. They use HomeLink Radio's web interface on a PC or phone to monitor for messages, send replies, and post family status reports.

The licensed operator (the traveler) is ultimately responsible for all transmissions made from the home station, including those initiated through this application. See [FCC Compliance](#9-fcc-compliance) for details.

---

## 2. System Requirements

### Required

| Component | Details |
|-----------|---------|
| **Python** | Version 3.8 or higher ([python.org](https://www.python.org/downloads/)) |
| **VarAC** | Version 5 or later ([varac.net](https://varac.net)) |
| **VARA HF Modem** | Required by VarAC for digital communications |
| **Windows PC** | Windows 10 or later (the home station computer) |

### Optional (enables additional channels)

| Component | Purpose |
|-----------|---------|
| **Pat Winlink Client** | Enables Winlink email messaging ([getpat.io](https://getpat.io)) |
| **Soundmodem** | Enables RF APRS via packet radio |
| **VARA FM Modem** | Enables Winlink RF gateway fallback |
| **Pushover Account** | Phone notifications ($5 one-time, [pushover.net](https://pushover.net)) |

### Python Libraries (auto-installed)

- `flask` — Web server framework
- `aprslib` — APRS-IS protocol support (optional, for APRS features)

---

## 3. Installation

### Step 1: Download HomeLink Radio

Place these files in a folder (e.g., `C:\HomeLink\`):
- `monitor.py` — The application
- `start_homelink.bat` — Launcher script
- `build_exe.bat` — Optional build script

### Step 2: Run the Launcher

Double-click `start_homelink.bat`. It will:

1. Check that Python 3.8+ is installed
2. Verify pip is available
3. Install Flask if not present
4. Install aprslib if not present
5. Check for Pat (optional)
6. Launch HomeLink Radio

The launcher displays version information and opens your browser to `http://127.0.0.1:5000`.

### Step 3: Install Python (if needed)

If the launcher reports Python is missing:

1. Download Python from [python.org](https://www.python.org/downloads/)
2. **Important:** Check "Add Python to PATH" during installation
3. Restart the launcher

### Step 4: Network Access (optional)

To access HomeLink Radio from a phone or tablet on the same Wi-Fi network, use the PC's local IP address instead of `127.0.0.1`. For example: `http://192.168.1.100:5000`.

---

## 4. First Launch

### Regulatory Compliance Notice

On first launch, you will see a compliance notice with three acknowledgements:

1. **Amateur radio license** — Confirms you understand a valid FCC amateur radio license is required
2. **Third-party communication rules** — Confirms you understand FCC rules on third-party traffic
3. **Emergency use and disclaimers** — Confirms you understand the app's limitations

You must check all three boxes and click "I Understand & Accept" to proceed. This screen appears once per browser.

### Welcome Screen

After the compliance notice, a splash screen explains what HomeLink Radio does:
- Monitors for check-in messages
- Sends alerts when messages arrive
- Enables replies and sitrep posting

Click "Start" to begin monitoring.

### Initial Configuration

Click the gear icon (top-right) to open Settings. At minimum, configure:

1. **Operator Name** — The traveler's name (e.g., "Facundo")
2. **Home Station Callsign** — Your FCC callsign (e.g., "KK4ODA")
3. **Watch For Callsigns** — The traveler's callsign(s) (e.g., "KK4ODA/P, KK4ODA")
4. **VarAC Database Path** — Path to VarAC's database file (e.g., `C:\VarAC\VarAC.db`)

Click "Save Settings" when done.

---

## 5. Configuration Guide

Open Settings by clicking the gear icon in the top-right corner. Each section is described below.

### 5.1 Operator Info

| Field | Description |
|-------|-------------|
| **Their Name** | The traveler's first name. Shown in alerts and notifications instead of callsigns. |
| **Home Station Callsign** | Your FCC-assigned callsign. Used as the "from" address for all replies. |
| **Watch For Callsigns** | Comma-separated list of callsigns to monitor (e.g., `KK4ODA/P, KK4ODA, KK4PCR`). Leave empty to monitor all incoming messages. |

### 5.2 VarAC

| Field | Description |
|-------|-------------|
| **VarAC Executable Path** | Full path to `VarAC.exe`. If set, HomeLink auto-launches VarAC on startup. |
| **VarAC Profile** | The `.ini` profile filename (e.g., `VarAC_7300.ini`). Leave blank to use the default `VarAC.ini`. |
| **BBS Directory Override** | Override the BBS folder path for sitreps. Leave blank to auto-read from the VarAC profile `.ini` file. A green "Resolved" hint shows the detected path. |
| **VarAC Database Path** | Path to `VarAC.db`. Click "Browse" to find it, then "Test" to verify the connection. |
| **Check Every (seconds)** | How often HomeLink polls the VarAC database for new messages. Default: 15 seconds. |

### 5.3 Alert Sound

| Field | Description |
|-------|-------------|
| **Sound** | Choose from: Gentle Chime, Two-Tone Beep, Sonar Ping, Siren, Klaxon, Telegraph, Phone Ring, Voice Alert. Click the preview button to hear each one. |
| **Volume** | Slider from 0% to 100%. Default: 30%. |

### 5.4 Quick Replies

A text area where you enter one pre-written reply per line (up to 4). These appear as one-tap buttons when composing a reply, and as clickable links in Pushover phone notifications. Examples:

```
Got your message, all is well here!
Please check in again soon.
Call home when you can.
We miss you, stay safe!
```

### 5.5 Phone Notifications (Pushover)

Pushover sends push notifications to your phone when a message arrives. Requires a Pushover account ($5 one-time purchase at [pushover.net](https://pushover.net)).

| Field | Description |
|-------|-------------|
| **Enable** | Toggle on/off. |
| **User Key** | Your Pushover user key (from the Pushover dashboard). |
| **API Token** | Your Pushover application token (create an app at Pushover). |
| **Priority** | `-2` (silent), `-1` (quiet), `0` (normal), `1` (high), `2` (emergency with repeat). |
| **Sound** | Pushover notification sound name. |

Click "Test Pushover" to send a test notification to your phone.

**Quick replies via Pushover:** When a notification arrives on your phone, it includes clickable links for each of your quick replies. Tapping one sends the reply immediately via the selected channel — no need to open the web interface.

### 5.6 APRS Messaging

APRS (Automatic Packet Reporting System) enables short messages via internet (APRS-IS) and RF.

| Field | Description |
|-------|-------------|
| **Enable APRS** | Toggle on/off. |
| **Home Station SSID** | SSID appended to your callsign for APRS (e.g., `-1` makes `KK4ODA-1`). Default: `-5`. |
| **Traveler SSID(s)** | Comma-separated SSIDs to monitor (e.g., `-7, -9`). |
| **APRS-IS Passcode** | Authentication code for APRS-IS. Click "Calculate" to auto-generate from your callsign. |
| **APRS-IS Server** | Server address. Default: `rotate.aprs2.net`. For North America: `noam.aprs2.net`. |
| **APRS-IS Port** | Server port. Default: `14580`. |
| **RF Fallback** | If enabled and internet is unavailable, APRS messages are sent via RF through Soundmodem. |
| **Mailbox (Store & Forward)** | If enabled, copies of outgoing messages are sent to the APRS mailbox system for later retrieval. |

### 5.7 Position Beacon

Periodically transmits your home station's position via APRS so the traveler can see it on the map.

| Field | Description |
|-------|-------------|
| **Enable** | Toggle on/off. |
| **Latitude / Longitude** | Your home station coordinates in decimal degrees. |
| **Symbol** | APRS map symbol (e.g., House, Antenna, Emergency). |
| **Interval** | How often to beacon, in minutes. Minimum: 5. Default: 30. |
| **Comment** | Text shown on the map alongside your beacon (e.g., "HomeLink Radio"). |
| **Via APRS-IS** | Send beacon over the internet. |
| **Via RF** | Send beacon over RF (requires Soundmodem). |

### 5.8 Winlink (via Pat)

Winlink provides email-like messaging over radio. HomeLink uses the Pat client to send and receive Winlink messages.

| Field | Description |
|-------|-------------|
| **Enable** | Toggle on/off. |
| **Pat Executable Path** | Full path to `pat.exe` (e.g., `C:\Pat\pat.exe`). |
| **Pat HTTP Address** | Pat's web interface address. Default: `localhost:8080`. |
| **Check Interval** | How often to sync with Winlink CMS, in seconds. Default: 30. |
| **Home Tactical Address** | Optional tactical callsign for the home station (e.g., `BRECKEN`). |
| **Traveler Tactical Address** | Optional tactical callsign for the traveler (e.g., `FACUNDO`). |
| **RF Fallback** | If enabled and internet is down, uses VARA FM to connect to an RF gateway. |
| **RF Gateway** | Gateway station callsign (e.g., `WD5EMA-10`). |
| **VARA FM Executable Path** | Path to `VARAFM.exe`. |
| **VARA FM Modem Address** | VARA FM modem address. Default: `localhost:8300`. |

### 5.9 APRS RF Monitor (Soundmodem)

Enables RF APRS via Soundmodem and the KISS protocol. Required for APRS RF fallback and RF beacons.

| Field | Description |
|-------|-------------|
| **Enable** | Toggle on/off. |
| **Soundmodem Path** | Full path to `soundmodem.exe`. Auto-launches on startup if set. |
| **KISS TCP Host** | Soundmodem KISS server host. Default: `127.0.0.1`. |
| **KISS TCP Port** | Soundmodem KISS server port. Default: `8100`. Must match Soundmodem's setting. |

---

## 6. Using HomeLink Radio

### 6.1 The Main Dashboard

The main screen shows:

- **Status Card** — Displays the current monitoring state:
  - Green: Last check-in received (shows time and sender name)
  - Gray: Waiting for messages
  - Red: New unread alert (pulsing border)
- **Connection Indicators** — Shows which channels are active (VarAC, APRS-IS, KISS/RF, Winlink)
- **Position Card** — If the traveler has been heard via APRS, shows their last known location with a "View on map" link
- **Send Message Button** — Opens the compose box
- **Post Sitrep to BBS Button** — Opens the sitrep form
- **New Messages** — Pending alerts with "Got it" and "Reply" buttons
- **Previous Messages** — Acknowledged message history
- **Saved Message Log** — Toggleable CSV log of all messages

### 6.2 Receiving Alerts

When the traveler sends a message:

1. HomeLink detects it during the next poll cycle (within seconds for APRS, within the configured interval for VarAC and Winlink)
2. The status card turns red and pulses
3. A sound plays through your PC speakers
4. A Pushover notification is sent to your phone (if configured)
5. The message appears in the "New Messages" section

Each alert shows:
- Sender name and callsign
- Message text
- Time received
- Channel (VarAC, APRS, Winlink, or Relay)

Tap "Got it" to acknowledge and move to history. Tap "Reply" to respond.

### 6.3 Sending Messages

Click "Send Message" or "Reply" on any alert. The compose box opens with:

- **Quick Reply Buttons** — Your pre-written replies (one tap to select)
- **Text Area** — Type a custom message
- **Channel Checkboxes** — Select which channels to send via:
  - **APRS** — Short messages up to 67 characters (fastest delivery)
  - **Winlink** — Full email messages (reliable, store-and-forward)
  - **VarAC** — Peer-to-peer digital messages (requires direct connection)

You can select multiple channels to maximize delivery chances. Click "Send" to transmit.

**APRS length warning:** If your message exceeds 67 characters, a red warning appears. APRS messages are truncated at 67 characters.

### 6.4 Quick Replies from Your Phone

If Pushover is configured, incoming message notifications on your phone include clickable quick reply links. Tapping one sends the reply immediately — useful when you're away from the computer.

### 6.5 Message Log

Click "View Log" at the bottom of the main screen to see a complete history of all incoming and outgoing messages in chronological order. The log is saved to `message_log.csv` and survives application restarts.

---

## 7. Sitrep System

### What Is a Sitrep?

A Sitrep (Situation Report) is a structured status update posted to the VarAC BBS (Bulletin Board System). It gives the traveler a complete picture of the home situation — especially useful during emergencies, natural disasters, or extended absences.

### How It Works

1. The home operator taps "Post Sitrep to BBS"
2. A form appears with pre-filled fields (defaults: "All OK" / "No change" / "None")
3. The operator edits only what has changed
4. Taps "Post Sitrep" (or "Quick All-OK" for a one-tap all-clear)
5. HomeLink:
   - Saves the sitrep as a text file in the VarAC BBS folder
   - Renames the previous sitrep to archive it
   - Sends an APRS bulletin announcing the sitrep

### Sitrep Fields

| Field | Default | Description |
|-------|---------|-------------|
| **House** | All OK | Structural status — damage, flooding, habitability |
| **Vehicles** | All OK | Car availability, fuel level, damage |
| **Utilities** | All OK | Power, water, gas, internet status |
| **Health** | All OK | Health status of all family members |
| **Immediate Needs** | None | Urgent needs — fuel, medicine, supplies, assistance |
| **Relocation** | No change | Plans to move — staying put, evacuating, relocated |
| **Location** | *(blank)* | New location if relocated — GPS coordinates, address, or What3Words |
| **Remarks** | *(blank)* | Any additional information |

### File Format

Sitreps are saved as plain text files in the VarAC BBS directory:

```
Filename: $$SITREP_003_2026-04-03_1845Z.txt

========================================
SITREP #003 — KK4ODA Home Station
2026-04-03 18:45 UTC
========================================

HOUSE:      All OK
VEHICLES:   All OK
UTILITIES:  Power out since 1400Z
HEALTH:     All OK
NEEDS:      Need generator fuel by tomorrow
RELOCATION: No change
LOCATION:   33.8392, -84.2744
REMARKS:    Tree down on Elm St, roads passable

--- End SITREP #003 ---
```

### BBS File Management

- The `$$` prefix sorts the latest sitrep to the top of the BBS file listing in VarAC
- When a new sitrep is posted, the previous one's `$$` prefix is removed (archived but still accessible)
- Sequential numbering (001, 002, 003...) makes it obvious if one was missed during relay
- UTC timestamps ensure consistency across time zones

### APRS Bulletin Announcement

When a sitrep is posted, HomeLink automatically sends an APRS bulletin to nearby stations:

```
SITREP#003 KK4ODA BBS 7.105MHz QSY 13:30Z 14.105 pls relay
```

This bulletin:
- Is transmitted via both APRS-IS (internet) and RF (Soundmodem)
- Includes the current VarAC frequency so hams know where to connect
- Includes the next scheduled frequency change (QSY) time and frequency
- Reaches all APRS-capable stations in the area

### Crowdsourced Relay

The sitrep system enables a powerful relay workflow:

1. Home station posts sitrep to BBS and sends APRS bulletin
2. Nearby hams see the bulletin on their APRS client or radio
3. They connect to the home station's VarAC BBS and download the sitrep
4. If they can reach the traveler on any band, they relay the information
5. The traveler gets the full family status update even without direct contact

---

## 8. Use Cases

### 8.1 Daily Check-Ins During Travel

**Scenario:** Your spouse is on a cross-country road trip with a mobile HF rig.

**Setup:**
- VarAC monitoring enabled, watching for their portable callsign (e.g., `KK4ODA/P`)
- APRS enabled to track their position
- Pushover enabled for phone alerts

**Workflow:**
1. Traveler sends a VarAC VMail each evening: "Made it to Amarillo, all good, 73"
2. HomeLink detects the message, plays an alert, and sends a phone notification
3. Family member taps a quick reply: "Got your message, all is well here!"
4. Reply queued to VarAC outbox for next contact

### 8.2 Hurricane / Natural Disaster

**Scenario:** A hurricane is approaching. The traveler is deployed for disaster response. The family at home needs to communicate status.

**Setup:**
- All channels enabled (VarAC, APRS, Winlink)
- RF fallback enabled (internet may go down)
- Soundmodem + APRS RF configured
- Pushover priority set to Emergency (2)

**Workflow:**
1. Before the storm: Post sitrep "All OK, preparing to shelter in place"
2. During: Post sitrep with utility status: "Power out since 1400Z, on generator"
3. If evacuating: Post sitrep with new location (GPS or What3Words) and relocation status
4. APRS bulletins automatically alert the ham community after each sitrep
5. If internet goes down, APRS messages continue via RF through local digipeaters
6. Winlink messages route via VARA FM RF gateway to reach the traveler's inbox

### 8.3 Off-Grid Backcountry Trip

**Scenario:** The traveler is hiking in a remote area with a portable HF radio, no cell coverage.

**Setup:**
- VarAC and APRS enabled
- APRS mailbox (store-and-forward) enabled
- Winlink enabled for longer messages

**Workflow:**
1. Traveler sends brief APRS messages when they have signal: "Day 3, at summit, all good"
2. Home station receives via APRS-IS and alerts the family
3. Family replies via APRS: "Miss you! Dog learned a new trick"
4. If APRS message doesn't reach directly, the mailbox stores it for later retrieval
5. Traveler can also check in via Winlink from a campsite with longer messages

### 8.4 Emergency Family Relay via BBS

**Scenario:** The traveler cannot be reached directly from the home station. Other hams in the area might be able to relay.

**Workflow:**
1. Family member posts a sitrep describing the situation at home
2. APRS bulletin goes out: "SITREP#005 KK4ODA BBS 7.105MHz pls relay"
3. A nearby ham (say, K5OOM) sees the bulletin
4. K5OOM connects to the home station's VarAC BBS and downloads the sitrep
5. K5OOM is on a different band where they can reach the traveler
6. K5OOM relays the family's status to the traveler
7. The traveler now knows their family is safe and what their situation is

---

## 9. FCC Compliance

HomeLink Radio is designed for use by licensed amateur radio operators under FCC Part 97. The following rules are particularly relevant.

### 9.1 Control Operator Responsibility (97.7, 97.103)

Every amateur station must have a control operator — the person responsible for the proper operation of the station. The control operator must hold a valid amateur radio license of the appropriate class.

**What this means for HomeLink Radio:** The licensed operator (typically the traveler) is responsible for all transmissions made from the home station, including those initiated through this application by a family member. If the family member does not hold their own amateur license, they are acting as a third party (see 97.115 below) and the licensed operator must ensure compliance.

### 9.2 Third-Party Traffic (97.115)

An amateur station may transmit messages on behalf of a third party (a person who is not a licensed amateur) to any station within the jurisdiction of the United States. The control operator must ensure compliance with all rules.

**What this means for HomeLink Radio:** A non-licensed family member may use HomeLink Radio to send replies and sitreps, but:
- The licensed operator must have authorized this use
- The licensed operator is responsible for the content of all transmissions
- All messages must comply with FCC content rules
- For international third-party traffic, the destination country must have a third-party traffic agreement with the United States

### 9.3 Station Identification (97.119)

Each amateur station must transmit its assigned call sign at the end of each communication and at least every 10 minutes during a communication. Identification may be by CW, phone (voice in English), RTTY, or data emission.

**What this means for HomeLink Radio:**
- VarAC and APRS messages automatically include the station callsign in the packet header
- APRS beacons include the callsign
- The home callsign configured in HomeLink settings is used for all transmissions
- Digital modes (VARA, APRS) satisfy the identification requirement through the data protocol

### 9.4 Prohibited Transmissions (97.113)

Amateur stations must not transmit:
- Communications for hire or material compensation
- Music
- Obscene or indecent language
- False or deceptive messages or signals
- Encoded messages intended to obscure meaning (except for certain control signals)
- Broadcasting (one-way transmissions to the general public)

**What this means for HomeLink Radio:**
- All messages must be personal, non-commercial family communications
- No encryption is used — all messages are transmitted in the clear
- Sitreps and APRS bulletins are addressed to specific parties or the amateur community, not the general public
- The licensed operator should instruct family members on appropriate content

### 9.5 Automatic Control (97.109, 97.221)

Automatic control is permitted for digital stations on certain frequencies. The control operator does not need to be physically present at the control point, but must ensure compliance with all rules.

**What this means for HomeLink Radio:**
- APRS beacons are transmitted automatically at configured intervals — this is standard practice and permitted on APRS frequencies
- APRS bulletins and message replies are initiated by a human operator (the family member) through the web interface, not automatically triggered
- VarAC BBS serves files to connecting stations — the BBS operates under VarAC's own automatic control provisions
- The control operator should be reachable and able to shut down the station if needed

### 9.6 Emergency Communications (97.403, 97.405)

An amateur station may use any means of radio communications at its disposal to provide essential communication needs in connection with the immediate safety of human life and the immediate protection of property when normal communication systems are not functioning.

**What this means for HomeLink Radio:** During a declared emergency or disaster:
- Normal operating restrictions may be relaxed when necessary for life safety
- The sitrep system is designed for exactly this purpose — communicating family welfare status during emergencies
- RF fallback (Soundmodem, VARA FM) ensures communications continue when internet infrastructure fails
- APRS bulletins requesting relay assistance are appropriate during emergencies

### 9.7 Content Guidelines for Family Members

If a non-licensed family member will be using HomeLink Radio, the licensed operator should brief them on these rules:

1. **Keep it personal** — Messages should be family communications, not commercial or business content
2. **Keep it clean** — No obscene, indecent, or profane language
3. **Keep it honest** — No false or misleading information
4. **Keep it relevant** — Sitreps should contain factual status information
5. **No secrets** — All amateur radio transmissions are unencrypted and can be received by anyone. Do not include passwords, financial details, or information you would not want made public

### 9.8 International Considerations

If the traveler is operating from outside the United States:
- Third-party traffic is only permitted to countries with a third-party traffic agreement with the United States (a current list is maintained by the ARRL at [arrl.org](http://www.arrl.org/third-party-operating-agreements))
- The traveler must comply with the radio regulations of the country they are operating from
- Some countries do not permit amateur radio operation by visitors; reciprocal operating permits may be required

---

## 10. Troubleshooting

### VarAC Database Connection Error

| Symptom | Solution |
|---------|----------|
| "File not found" | Verify the database path in Settings. Click "Browse" to locate `VarAC.db`. |
| "Not a valid VarAC database" | Ensure VarAC has been run at least once to create the database tables. |
| Status stays "Waiting" | Check that VarAC is running. Enable auto-launch in Settings by setting the VarAC executable path. |

### No Alerts Received

| Symptom | Solution |
|---------|----------|
| Messages exist in VarAC but no alerts | Check that the sender's callsign is in the "Watch For" list. Leave it empty to watch all callsigns. |
| APRS messages not detected | Verify APRS is enabled and the APRS-IS connection indicator shows green. Check the passcode. |
| Winlink messages not detected | Ensure Pat is running (`auto_launch` enabled) and the Pat HTTP address is correct. |

### APRS Issues

| Symptom | Solution |
|---------|----------|
| "APRS send failed" | Check internet connection. Verify the APRS-IS passcode (use "Calculate" button). |
| RF APRS not transmitting | Ensure Soundmodem is running and the KISS port matches. Check that your radio PTT is working. |
| Bulletin not appearing on aprs.fi | Check `https://aprs.fi/?c=raw&call=YOURCALL` for raw packets. The bulletin board page may have display delays. |
| Passcode incorrect | Click "Calculate" in APRS settings to auto-generate. The passcode is derived from your base callsign (without SSID). |

### Pushover Issues

| Symptom | Solution |
|---------|----------|
| Test notification not received | Verify both User Key and API Token are correct. Check your Pushover app is installed and logged in. |
| Quick reply links not working | Ensure HomeLink Radio is running and accessible when you tap the link. |

### Winlink / Pat Issues

| Symptom | Solution |
|---------|----------|
| Pat not starting | Verify the executable path is correct. Check that the port (`8080`) is not in use by another application. |
| No messages syncing | Pat requires internet for CMS (telnet) mode. Check `poll_interval` is reasonable (60-600 seconds). |
| RF fallback not working | Ensure VARA FM is installed and the gateway callsign is correct. The gateway station must be within radio range. |

### General

| Symptom | Solution |
|---------|----------|
| Web interface not loading | Check that `python monitor.py` is running. Verify the port (default: 5000) is not blocked. |
| "Connection stuck on waiting" | Restart the application. Check the terminal window for error messages. |
| Sitrep button not visible | The BBS directory must be configured. Check that VarAC's `.ini` file is readable or set the BBS Directory Override in Settings. |

---

## 11. Building a Standalone EXE

You can build HomeLink Radio as a single `.exe` file that runs without a Python installation.

### Prerequisites

Install PyInstaller:
```
pip install pyinstaller
```

### Build

Double-click `build_exe.bat` or run:
```
pyinstaller --onefile --name HomeLink_Radio monitor.py
```

The executable will be created at `dist\HomeLink_Radio.exe`.

### Distribution

To distribute HomeLink Radio to another computer:
1. Copy `HomeLink_Radio.exe` to the target machine
2. Run it — a `config.json` will be created on first launch
3. Configure Settings through the web interface
4. VarAC and other external programs must still be installed separately

---

## License and Disclaimer

HomeLink Radio is open-source software provided as-is, without warranty. The licensed amateur radio operator is solely responsible for all transmissions made from their station, including those initiated through this application. This software does not constitute legal advice regarding FCC regulations. Consult the [ARRL](http://www.arrl.org) or the [FCC](https://www.fcc.gov/wireless/bureau-divisions/mobility-division/amateur-radio-service) for authoritative guidance on amateur radio rules.

---

*73 de KK4ODA — HomeLink Radio*
