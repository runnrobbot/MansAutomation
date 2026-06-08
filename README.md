# MansAutomation

Production-grade desktop automation assistant for fast online checkout workflows, flash sales, and high-traffic web interactions.

## Features

- Async-first browser automation via Playwright with persistent contexts and cookie persistence
- Adaptive intelligent form detection (labels, placeholders, ids, names, ARIA attributes, nearby text, DOM hierarchy)
- Encrypted local profile storage with SQLite, JSON and YAML interoperability
- Modular plugin architecture for site-specific workflows
- PyQt6 dark-mode desktop UI with sidebar, profile manager, automation controls, real-time logs, status monitoring, notifications
- Telegram, Discord webhook, desktop, and sound notifications
- CAPTCHA / waiting-room detection that pauses automation and yields to the user
- Smart retry, automatic recovery, and structured logging

This project does **not** implement and will refuse to ship any CAPTCHA bypass, queue bypass, anti-bot evasion, abusive request generation, DDoS behaviour, or credential theft.

## Setup

```cmd
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
python -m mansautomation
```

The first launch creates a per-user data directory containing:

- `keystore.bin` — AES-256 master key for encrypting profile data at rest
- `profiles.db` — SQLite database for profiles and workflow history
- `plugins/` — drop-in folder for additional automation plugins
- `sessions/` — persistent browser profiles (cookies, local storage, IndexedDB)
- log files in the OS-appropriate log directory

Settings live in YAML at the user config directory and are also editable from the GUI.

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
    core/              Domain models, config, DI container, exceptions
    services/          Logging, notifications, crypto, browser, storage
    automation/        Playwright runner, form detector, autofill engine, recovery
    profiles/          Profile manager and dataset engine (SQLite / JSON / YAML)
    plugins/           Plugin manager + base classes
    notifications/     Telegram, Discord, desktop, sound channels
    gui/               PyQt6 dark theme, main window, panels, theming
    utils/             Async helpers, fuzzy matching, DOM helpers
    config/            Default settings, schema, constants
plugins/               User-installable plugin directory (example included)
```
