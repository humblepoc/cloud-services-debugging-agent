"""CLI interface: one-shot mode and interactive REPL."""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import sys
from pathlib import Path
from typing import Any

# Force UTF-8 on Windows to avoid cp1252 crashes with emoji/unicode
if sys.platform == "win32" and hasattr(sys.stdout, "buffer") and "pytest" not in sys.modules:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from rich.console import Console
from rich.logging import RichHandler
from rich.markdown import Markdown

from agent.config import AgentConfig, apply_provider_defaults, load_config
from agent.core.context import ConversationContext
from agent.core.loop import run_agent
from agent.core.types import Message, Role
from agent.llm.registry import get_provider
from agent.setup import build_registry, get_system_prompt

console = Console(force_terminal=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent", description="LLM Agent CLI")
    p.add_argument("query", nargs="?", help="One-shot query (skip REPL if provided)")
    p.add_argument("-c", "--config", help="Path to config YAML")
    p.add_argument("-p", "--provider", help="LLM provider override")
    p.add_argument("-m", "--model", help="Model override")
    p.add_argument("-f", "--flavor", help="Flavor override")
    p.add_argument("--max-iterations", type=int, help="Max agent loop iterations")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    p.add_argument("--offline", action="store_true", help="Skip auto-update check on startup")
    p.add_argument("--list-models", action="store_true", help="List available models")
    return p


def apply_cli_overrides(cfg: AgentConfig, args: argparse.Namespace) -> None:
    if args.provider:
        cfg.llm.provider = args.provider
    if args.model:
        cfg.llm.model = args.model
    if args.flavor:
        cfg.flavor = args.flavor
    if args.max_iterations:
        cfg.max_iterations = args.max_iterations
    if args.verbose:
        cfg.log_level = "DEBUG"
    # Re-apply provider defaults after CLI overrides (covers -p custom)
    apply_provider_defaults(cfg.llm)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.WARNING),
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, show_time=False)],
        force=True,
    )
    if level.upper() != "DEBUG":
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
    else:
        logging.getLogger("httpx").setLevel(logging.NOTSET)
        logging.getLogger("httpcore").setLevel(logging.NOTSET)


async def _ensure_auth(provider: Any, cfg: AgentConfig) -> None:
    """Trigger auth eagerly for providers that need interactive setup (e.g. Copilot OAuth)."""
    if cfg.llm.provider == "copilot" and hasattr(provider, "_get_copilot_token"):
        await provider._get_copilot_token()


async def _list_models(cfg: AgentConfig) -> None:
    """List available models for the current provider."""
    provider = get_provider(cfg.llm.provider, cfg)
    if not hasattr(provider, "list_models"):
        console.print(f"[dim]--list-models is not available for the {cfg.llm.provider} provider[/dim]")
        return
    models = await provider.list_models()
    console.print(f"\n[bold]Available {cfg.llm.provider} Models[/bold] ({len(models)}):\n")
    for m in models:
        mid = m.get("id", "")
        vendor = m.get("vendor", "")
        category = m.get("model_picker_category", "")
        preview = " [dim][preview][/dim]" if m.get("preview") else ""
        current = " [green]<-- current[/green]" if mid == cfg.llm.model else ""
        console.print(f"  {mid:35s}  {vendor:12s}  {category}{preview}{current}")
    console.print()


async def run_one_shot(query: str, cfg: AgentConfig) -> None:
    """Run a single query and print the result."""
    provider = get_provider(cfg.llm.provider, cfg)
    await _ensure_auth(provider, cfg)
    registry = build_registry(cfg)
    context = ConversationContext(system_prompt=get_system_prompt(cfg))
    context.add(Message(role=Role.USER, content=query))

    console.print(f"[dim]Provider: {cfg.llm.provider} | Model: {cfg.llm.model}[/dim]")
    console.print()

    result = await run_agent(provider, registry, context, max_iterations=cfg.max_iterations)
    if result.content:
        console.print(Markdown(result.content))


