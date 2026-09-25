# phpvm · PHP Version Manager

[简体中文](README.md) · **English**

A cross-platform (Windows / macOS / Linux) desktop tool that keeps your local PHP development
environment in one place: **multiple PHP versions, Nginx, Redis, MySQL, SQLite, sites and HTTPS**.
Each PHP version is started and stopped precisely by port, so versions never interfere
(no more `taskkill /IM php-cgi.exe` killing every version like the old `start_phpXX.bat` scripts).

- Local use only, no login; pure Python standard library (tkinter), **no third-party dependencies**
- GUI and CLI (`cli.py`) share the same `core/*` services
- UI in 5 languages (Simplified / Traditional Chinese, English, Japanese, Korean); light / dark / follow-system themes
- Environment root: `C:\wnrp` on Windows, `~/wnrp` elsewhere (override with `WNRP_ROOT`)

> This document describes **implemented** features only. See [ROADMAP.md](ROADMAP.md) for the roadmap,
> [docs/](docs/README.md) for architecture and flows, and [AGENTS.md](AGENTS.md) for the CLI contract used by AI / scripts.

## Key Features

**Diagnostics & observability**

- **One-click health check / 502 diagnosis**: per-site checks for Nginx running, include, hosts, port mapping, PHP running, docroot, certificate and error log, with readable conclusions
- **Overview dashboard**: service status + site alerts + recent crashes / run logs on one screen, plus start-all / stop-all
- **Log page**: Nginx / PHP / site logs in a single view (incremental follow, coloring, filtering); plus phpvm's own "Run Log", searchable and exportable

**Sites**

- **Site mapping matrix**: domains / config file / port / PHP version / docroot / hosts status at a glance, with anomalous entries highlighted and explained
- **New-site wizard**: 6 templates (Laravel / WordPress / ThinkPHP / plain PHP / static / SPA); automatic include, `nginx -t`, hosts write and graceful reload, with one-click rollback on failure
- **One-click HTTPS**: enable / disable for both new and **existing** sites (mkcert preferred, openssl self-signed fallback; backed up first, rolled back if validation fails)
- **Search & context actions**: live filtering; right-click to switch PHP, enable / disable a site, open a terminal, copy domain / URL, diagnose the site, clean hosts, apply project config
- **Project config `.phpvm.json`**: declare PHP version / domains / services at the project root; `project apply` aligns everything at once (fastcgi_pass, hosts, services)

**Services**

- **PHP**: auto-discovery (including Homebrew), start / stop / restart by port, edit port with one-click vhost sync, ini form editing and recommended settings, extension management and online install, download new versions, version self-check, Composer detection, switch the terminal `php`, **one-click Xdebug toggle**
- **Nginx**: start / graceful reload / config check / recommended settings
- **Redis**: multi-instance discovery and control, command execution (confirmation for dangerous commands), DB keyspace chart
- **MySQL**: instance discovery and control (Windows service first), config and error log
- **SQLite**: read-only browsing of schema and queries

**Data & operations**

- **Environment backup & migration**: export config.json / vhost / nginx.conf / php.ini to a zip and restore in one click (backed up first, full rollback if `nginx -t` fails; database data excluded)
- **Adminer database GUI**: download the single file and host it at `adminer.test` (zero driver dependencies, local development only)
- **Crash detection & self-healing**: detect php-cgi crashes and alert; optional standalone watchdog to restart automatically (debounce + rate limit)
- **Auto-update** (installer builds), **launch at login**, **system tray** (Windows)

## Getting Started

**Installer (recommended)**

- macOS: download `phpvm-<version>-macos.dmg` and drag `phpvm.app` into Applications (right-click → Open if Gatekeeper warns)
- Windows: download `phpvm-<version>-windows-setup.exe` and run the installer
- Installer builds check for updates silently on launch and can upgrade from the "About" page

**From source / portable**

```bash
python3 main.py                # macOS / Linux (or double-click phpvm.command)
phpvm.bat                      # Windows (runs via pythonw, no console window)
python3 main.py --start-all    # start all services headlessly (for login scripts)
```

