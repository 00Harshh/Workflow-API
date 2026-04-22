#!/usr/bin/env python3
from __future__ import annotations
"""
Workflow API CLI
Usage: python cli.py <command>
"""

import sys
import subprocess
import json
import os
import time
from collections import deque
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box
from rich.prompt import Prompt, Confirm

from core.auth import (
    create_key,
    get_all_keys,
    get_gateways,
    is_key_expired,
    load_config,
    parse_allowed_gateways,
    parse_expiration,
    revoke_key,
    save_config,
    validate_allowed_gateways,
)
from core.logger import get_log_path, level_for_status

console = Console()
CONFIG_PATH = Path("config.yaml")


# ── Helpers ────────────────────────────────────────────────────────────────────

def print_banner():
    console.print()
    console.print(Panel.fit(
        "[bold white]Workflow API[/bold white]  [dim]— turn any workflow into an API[/dim]",
        border_style="dim"
    ))
    console.print()


def config_exists() -> bool:
    return CONFIG_PATH.exists()


def require_config():
    if not config_exists():
        console.print("[red]✗[/red] No config.yaml found. Run [bold]python cli.py init[/bold] first.")
        sys.exit(1)


def _slugify(value: str) -> str:
    slug = value.strip().lower().replace(" ", "-")
    return "".join(ch for ch in slug if ch.isalnum() or ch == "-").strip("-") or "workflow"


def _normalize_endpoint(endpoint: str) -> str:
    endpoint = endpoint.strip()
    if not endpoint:
        raise ValueError("Endpoint path cannot be empty.")
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint
    return endpoint


def _prompt_required(label: str, default: str | None = None) -> str:
    while True:
        value = Prompt.ask(label, default=default).strip()
        if value:
            return value
        console.print("[red]Please enter a value. Blank values are not valid here.[/red]")


def _prompt_port(default: int = 8000) -> int:
    while True:
        value = Prompt.ask("[cyan]Port[/cyan]", default=str(default)).strip()
        try:
            port = int(value)
        except ValueError:
            console.print("[red]Port must be a number, for example 8000. Do not paste commands here.[/red]")
            continue
        if 1 <= port <= 65535:
            return port
        console.print("[red]Port must be between 1 and 65535.[/red]")


def _base_config(workflows: list[dict], port: int) -> dict:
    return {
        "workflows": workflows,
        "keys": [],
        "logging": {
            "file": "logs/usage.log",
        },
        "stripe": {
            "webhook_secret": None,
            "api_key": None,
            "price_to_gateway": {},
        },
        "server": {
            "host": "0.0.0.0",
            "port": port,
        },
    }


def _print_key_created(record: dict, rate_limit: int, expires_at: str | None, allowed_gateways: list[str] | None):
    limit_str = f"{rate_limit} req/min" if rate_limit > 0 else "Unlimited"
    expires_str = expires_at or "Never"
    scope_str = ", ".join(allowed_gateways) if allowed_gateways else "All gateways"
    console.print()
    console.print(Panel(
        f"[bold green]Key created[/bold green]\n\n"
        f"  Name        [cyan]{record['name']}[/cyan]\n"
        f"  Rate limit  [cyan]{limit_str}[/cyan]\n"
        f"  Scope       [cyan]{scope_str}[/cyan]\n"
        f"  Expires     [cyan]{expires_str}[/cyan]\n"
        f"  Key         [bold yellow]{record['key']}[/bold yellow]\n\n"
        f"[dim]Share this key with your user. It won't be shown again.[/dim]",
        border_style="green",
        expand=False
    ))


def _print_next_steps(endpoint: str, key: str, port: int):
    console.print()
    console.print(Panel(
        "[bold]Next steps[/bold]\n\n"
        "1. Keep your n8n Webhook node on [cyan]Listen for test event[/cyan].\n"
        f"2. Start Workflow API:\n   [cyan]python3 cli.py start[/cyan]\n\n"
        "3. In a second terminal, test it:\n"
        f"   [cyan]curl -X POST http://localhost:{port}{endpoint} \\\\[/cyan]\n"
        f"   [cyan]  -H \"Authorization: Bearer {key}\" \\\\[/cyan]\n"
        f"   [cyan]  -H \"Content-Type: application/json\" \\\\[/cyan]\n"
        f"   [cyan]  -d '{{\"name\":\"Harsh\",\"test\":true}}'[/cyan]",
        border_style="green",
        expand=False,
    ))


