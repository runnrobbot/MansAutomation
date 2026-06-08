# MansAutomation

Production-grade desktop automation assistant for fast online checkout workflows, flash sales, and high-traffic web interactions.

## Features

- Async-first browser automation via Playwright with persistent contexts and cookie persistence
- Adaptive intelligent form detection (labels, placeholders, ids, names, ARIA attributes, nearby text, DOM hierarchy)
- Encrypted local profile storage with SQLite, JSON and YAML interoperability
- Modular plugin architecture for site-specific workflows
- PyQt6 dark-mode desktop UI with sidebar, profile manager, automation controls, real-time logs, status monitoring, notifications
- Telegram, Discord webhook, desktop, and sound notifications
- Resilience layer: heartbeat watchdog, queue/waiting-room watcher, hydration & DOM-settle synchronization, stale-element-resilient interactions
- CAPTCHA / OTP / waiting-room detection that pauses automation and yields to the user
- Smart retry, automatic recovery, and structured logging

This project does **not** implement and will refuse to ship any CAPTCHA bypass, queue bypass, anti-bot evasion, abusive request generation, DDoS behaviour, or credential theft. When a queue or verification step is encountered, automation waits its turn or hands control back to you.

## Setup

```cmd
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
python -m mansautomation
```

Always run through the virtual environment interpreter so the dependencies resolve:

```cmd
.venv\Scripts\python.exe -m mansautomation
```

The first launch creates a per-user data directory containing:

- `keystore.bin` — AES-256 master key for encrypting profile data at rest
- `profiles.db` — SQLite database for profiles and workflow history
- `plugins/` — drop-in folder for additional automation plugins
- `sessions/` — persistent browser profiles (cookies, local storage, IndexedDB)
- log files in the OS-appropriate log directory

Settings live in YAML at the user config directory and are also editable from the GUI. Changes apply live (log level, browser options, notification channels, workflow timing) without restarting.

## Profiles

A profile stores the autofill dataset used during checkout: identity, address, login credentials (email + password, encrypted at rest), bank info, attendees, and custom fields. Create and edit profiles in the **Profiles** tab. Passwords and bank secrets are stripped from JSON/YAML exports.

## Automation tab

Pick a plugin and profile, then drive a workflow:

- **Search query / Event title / Category / Package / Quantity** — booking inputs. Category and Package accept comma-separated fallback lists (e.g. `CAT 1, FESTIVAL` and `CAT 1 RIGHT, CAT 1 LEFT, FESTIVAL B`). Matching is fuzzy, so partial or slightly misspelled names still resolve to the right card, and sold-out options fall through to the next entry.
- **Pre-sale auto-wait / max wait** — when tickets are not yet on sale, the workflow reads the countdown and waits (up to the budget) or pauses for you.
- **Queue auto-wait / max wait** — when a waiting room appears, the workflow waits in line, reports your position, and resumes package selection the moment the queue releases.
- **Login first (Sign in)** — controls authentication:
  - Already signed in → login is skipped regardless of this toggle.
  - Not signed in + toggle **on** → signs in using the profile credentials (or pauses for manual login if none are set).
  - Not signed in + toggle **off** → the workflow aborts and tells you to enable Sign in.
- **Start workflow / Search events / Buy ticket** — run the full workflow, a search-only discovery, or the booking pipeline up to (but not including) payment confirmation.

## Notifications

The **Settings** tab configures desktop, sound, Telegram, and Discord notifications, each with a test button that sends a real message through the channel. Critical workflow events (queue regressed/frozen, heartbeat unhealthy, manual-action-required) are pushed to the enabled channels.

## tiket.com plugin

The bundled `plugins/tiket_com.py` automates the full tiket.com flow: sign in (email-first with the continue/password steps), search the events catalogue, open the matching event, wait out any pre-sale countdown or waiting room, pick a package (with category + package fallback chains), set the ticket quantity, and pre-fill the buyer + attendee forms. It stops one click short of payment so you review and confirm manually. CAPTCHA, OTP, and queue screens are never bypassed.

## Development

```cmd
pip install -e .[dev]
ruff check .
mypy mansautomation
pytest
```

## Project layout

```
mansautomation/
    core/              Domain models, config, DI container, events, settings applier, exceptions
    services/          Logging, crypto, storage, browser lifecycle
    automation/        Runner, form detector, autofill engine, DOM extractor,
                       sync (hydration/DOM-settle), interactions (resilient clicks),
                       resilience (heartbeat + queue watcher), presale, queue_wait,
                       human-intervention detection
    profiles/          Profile manager and dataset engine (SQLite / JSON / YAML)
    plugins/           Plugin manager + base classes
    notifications/     Telegram, Discord, desktop + sound channels, dispatcher
    gui/               PyQt6 dark theme, main window, panels, widgets
    utils/             Async/Qt helpers
plugins/               User-installable plugin directory
    generic_checkout.py  Adaptive autofill for generic checkout pages
    tiket_com.py         Full tiket.com booking workflow
```