Requires Python 3.10+ with tkinter (on macOS: `brew install python-tk@3.13`); no third-party packages.

## CLI (`cli.py`)

Besides the GUI, phpvm ships a **headless, fully non-interactive** CLI for AI tools and ops scripts
(it does not import tkinter, so it runs over SSH / in CI):

```bash
python3 cli.py env --json       # environment and all-service snapshot
python3 cli.py schema --json    # self-describing contract for every command / argument / default
```

- Add `--json` to any command: stdout carries a single `{"ok", "command", "data", "warnings"}` document (English field names, stable contract)
- Exit codes: `0` success / `1` business failure (see `data.error`) / `2` usage error
- Safe defaults: write operations support `--dry-run`; deletion requires `--yes`; dangerous Redis commands require `--force`; files are backed up (`.bak`) before changes
- Groups cover PHP / Nginx / sites / hosts / Redis / MySQL / SQLite / service orchestration / diagnostics / logs / backup / project config / Adminer

Full command reference: [docs/CLI.md](docs/CLI.md) (generated from `cli.py`, so it never drifts).

## Default Port Mapping

| Version dir | Default port | Note |
|---|---|---|
| php | 9001 | Compatible with `fund.conf` / `type_test.conf` |
| php56 | 9056 | |
| php72 | 9072 | |
| php73 | 9073 | |
| php74 | 9074 | |
| php8 | 9080 | |
| php81 | 9081 | |
| php82 | 9000 | Main version, default vhost target |
| php85 | 9085 | |
| php83 / php84 | — | New versions installed online derive the port as `9000 + version` and persist it to `config.json` |

`fastcgi_pass 127.0.0.1:<port>` in a vhost decides which PHP version serves the site; after changing a port, use "one-click sync" to update vhosts and reload gracefully.

## Config & Data Directory

- **config.json** stores port mappings and settings (self-healing switch, language, theme, autostart scope, ...). It lives in the program directory when that is writable; under read-only install locations (`.app` / `Program Files`) it moves to `~/.phpvm`, so upgrading program files **never loses your config**. `PHPVM_HOME` forces a custom data directory
- If the file is corrupt or invalid JSON, built-in defaults are restored and re-saved
- The CLI can read / write it too: `cli.py config get settings.theme` / `cli.py config set settings.theme dark`

## Documentation

| Document | Contents |
|---|---|
| [AGENTS.md](AGENTS.md) | CLI contract for AI / automation (single entry point `cli.py`) |
| [docs/CLI.md](docs/CLI.md) | Full command reference (generated) |
| [docs/MODULES.md](docs/MODULES.md) | Capability → UI entry → core location map |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layering, concurrency, data locations, write rules |
| [docs/FLOWS.md](docs/FLOWS.md) | Startup / site creation / port sync / autostart / self-healing / update flows |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Key trade-off records |
| [ROADMAP.md](ROADMAP.md) | Gap analysis and development roadmap |

## FAQ

- **Site returns 502 / 404 after changing a port**: run the "one-click sync" dialog to replace and reload Nginx, or check the highlighted entries on the Sites page
- **php-cgi keeps crashing (502)**: a status-bar alert opens the event details (faulting module / exception code / offset); `0xc0000005` is often opcache JIT, which can be turned off in "PHP Versions → Edit config"
- **Which ini does FastCGI use**: always the `php.ini` inside the version directory; **CLI and FastCGI share the same file**
- **Port already in use**: the UI reports the owning process (name + PID); stop it or pick another port and sync the vhost

## macOS / Linux Support

- Environment root defaults to `~/wnrp`; PHP scans Homebrew kegs, Nginx / Redis detect brew installs, and terminal `php` switching maintains the managed block in `~/.zshrc` and `~/.bash_profile`
- hosts writes request system authorization via `osascript`; crash detection parses `php-cgi-*.ips` under `~/Library/Logs/DiagnosticReports`
- Packaging: `python3 packaging/build.py macos` produces a `.dmg`; user data lives in `~/.phpvm`, so upgrades keep your config
- Not yet supported: macOS menu-bar tray; online PHP / extension installs fall back to brew / pecl guidance on mac