def _format_expiration(key_record: dict) -> str:
    expires_at = key_record.get("expires_at")
    if not expires_at:
        return "Never"
    if is_key_expired(key_record):
        return f"Expired ({expires_at})"
    return str(expires_at)


def _format_scope(key_record: dict, max_length: int = 32) -> str:
    allowed_gateways = key_record.get("allowed_gateways")
    if not allowed_gateways:
        return "All"

    scope = ", ".join(allowed_gateways)
    if len(scope) <= max_length:
        return scope
    return scope[: max_length - 3] + "..."


def _log_line_level(line: str) -> str | None:
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return None

    level = entry.get("level")
    if level:
        return str(level).upper()

    status = entry.get("status")
    if isinstance(status, int):
        return level_for_status(status)
    return None


def _matches_log_level(line: str, level: str | None) -> bool:
    if not level:
        return True
    return _log_line_level(line) == level.upper()


def _print_log_line(line: str):
    click.echo(line.rstrip("\n"))


def _tail_log_file(path: Path, lines: int, level: str | None):
    matches = deque(maxlen=lines)
    with open(path, "r") as f:
        for line in f:
            if _matches_log_level(line, level):
                matches.append(line)

    for line in matches:
        _print_log_line(line)


def _follow_log_file(path: Path, level: str | None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)

    with open(path, "r") as f:
        f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if line:
                if _matches_log_level(line, level):
                    _print_log_line(line)
                continue
            time.sleep(0.5)


# ── CLI root ────────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """Workflow API — API layer for your workflows."""
    pass


# ── n8n quick setup ───────────────────────────────────────────────────────────

@cli.command("n8n")
@click.option("--url", "webhook_url", help="Your n8n webhook URL, e.g. http://localhost:5678/webhook-test/n8ntest.")
@click.option("--name", default="n8ntest", show_default=True, help="Workflow API workflow/gateway name.")
@click.option("--endpoint", default=None, help="Public Workflow API endpoint. Defaults to /run/<name>.")
@click.option("--port", default=8000, show_default=True, type=click.IntRange(1, 65535), help="Workflow API server port.")
@click.option("--key-name", default="Test", show_default=True, help="Name for the generated API key.")
@click.option("--rate-limit", default=60, show_default=True, type=int, help="Requests per minute for the generated key.")
@click.option("--force", is_flag=True, help="Overwrite config.yaml without asking.")
def setup_n8n(webhook_url, name, endpoint, port, key_name, rate_limit, force):
    """One-command n8n setup. Creates config.yaml and a test API key."""
    print_banner()
    console.print("[bold]n8n quick setup[/bold]\n")

    if config_exists() and not force:
        overwrite = Confirm.ask("[yellow]config.yaml already exists. Overwrite it for this n8n setup?[/yellow]", default=False)
        if not overwrite:
            console.print("[dim]Aborted. Nothing changed.[/dim]")
            return

    if not webhook_url:
        webhook_url = _prompt_required(
            "[cyan]n8n webhook URL[/cyan]",
            "http://localhost:5678/webhook-test/n8ntest",
        )

    if not webhook_url.startswith(("http://", "https://")):
        raise click.ClickException("Webhook URL must start with http:// or https://")

    workflow_name = _slugify(name)
    endpoint_path = _normalize_endpoint(endpoint or f"/run/{workflow_name}")
    workflows = [{
        "name": workflow_name,
        "endpoint": endpoint_path,
        "target": webhook_url.strip(),
        "method": "POST",
    }]

    save_config(_base_config(workflows, port))
    record = create_key(
        name=key_name,
        rate_limit_per_minute=rate_limit,
        expires_at=None,
        allowed_gateways=[workflow_name],
    )

    console.print("[green]✓[/green] config.yaml created for n8n")
    console.print(f"[green]✓[/green] Gateway: [bold]{endpoint_path}[/bold] → [dim]{webhook_url}[/dim]")
    _print_key_created(record, rate_limit, None, [workflow_name])
    _print_next_steps(endpoint_path, record["key"], port)


# ── init ───────────────────────────────────────────────────────────────────────

