"""Command line entry point: one image, one console script, the subcommand selects the role."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence

from pydantic import ValidationError

from rapidwire import __version__
from rapidwire.config import ConfigError, Settings, format_validation_error, get_settings, reset_settings_cache
from rapidwire.core.invariants import INVARIANTS_SUMMARY
from rapidwire.logging import configure_logging

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INVARIANT_VIOLATION = 4
EXIT_CONFIG = 78  # EX_CONFIG from sysexits.h

# Commands whose stdout is a report, so their log lines go to stderr instead; `bot --list-commands` is one too.
REPORT_COMMANDS: frozenset[str] = frozenset({"verify-invariants"})

LIST_COMMANDS_PASSWORD = "unused-by-list-commands"  # noqa: S105 - a placeholder, never sent to a database.

log = logging.getLogger("rapidwire.cli")


def _migrate(settings: Settings, args: argparse.Namespace) -> int:
    from rapidwire.db.migrate import upgrade

    log.info("applying migrations", extra={"revision": "head", "db_host": settings.db_host, "db_name": settings.db_name})
    upgrade(settings.database_url, "head")
    log.info("migrations applied")
    return EXIT_OK


def _api(settings: Settings, args: argparse.Namespace) -> int:
    import asyncio
    import signal
    import threading
    from collections.abc import Iterator
    from contextlib import contextmanager
    from types import FrameType

    import uvicorn

    from rapidwire.api.app import create_app

    class GracefulServer(uvicorn.Server):
        """A uvicorn server for which SIGINT and SIGTERM request a graceful shutdown and are not raised again afterwards, so the command returns its own exit code; a second SIGINT forces the exit."""

        @contextmanager
        def capture_signals(self) -> Iterator[None]:
            if threading.current_thread() is not threading.main_thread():
                yield
                return
            previous = {sig: signal.signal(sig, self.request_exit) for sig in (signal.SIGINT, signal.SIGTERM)}
            try:
                yield
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)

        def request_exit(self, sig: int, frame: FrameType | None) -> None:
            if self.should_exit and sig == signal.SIGINT:
                self.force_exit = True
            else:
                self.should_exit = True

    app = create_app(settings)
    # The API is reachable only through cloudflared on the compose network, so forwarded headers are trusted from any peer.
    config = uvicorn.Config(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
        proxy_headers=True,
        forwarded_allow_ips="*",
        server_header=False,
        lifespan="on",
    )
    server = GracefulServer(config)
    log.info("starting API", extra={"host": settings.api_host, "port": settings.api_port})
    asyncio.run(server.serve())
    if not server.started:
        log.error("API did not start")
        return EXIT_FAILURE
    log.info("API stopped")
    return EXIT_OK


def _bot(settings: Settings, args: argparse.Namespace) -> int:
    if args.list_commands:
        import asyncio

        from rapidwire.bot.app import build_bot, describe_commands

        bot = build_bot(settings)
        for line in describe_commands(bot.tree):
            print(line)
        asyncio.run(bot.context.engine.dispose())
        return EXIT_OK

    from rapidwire.bot.app import run_bot

    run_bot(settings)
    return EXIT_OK


def _worker(settings: Settings, args: argparse.Namespace) -> int:
    from rapidwire.worker.app import run_worker

    return run_worker(settings)


def _verify_invariants(settings: Settings, args: argparse.Namespace) -> int:
    import asyncio

    from rapidwire.core.invariants import InvariantReport, verify_invariants
    from rapidwire.db.engine import create_engine

    async def run() -> InvariantReport:
        engine = create_engine(settings.database_url)
        try:
            return await verify_invariants(engine)
        finally:
            await engine.dispose()

    report = asyncio.run(run())
    for item in report.currencies:
        state = "deleted" if item.deleted else "active"
        verdict = "ok" if item.ok else "VIOLATION"
        print(f"currency {item.currency_id} {item.symbol} ({state}): supply {item.supply}, balance sum {item.balance_sum}, staked {item.stake_total}: {verdict}")
    for entry in report.pools:
        state = "active" if entry.active else "dissolved"
        verdict = "ok" if entry.ok else "VIOLATION"
        print(
            f"pool {entry.pool_id} {entry.currency_a}/{entry.currency_b} fee {entry.fee_bps} bps ({state}): reserves {entry.reserve_a}/{entry.reserve_b},"
            f" custody {entry.custody_a}/{entry.custody_b}, shares {entry.total_shares} ({entry.locked_shares} locked, {entry.provider_shares} provider): {verdict}"
        )
    print(f"{len(report.currencies)} currencies checked, {len(report.pools)} pools checked, {len(report.violations)} violations")
    for violation in report.violations:
        print(f"violation [{violation.check}]: {violation.message}")
    log.info("invariants verified", extra={"currencies": len(report.currencies), "pools": len(report.pools), "violations": len(report.violations)})
    return EXIT_OK if report.ok else EXIT_INVARIANT_VIOLATION


def _backup(settings: Settings, args: argparse.Namespace) -> int:
    from rapidwire.backup.cli import run_backup_command

    return run_backup_command(settings, args)


HELP: dict[str, str] = {
    "bot": "Run the Discord bot.",
    "api": "Run the HTTP API and web explorer.",
    "worker": "Run scheduled jobs and the outbox dispatcher.",
    "migrate": "Apply database migrations up to the latest revision.",
    "backup": (
        "Take a database backup now under the backup job lease; the verify and restore subcommands check downloaded backups on the operator's machine without RapidWire settings."
        f" Without a subcommand it exits {EXIT_OK} when the backup was written, {EXIT_INVARIANT_VIOLATION} when it was written but its snapshot violates the ledger invariants, and {EXIT_FAILURE} when the lease is held elsewhere or the backup fails."
    ),
    "verify-invariants": (
        f"Check the ledger invariants and print a report: {INVARIANTS_SUMMARY}"
        f" Exits {EXIT_OK} when clean, {EXIT_INVARIANT_VIOLATION} when a violation is found and {EXIT_FAILURE} when the check itself fails."
    ),
}

COMMANDS: dict[str, Callable[[Settings, argparse.Namespace], int]] = {
    "api": _api,
    "backup": _backup,
    "bot": _bot,
    "migrate": _migrate,
    "verify-invariants": _verify_invariants,
    "worker": _worker,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rapidwire", description="RapidWire service entry point.")
    parser.add_argument("--version", action="version", version=f"rapidwire {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="command")
    for name, help_text in HELP.items():
        sub = subparsers.add_parser(name, help=help_text, description=help_text)
        if name == "bot":
            sub.add_argument(
                "--list-commands",
                action="store_true",
                help="Build the bot and its command tree without connecting, print one line per command with its parameters, and exit; needs neither a Discord token nor a database password.",
            )
        if name == "backup":
            from rapidwire.backup.cli import add_arguments

            add_arguments(sub)
    return parser


def _is_report(args: argparse.Namespace) -> bool:
    return args.command in REPORT_COMMANDS or (args.command == "bot" and bool(args.list_commands))


def _load_settings(args: argparse.Namespace) -> Settings:
    if args.command == "bot" and args.list_commands:
        # The listing opens no database connection, so a placeholder stands in for the password; every other setting is loaded and validated as usual.
        return Settings(db_password=LIST_COMMANDS_PASSWORD)
    return get_settings()


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, load and validate settings, configure logging, then run the chosen command and return its exit code."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE

    command: str = args.command
    if command == "backup":
        from rapidwire.backup.cli import is_operator_command, run_operator_command

        if is_operator_command(args):
            return run_operator_command(args)

    reset_settings_cache()
    try:
        settings = _load_settings(args)
    except ValidationError as exc:
        print(format_validation_error(exc), file=sys.stderr)
        return EXIT_CONFIG
    except ConfigError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    configure_logging(settings.log_level, settings.secret_values())
    if _is_report(args):
        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.StreamHandler):
                handler.setStream(sys.stderr)
    try:
        return COMMANDS[command](settings, args)
    except ConfigError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except Exception:
        log.exception("command failed", extra={"command": command})
        return EXIT_FAILURE
