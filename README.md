# HamLink Radio

A family emergency communications app for licensed amateur radio operators. HamLink monitors VarAC, APRS, and Winlink for check-in messages from a traveling loved one and lets the family at home send replies via the internet, post situation reports for pickup or relay, and receive phone notifications via the internet — all from a simple web interface.

No ham radio knowledge needed on the home end.

## Features

- **Multi-channel monitoring** — VarAC VMail, APRS (internet + RF), and Winlink/Pat
- **Sitrep system** — Post structured family status reports to the VarAC BBS with one tap
- **VarAC broadcast** — Automatically announces sitreps to all VarAC stations on frequency
- **Phone notifications** — Pushover alerts with optional quick reply links (with confirmation)
- **RF fallback** — APRS via Soundmodem and Winlink via VARA FM gateway when internet is down. RF fallback is blocked by default and requires a licensed operator present, or an emergency involving immediate safety of life or property (FCC Part 97.403)
- **Position tracking** — APRS beacons and last-known position display
- **FCC compliance safeguards** — Non-compliant RF transmissions (exceeding 500 Hz bandwidth) are blocked by default under FCC Part 97.221; channel labels show internet vs RF path; all RF actions require explicit override
- **Message logging** — Persistent CSV log of all incoming and outgoing messages
- **Simple web UI** — Designed for non-technical family members; works on PC or phone

## Quick Start

1. Double-click `start_hamlink.bat` (installs dependencies automatically)
2. Open `http://127.0.0.1:5000` in your browser
3. Accept the regulatory compliance notice
4. Click the gear icon and configure:
   - **Their Name** and **Home Station Callsign**
   - **Watch Callsigns** — the traveler's callsign(s)
   - **VarAC Database Path** — browse to `VarAC.db`
   - Enable APRS, Winlink, Pushover as needed

See [MANUAL.md](MANUAL.md) for complete documentation including all configuration options, use cases, and FCC compliance guide.

## How It Works

**Receiving:** HamLink polls the VarAC database, listens on APRS-IS and RF, and checks the Winlink inbox. When a message arrives from a watched callsign, it plays an alert sound, shows the message on screen, and sends a phone notification.

**Sending:** The family member types a reply or taps a quick reply. By default, replies go via internet: APRS-IS, Winlink telnet, or VarAC outbox (queued for pickup). If internet is down, RF fallback (APRS via Soundmodem, Winlink via VARA FM) is available but blocked by default — it requires the operator to confirm they are a licensed amateur radio operator, or to invoke the emergency exception for immediate safety of life or property (FCC Part 97.403). Data transmissions at 500 Hz bandwidth or below (VARA HF 500 Hz) are compliant under automatic control (FCC Part 97.221).

**Sitreps:** The family member posts a structured status report (house, vehicles, utilities, health, needs, relocation) to the VarAC BBS. HamLink automatically sends a 500 Hz VarAC broadcast so nearby hams can connect to the BBS and relay the information to the traveler.

## Files

| File | Description |
|------|-------------|
| `monitor.py` | The application (Flask backend + embedded frontend) |
| `start_hamlink.bat` | Windows launcher with dependency checking |
| `build_hamlink_exe.bat` | Build standalone .exe via PyInstaller |
| `config.json` | Auto-saved settings (created on first run, not tracked in git) |
| `MANUAL.md` | Complete user manual with FCC compliance guide |

## Requirements

- Python 3.8+ (or standalone .exe)
- VarAC V5+ with VARA HF modem
- Optional: Pat (Winlink), Soundmodem (RF APRS), VARA FM (RF Winlink), Pushover account

## FCC Compliance

HamLink is designed for use by licensed amateur radio operators under FCC Part 97. The licensed operator is responsible for all transmissions from the station. RF transmissions initiated through the app require a confirmation step citing Part 97.115 (third-party traffic) and Part 97.403 (emergency exception). See the [FCC Compliance section](MANUAL.md#9-fcc-compliance) in the manual for details.

## Contributing

Bugs and feature requests: [GitHub Issues](https://github.com/KK4ODA/HamLink-Radio/issues)

## License

Open source. 73 de KK4ODA.