@cli.command()
def init():
    """Interactive setup wizard. Run this first."""
    print_banner()
    console.print("[bold]Setup wizard[/bold]\n")

    if config_exists():
        overwrite = Confirm.ask("[yellow]config.yaml already exists. Overwrite?[/yellow]", default=False)
        if not overwrite:
            console.print("[dim]Aborted.[/dim]")
            return

    # Collect workflows
    workflows = []
    console.print("[dim]Add your workflow(s). Press enter to skip optional fields.[/dim]\n")

    while True:
        name = _prompt_required("[cyan]Workflow name[/cyan] (e.g. summarize)")
        target = _prompt_required("[cyan]Webhook / target URL[/cyan] (e.g. http://localhost:5678/webhook/abc)")
        endpoint = _normalize_endpoint(Prompt.ask(
            "[cyan]Endpoint path[/cyan]",
            default=f"/run/{_slugify(name)}"
        ))
        method = Prompt.ask("[cyan]HTTP method[/cyan]", default="POST").upper()

        workflows.append({
            "name": name,
            "endpoint": endpoint,
            "target": target,
            "method": method,
        })

        console.print()
        another = Confirm.ask("Add another workflow?", default=False)
        if not another:
            break
        console.print()

    # Server config
    console.print()
    port = _prompt_port(default=8000)

    save_config(_base_config(workflows, port))

    console.print()
    console.print("[green]✓[/green] config.yaml created\n")

    # Offer to create first key
    create_first = Confirm.ask("Create your first API key now?", default=True)
    if create_first:
        console.print()
        _create_key_interactive()

    console.print()
    console.print(Panel(
        "[bold]You're ready.[/bold]\n\n"
        "  Start the server:   [cyan]python cli.py start[/cyan]\n"
        "  Manage keys:        [cyan]python cli.py keys[/cyan]\n"
        "  View status:        [cyan]python cli.py status[/cyan]",
        border_style="green",
        expand=False
    ))
    console.print()


# ── start ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--port", default=None, type=int, help="Override port from config.")
@click.option(
    "--workers",
    default=1,
    show_default=True,
    type=click.IntRange(1, 32),
    help="Number of uvicorn worker processes. Use >1 for production. Redis recommended for accurate rate-limiting across workers.",
)
def start(port, workers):
    """Start the API server."""
    require_config()

    cfg = load_config()
    keys = cfg.get("keys") or []
    workflows = get_gateways(cfg)

    console.print()
    console.print(Panel.fit(
        "[bold white]Workflow API[/bold white] starting...",
        border_style="dim"
    ))

    if not keys:
        console.print()
        console.print("[yellow]⚠[/yellow]  No API keys found.")
        console.print('   Create one: [cyan]python cli.py keys create[/cyan]\n')

    console.print()
    for wf in workflows:
        console.print(f"  [green]→[/green] [bold]{wf['endpoint']}[/bold]  [dim]→  {wf['target']}[/dim]")

    server_port = port or cfg.get("server", {}).get("port", 8000)
    server_host = cfg.get("server", {}).get("host", "0.0.0.0")
    console.print()
    console.print(f"  [dim]Workers:[/dim]       {workers}")
    console.print(f"  [dim]Listening on[/dim] http://0.0.0.0:{server_port}")
    console.print(f"  [dim]Docs:[/dim]          http://localhost:{server_port}/docs")
    console.print()

    if workers > 1:
        import os as _os
        redis_url = _os.environ.get("REDIS_URL", "")
        if not redis_url:
            console.print(
                "[yellow]⚠[/yellow]  Multiple workers without REDIS_URL set.\n"
                "   Rate limits will be per-worker (not global). "
                "Set [cyan]REDIS_URL=redis://localhost:6379[/cyan] for accurate limiting.\n"
            )
        # uvicorn --workers requires passing the app as a string import path
        subprocess.run([
            sys.executable, "-m", "uvicorn", "main:app",
            "--host", server_host,
            "--port", str(server_port),
            "--workers", str(workers),
        ])
    else:
        # Single worker: run main.py directly (preserves startup prints)
        subprocess.run([sys.executable, "main.py"])


# ── status ─────────────────────────────────────────────────────────────────────

