"""
Graphics Module

This module handles all visual rendering tasks. It formats and displays data
(e.g., job evaluations, configuration flags, live status, JSON output) in a
structured, visually appealing way using Rich.
"""

import json
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text

console = Console()

def render_evaluation(updated: dict, recency: float, app_count: int) -> None:
    """
    Render job evaluation details as a Rich table and panel.
    """
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Metric", style="cyan", justify="left")
    table.add_column("Value", style="green", justify="right")

    # Extract metrics
    prelim = int(round(updated.get("preliminary_score", 0.0)))
    skills = int(round(updated.get("skills_match", 0)))
    model_fit = int(round(updated.get("model_fit_score", 0)))
    success_prob = int(round(updated.get("success_probability", 50)))
    exp_gap = int(round(updated.get("experience_gap", 0)))
    crit_penalty = int(round(updated.get("critical_skill_mismatch_penalty", 0)))
    role_complex = int(round(updated.get("role_complexity", 0)))
    effort_days = int(round(updated.get("effort_days_to_fit", 0)))
    recency_val = int(round(recency))
    applicants = int(round(app_count))

    # Helper function to create an ASCII block for a given value.
    def ascii_block(value: int) -> str:
        if value >= 75:
            return "█"
        elif value >= 50:
            return "▓"
        elif value >= 25:
            return "▒"
        return "░"

    def add_metric(label: str, value: int, reverse: bool = False) -> None:
        block = ascii_block(value)
        table.add_row(label, f"{block} {value}")

    add_metric("🏆 Preliminary Score", prelim)
    add_metric("✅ Skills Match", skills)
    add_metric("📊 Model Fit", model_fit)
    add_metric("🎯 Success Prob.", success_prob)
    add_metric("⚠️ Experience Gap", exp_gap, reverse=True)
    add_metric("🚫 Crit. Penalty", crit_penalty, reverse=True)
    add_metric("🤔 Role Complexity", role_complex, reverse=True)
    add_metric("⏳ Effort Days", effort_days, reverse=True)
    add_metric("🕒 Recency", recency_val)
    add_metric("👥 Applicants", applicants, reverse=True)

    details = (
        f"💼 [bold blue]{updated.get('title', 'N/A')}[/bold blue] @ [bold green]{updated.get('company', 'N/A')}[/bold green]\n\n"
        f"📝 [bold yellow]Reasoning:[/bold yellow]\n{updated.get('reasoning', 'No reasoning provided.')}\n\n"
        f"🛠️ [bold yellow]Development Areas:[/bold yellow]\n{updated.get('areas_for_development', 'None specified.')}"
    )
    panel = Panel(details, title="[bold blue]Job Details[/bold blue]", border_style="bright_blue")
    console.print(Columns([table, panel], equal=True, expand=True))

def render_live_status(status: dict) -> None:
    """
    Render a live status panel.
    """
    text = Text.assemble(
        ("🚀 Processing Jobs...\n", "bold green"),
        (f"Jobs Processed: {status.get('jobs_processed', 0)}", "bold yellow")
    )
    panel = Panel(text, title="[bold blue]Live Status[/bold blue]", border_style="bright_green")
    console.print(panel)

def render_json(data: dict) -> None:
    """
    Render JSON data with syntax highlighting.
    """
    json_text = json.dumps(data, indent=2)
    console.print(f"[bold blue]JSON Output:[/bold blue]\n[cyan]{json_text}[/cyan]")

def render_config_flags(config: dict, args: any) -> None:
    """
    Render configuration flags in a table.
    """
    flags = {
        "Manual Login": args.manual_login,
        "Debug Mode": args.debug_single or config.get("debug", {}).get("enabled", False),
        "Headless": config.get("headless", False),
        "Scraping Mode": config.get("scraping", {}).get("mode", "normal"),
        "Verbose": args.verbose or config.get("debug", {}).get("verbose", False),
    }
    table = Table(show_header=True, header_style="bold magenta", expand=True, pad_edge=True)
    table.add_column("Configuration Flag", style="cyan", justify="left")
    table.add_column("Value", style="green", justify="right")
    for key, value in flags.items():
        mark = "[green]✅[/green]" if value else "[red]❌[/red]"
        table.add_row(key, f"{value} {mark}")
    panel = Panel(table, title="[bold blue]Configuration Flags[/bold blue]", border_style="bright_blue")
    console.print(panel)