def _handle_publish(cfg: AgentConfig, content: str, title_arg: str) -> None:
    """Handle /publish REPL command — publish last assistant response to Confluence."""
    if not content:
        console.print("[dim]Nothing to publish. Run an investigation first.[/dim]")
        return

    conf = cfg.confluence
    if not conf.user_email or not conf.api_token:
        console.print(
            "[red]Confluence credentials not configured.[/red]\n"
            "[dim]Set confluence.user_email and confluence.api_token in config.local.yaml.\n"
            "Generate a token at: https://id.atlassian.com/manage-profile/security/api-tokens[/dim]"
        )
        return

    # Get title from argument or prompt
    title = title_arg.strip()
    if not title:
        try:
            title = console.input("[bold]Page title: [/bold]").strip()
        except (EOFError, KeyboardInterrupt):
            return
    if not title:
        console.print("[dim]Publish cancelled — no title provided.[/dim]")
        return

    try:
        from agent.confluence import ConfluenceClient

        client = ConfluenceClient(conf.url, conf.user_email, conf.api_token)
        result = client.create_page(
            title=title,
            content_markdown=content,
            space_key=conf.space_key,
            parent_page_title=conf.parent_page_title,
            parent_page_id=conf.parent_page_id,
        )
        console.print(f"[green]Published![/green] {result['url']}")
    except Exception as exc:
        console.print(f"[red]Failed to publish: {exc}[/red]")


def _handle_email(cfg: AgentConfig, content: str, title_arg: str) -> None:
    """Handle /email REPL command — send last assistant response as email."""
    if not content:
        console.print("[dim]Nothing to send. Run an investigation first.[/dim]")
        return

    email_cfg = cfg.email
    if not email_cfg.client_id or not email_cfg.client_secret:
        console.print(
            "[red]Email credentials not configured.[/red]\n"
            "[dim]Set email.client_id and email.client_secret in config.local.yaml.[/dim]"
        )
        return

    if not email_cfg.notify_url or not email_cfg.token_url:
        console.print("[red]Email URLs not configured.[/red]")
        return

    if not email_cfg.admin_email:
        console.print("[red]No admin_email configured in email config.[/red]")
        return

    # Get title from argument or prompt
    title = title_arg.strip()
    if not title:
        try:
            title = console.input("[bold]Email subject: [/bold]").strip()
        except (EOFError, KeyboardInterrupt):
            return
    if not title:
        console.print("[dim]Email cancelled — no subject provided.[/dim]")
        return

    from datetime import datetime
    from agent.email_client import EmailClient, markdown_to_html

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    subject = f"{email_cfg.subject_prefix} {title} - {now}"
    # /email sends to admin only (no service context)
    recipients = [email_cfg.admin_email]
    html_body = markdown_to_html(content)

    try:
        client = EmailClient(
            notify_url=email_cfg.notify_url,
            token_url=email_cfg.token_url,
            client_id=email_cfg.client_id,
            client_secret=email_cfg.client_secret,
        )
        client.send_email(
            recipients=recipients,
            subject=subject,
            html_body=html_body,
            from_application=email_cfg.from_application,
            sender=email_cfg.sender,
        )
        console.print(f"[green]Email sent![/green] To: {', '.join(recipients)} | Subject: {subject}")
    except Exception as exc:
        console.print(f"[red]Failed to send email: {exc}[/red]")