@cli.command()
def status():
    """Show current config and active keys."""
    require_config()
    print_banner()

    cfg = load_config()
    workflows = get_gateways(cfg)
    keys = cfg.get("keys") or []
    server = cfg.get("server", {})

    # Workflows table
    wf_table = Table(box=box.SIMPLE, show_header=True, header_style="bold dim")
    wf_table.add_column("Name",     style="bold")
    wf_table.add_column("Endpoint", style="cyan")
    wf_table.add_column("Target",   style="dim")
    wf_table.add_column("Method",   style="dim")

    for wf in workflows:
        wf_table.add_row(wf["name"], wf["endpoint"], wf["target"], wf.get("method", "POST"))

    console.print("[bold]Workflows[/bold]")
    if workflows:
        console.print(wf_table)
    else:
        console.print("  [dim]None. Run python cli.py init[/dim]\n")

    # Keys table
    key_table = Table(box=box.SIMPLE, show_header=True, header_style="bold dim")
    key_table.add_column("Name",       style="bold")
    key_table.add_column("Rate limit", style="cyan")
    key_table.add_column("Scope",      style="magenta")
    key_table.add_column("Expires",    style="yellow")
    key_table.add_column("Created",    style="dim")
    key_table.add_column("Key prefix", style="dim")

    for k in keys:
        rpm = k.get("rate_limit_per_minute", 60)
        limit_str = f"{rpm} req/min" if rpm > 0 else "Unlimited"
        key_table.add_row(
            k["name"],
            limit_str,
            _format_scope(k),
            _format_expiration(k),
            k.get("created_at", "-"),
            k["key"][:22] + "...",
        )

    console.print("[bold]API Keys[/bold]")
    if keys:
        console.print(key_table)
    else:
        console.print("  [dim]None. Run python cli.py keys create[/dim]\n")

    console.print(f"[bold]Server[/bold]  [dim]port {server.get('port', 8000)}[/dim]\n")


# ── logs ───────────────────────────────────────────────────────────────────────

@cli.command("logs")
@click.option("--follow", "-f", is_flag=True, help="Stream new log entries as they are written.")
@click.option("--level", type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False), help="Only show logs with this severity.")
@click.option("--lines", default=20, show_default=True, type=int, help="Number of matching log lines to print before exiting or following.")
def logs(follow, level, lines):
    """Show Workflow API access logs."""
    log_path = get_log_path()
    normalized_level = level.upper() if level else None

    if not log_path.exists() and not follow:
        console.print(f"[yellow]No log file found at {log_path}[/yellow]")
        return

    if lines > 0 and log_path.exists():
        _tail_log_file(log_path, lines, normalized_level)

    if follow:
        _follow_log_file(log_path, normalized_level)


# ── keys group ─────────────────────────────────────────────────────────────────

@cli.group()
def keys():
    """Manage API keys."""
    pass


def _create_key_interactive(name=None, rate_limit=None, expires_at=None, expires_in=None, gateways=None):
    if not name:
        name = Prompt.ask("[cyan]Key name / tier[/cyan] (e.g. Free, Pro, Enterprise)")
    if rate_limit is None:
        rate_input = Prompt.ask("[cyan]Rate limit (requests/min)[/cyan]  [dim]0 = unlimited[/dim]", default="60")
        rate_limit = int(rate_input)

    try:
        parsed_expires_at = parse_expiration(expires_at=expires_at, expires_in=expires_in)
        allowed_gateways = parse_allowed_gateways(gateways)
        validate_allowed_gateways(allowed_gateways)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    record = create_key(
        name=name,
        rate_limit_per_minute=rate_limit,
        expires_at=parsed_expires_at,
        allowed_gateways=allowed_gateways,
    )

    _print_key_created(record, rate_limit, parsed_expires_at, allowed_gateways)


@keys.command("create")
@click.option("--name", help="Name/tier for this key, for example Free or Pro.")
@click.option("--rate-limit", type=int, help="Requests per minute. Use 0 for unlimited.")
@click.option("--expires-in", help="Relative expiration, for example 30d, +30d, 12h, or 45m.")
@click.option("--expires-at", help="Absolute expiration, for example 2026-12-31 or 2026-12-31T23:59:59Z.")
@click.option("--gateways", "--scope", help="Comma-separated gateway names this key can access. Omit for all gateways.")
def keys_create(name, rate_limit, expires_in, expires_at, gateways):
    """Generate a new API key with a custom rate limit."""
    require_config()
    console.print()
    _create_key_interactive(
        name=name,
        rate_limit=rate_limit,
        expires_at=expires_at,
        expires_in=expires_in,
        gateways=gateways,
    )
    console.print()