async def run_repl(cfg: AgentConfig) -> None:
    """Run interactive REPL."""
    provider = get_provider(cfg.llm.provider, cfg)
    await _ensure_auth(provider, cfg)
    registry = build_registry(cfg)
    context = ConversationContext(system_prompt=get_system_prompt(cfg))
    last_assistant_content: str = ""

    try:
        from agent._build_info import BUILD_VERSION, CONTACT_ADMIN as _contact
        ver = BUILD_VERSION
    except ImportError:
        ver = "dev"
        _contact = "maintainer@example.com"
    console.print(f"[bold]devops-agent[/bold] v{ver} ({cfg.llm.provider}/{cfg.llm.model})")
    console.print(f"[dim]For feedback or issues, contact: {_contact}[/dim]")
    console.print()

    while True:
        try:
            user_input = console.input("[bold green]> [/bold green]").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not user_input:
            continue

        # REPL commands
        if user_input.startswith("/"):
            cmd_parts = user_input.split(maxsplit=1)
            cmd = cmd_parts[0].lower()

            if cmd == "/quit":
                break
            elif cmd == "/clear":
                context.clear()
                last_assistant_content = ""
                console.print("[dim]Context cleared.[/dim]")
                continue
            elif cmd == "/tools":
                for t in registry.list():
                    s = t.schema()
                    console.print(f"  [bold]{s.name}[/bold] — {s.description[:80]}")
                continue
            elif cmd == "/model" and len(cmd_parts) > 1:
                cfg.llm.model = cmd_parts[1]
                provider = get_provider(cfg.llm.provider, cfg)
                console.print(f"[dim]Model set to {cfg.llm.model}[/dim]")
                continue
            elif cmd == "/provider" and len(cmd_parts) > 1:
                cfg.llm.provider = cmd_parts[1]
                provider = get_provider(cfg.llm.provider, cfg)
                console.print(f"[dim]Provider set to {cfg.llm.provider}[/dim]")
                continue
            elif cmd == "/models":
                if hasattr(provider, "list_models"):
                    models = await provider.list_models()
                    for m in models:
                        mid = m.get("id", "")
                        vendor = m.get("vendor", "")
                        category = m.get("model_picker_category", "")
                        current = " [green]<-- current[/green]" if mid == cfg.llm.model else ""
                        console.print(f"  [bold]{mid}[/bold]  {vendor:12s}  {category}{current}")
                else:
                    console.print(f"[dim]/models is not available for the {cfg.llm.provider} provider[/dim]")
                continue
            elif cmd == "/publish":
                _handle_publish(cfg, last_assistant_content, cmd_parts[1] if len(cmd_parts) > 1 else "")
                continue
            elif cmd == "/email":
                _handle_email(cfg, last_assistant_content, cmd_parts[1] if len(cmd_parts) > 1 else "")
                continue
            else:
                console.print(f"[dim]Unknown command: {cmd}[/dim]")
                continue

        context.add(Message(role=Role.USER, content=user_input))
        result = await run_agent(provider, registry, context, max_iterations=cfg.max_iterations)

        if result.content:
            last_assistant_content = result.content
            console.print()
            console.print(Markdown(result.content))
            console.print()


def main() -> None:
    try:
        from agent._build_info import CONTACT_ADMIN
    except ImportError:
        CONTACT_ADMIN = "maintainer@example.com"

    try:
        _main_inner(CONTACT_ADMIN)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        console.print(f"[red]Unexpected error: {exc}[/red]")
        console.print(f"[red]Contact admin: {CONTACT_ADMIN}[/red]")
        sys.exit(1)


def _main_inner(contact_admin: str) -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Auto-update check (exe-only — dev/source runs skip this)
    if getattr(sys, "frozen", False) and not args.offline:
        from agent.updater import check_and_update
        install_dir = Path(sys.executable).parent.parent
        check_and_update(install_dir)
    cfg = load_config(args.config)
    apply_cli_overrides(cfg, args)
    setup_logging(cfg.log_level)

    if args.list_models:
        asyncio.run(_list_models(cfg))
        sys.exit(0)

    _has_key = cfg.llm.api_key or (
        cfg.llm.provider == "custom" and cfg.llm.custom_api_key
    )
    if not _has_key and cfg.llm.provider != "copilot":
        console.print("[red]Error: No API key. Set AGENT_API_KEY or configure in YAML.[/red]")
        console.print(f"[red]Contact admin: {contact_admin}[/red]")
        sys.exit(1)

    if args.query:
        asyncio.run(run_one_shot(args.query, cfg))
    else:
        asyncio.run(run_repl(cfg))