@keys.command("list")
def keys_list():
    """List all active API keys."""
    require_config()

    all_keys = get_all_keys()
    console.print()

    if not all_keys:
        console.print("  [dim]No keys yet. Run:[/dim] [cyan]python cli.py keys create[/cyan]\n")
        return

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold dim")
    table.add_column("Name",        style="bold")
    table.add_column("Rate limit",  style="cyan")
    table.add_column("Scope",       style="magenta")
    table.add_column("Expires",     style="yellow")
    table.add_column("Created",     style="dim")
    table.add_column("Key",         style="dim")

    for k in all_keys:
        rpm = k.get("rate_limit_per_minute", 60)
        limit_str = f"{rpm} req/min" if rpm > 0 else "Unlimited"
        table.add_row(
            k["name"],
            limit_str,
            _format_scope(k),
            _format_expiration(k),
            k.get("created_at", "-"),
            (k.get("key_prefix") or k.get("key", "")[:16]) + "...",
        )

    console.print(table)


@keys.command("revoke")
@click.argument("name")
def keys_revoke(name):
    """Revoke all keys with the given name."""
    require_config()
    console.print()

    confirm = Confirm.ask(f"[yellow]Revoke all keys named '{name}'?[/yellow]")
    if not confirm:
        console.print("[dim]Aborted.[/dim]\n")
        return

    if revoke_key(name):
        console.print(f"[green]✓[/green] Revoked keys named '[bold]{name}[/bold]'\n")
    else:
        console.print(f"[red]✗[/red] No keys found with name '[bold]{name}[/bold]'\n")


# Singular alias: `python cli.py key create` works the same as `python cli.py keys create`.
cli.add_command(keys, "key")


# ── migrate ───────────────────────────────────────────────────────────────────

@cli.group("migrate")
def migrate():
    """Data migration utilities."""
    pass


@migrate.command("hash-keys")
def migrate_hash_keys():
    """
    Replace plaintext API keys in config.yaml with SHA-256 hashes (in-place).
    Safe to run multiple times. Existing keys keep working after migration.
    """
    require_config()
    console.print()
    from core.auth import migrate_keys_to_hashed
    n = migrate_keys_to_hashed()
    if n:
        console.print(f"[green]✓[/green] Hashed [bold]{n}[/bold] key(s) in config.yaml")
    else:
        console.print("[dim]All keys are already hashed. Nothing to do.[/dim]")
    console.print()


@migrate.command("yaml-to-sqlite")
@click.option("--sqlite-path", default="workflow-api.db", show_default=True, help="SQLite DB path.")
@click.option("--switch", is_flag=True, help="Also update config.yaml storage.backend to sqlite.")
def migrate_yaml_to_sqlite(sqlite_path, switch):
    """
    Copy all keys from config.yaml into a SQLite database.
    Use --switch to automatically change storage.backend to sqlite.
    """
    require_config()
    console.print()

    from core.auth import migrate_yaml_to_sqlite as _migrate, load_config, save_config

    n = _migrate(sqlite_path)
    console.print(f"[green]✓[/green] Migrated [bold]{n}[/bold] key(s) to SQLite: [cyan]{sqlite_path}[/cyan]")

    if switch:
        cfg = load_config()
        if "storage" not in cfg or cfg["storage"] is None:
            cfg["storage"] = {}
        cfg["storage"]["backend"] = "sqlite"
        cfg["storage"]["sqlite_path"] = sqlite_path
        save_config(cfg)
        console.print(f"[green]✓[/green] config.yaml updated: storage.backend = sqlite")
        console.print("[dim]Restart Workflow API for the change to take effect.[/dim]")
    else:
        console.print()
        console.print(Panel(
            f"Keys copied. To switch to SQLite, either:\n"
            f"  Run: [cyan]python cli.py migrate yaml-to-sqlite --switch[/cyan]\n"
            f"  Or set [cyan]storage.backend: sqlite[/cyan] in config.yaml, then restart.",
            border_style="dim",
            expand=False,
        ))
    console.print()


# ── entry ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
