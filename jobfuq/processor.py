#!/usr/bin/env python3
"""
Job Processing & Ranking Script

This script processes and ranks job evaluations with an ASCII gradient
display and color output using Rich.
"""

import asyncio
import argparse
import math
import sys
import time
from datetime import datetime
from typing import Any, Dict

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns

from .utils import load_config
from .llm_handler import AIModel, evaluate_job, ProviderManager
from .database import create_connection, get_jobs_for_scoring, update_job_scores
from .logger import logger, set_verbose



# ==== GLOBAL CONSTANTS & UTILITY FUNCTIONS ==== #

console: Console = Console()
retry_map: Dict[Any, float] = {}

# Define a 10-level gradient from red to green.
COLOR_GRADIENT = [
    "#FF0000", "#FF3300", "#FF6600", "#FF9900", "#FFCC00",
    "#FFFF00", "#CCFF00", "#99FF00", "#66FF00", "#33FF00"
]


def ascii_block(value: int) -> str:
    """
    Return an ASCII block character based on the given value.

    Args:
        value (int): The numeric value to map.

    Returns:
        str: An ASCII block character.
    """
    if value <= 25:
        return "░"
    elif value <= 50:
        return "▒"
    elif value <= 75:
        return "▓"
    else:
        return "█"



def color_for_value(value: int, reverse: bool = False) -> str:
    """
    Choose a color from the gradient for the given value.

    If reverse is True, a high value is bad (red); otherwise, high is good (green).

    Args:
        value (int): The metric value.
        reverse (bool, optional): Reverse gradient if high is bad. Defaults to False.

    Returns:
        str: A hex color string.
    """
    if reverse:
        idx = min((100 - value) // 10, 9)
    else:
        idx = min(value // 10, 9)
    return COLOR_GRADIENT[idx]



def format_metric_as_block(value: int, reverse: bool = False) -> str:
    """
    Format a numeric metric as a colored block with fixed width.

    Args:
        value (int): The metric value.
        reverse (bool, optional): Reverse color gradient if high is bad. Defaults to False.

    Returns:
        str: A string with colored ASCII block and formatted value.
    """
    block = ascii_block(value)
    color = color_for_value(value, reverse=reverse)
    # Fixed width: block in 2 chars, value as a 3-digit integer.
    return f"[{color}]{block:2s} {value:3d}[/]"



# ==== SCORE CALCULATION FUNCTIONS ==== #

def calculate_recency_score(job_date: str, max_days: int = 60) -> float:
    """
    Calculate a recency score (0-100) based on how recent the job date is.

    Args:
        job_date (str): The job's date in "%Y-%m-%d" format.
        max_days (int, optional): Maximum days to consider for recency. Defaults to 60.

    Returns:
        float: A recency score between 0.0 and 100.0.
    """
    try:
        d = (datetime.now() - datetime.strptime(job_date, "%Y-%m-%d")).days
        if d < 0:
            d = 0
        if d > max_days:
            return 0.0
        return max(0.0, min(100.0, 100.0 * (1.0 - d / float(max_days))))
    except Exception as e:
        logger.error(f"Error calculating recency score: {e}")
        return 0.0



def calculate_company_size_score(sz: Any) -> float:
    """
    Calculate a score (0-100) based on the company's size.

    Args:
        sz (Any): The company size metric.

    Returns:
        float: A score between 0.0 and 100.0.
    """
    try:
        if 3 <= sz <= 5:
            return 100.0
        if sz == 6:
            return 80.0
        if sz == 7:
            return 60.0
        if sz >= 8:
            return 40.0
        return max(0.0, 50.0 - (sz * 5))
    except Exception:
        return 0.0



def softened_competition_penalty(cnt: Any) -> float:
    """
    Calculate a softened penalty for competition based on applicant count.

    Args:
        cnt (Any): The applicant count.

    Returns:
        float: A penalty score.
    """
    try:
        c = min(cnt, 500)
        return max(0.0, 100.0 - (c * 0.2))
    except Exception:
        return 50.0



def calculate_preliminary_score(
        e: Dict[str, float], rec: float, applicants: int, csize: float
) -> float:
    """
    Calculate the preliminary job score using various evaluation metrics.

    Args:
        e (Dict[str, float]): Evaluation metrics dictionary.
        rec (float): Recency score.
        applicants (int): Number of applicants.
        csize (float): Company size score.

    Returns:
        float: The preliminary score (0-100).
    """
    sm = e.get("skills_match", 0.0)
    xp_gap = e.get("experience_gap", 0.0)
    mod_fit = e.get("model_fit_score", 0.0)
    sp = e.get("success_probability", 50.0)
    cpen = e.get("critical_skill_mismatch_penalty", 0.0)

    penalty = min(80.0, xp_gap + cpen)
    base = sm - penalty
    if base < 1.0:
        base = 1.0

    score = base * (sp / 100.0)
    comp_factor = softened_competition_penalty(applicants) / 100.0
    score *= comp_factor

    rec_adj = (rec - 50.0) * 0.2
    csize_adj = (csize - 50.0) * 0.1
    score += rec_adj + csize_adj

    return max(0.0, min(score, 100.0))



# ==== DISPLAY & VISUALIZATION FUNCTIONS ==== #

def display_evaluation(
        updated: Dict[str, Any], recency: float, app_count: int
) -> None:
    """
    Display the evaluation metrics and job details using Rich tables and panels.

    Args:
        updated (Dict[str, Any]): Job evaluation details.
        recency (float): Recency score.
        app_count (int): Applicant count.
    """
    metrics_table = Table(show_header=True, header_style="bold magenta")
    metrics_table.add_column("Metric", style="cyan", justify="left")
    metrics_table.add_column("Value", style="green", justify="right")

    # Convert values to integers.
    prelim = int(round(updated.get("preliminary_score", 0.0)))
    sm = int(round(updated.get("skills_match", 0)))
    mf = int(round(updated.get("model_fit_score", 0)))
    sp = int(round(updated.get("success_probability", 50)))
    xp_gap = int(round(updated.get("experience_gap", 0)))
    cp = int(round(updated.get("critical_skill_mismatch_penalty", 0)))
    rc = int(round(updated.get("role_complexity", 0)))
    ed = int(round(updated.get("effort_days_to_fit", 0)))
    rec_i = int(round(recency))
    app_i = int(round(app_count))

    metrics_table.title = "[bold blue]Results[/bold blue]"

    # Positive metrics: higher is better. Negative metrics: reverse the gradient.
    metrics_table.add_row("🏆 Preliminary Score", format_metric_as_block(prelim))
    metrics_table.add_row("✅ Skills Match", format_metric_as_block(sm))
    metrics_table.add_row("📊 Model Fit", format_metric_as_block(mf))
    metrics_table.add_row("🎯 Success Prob.", format_metric_as_block(sp))
    metrics_table.add_row("⚠️ Experience Gap", format_metric_as_block(xp_gap, reverse=True))
    metrics_table.add_row("🚫 Crit. Penalty", format_metric_as_block(cp, reverse=True))
    metrics_table.add_row("🤔 Role Complexity", format_metric_as_block(rc, reverse=True))
    metrics_table.add_row("⏳ Effort Days", format_metric_as_block(ed, reverse=True))
    metrics_table.add_row("🕒 Recency", format_metric_as_block(rec_i))
    metrics_table.add_row("👥 Applicants", format_metric_as_block(app_i, reverse=True))

    title_str = updated.get("title", "N/A")
    company_str = updated.get("company", "N/A")
    reasoning_str = updated.get("reasoning", "No reasoning provided.")
    dev_areas_str = updated.get("areas_for_development", "None specified.")

    details = (
        f"💼 [bold blue]{title_str}[/bold blue] @ [bold green]{company_str}[/bold green]\n\n"
        f"📝 [bold yellow]Reasoning:[/bold yellow]\n{reasoning_str}\n\n"
        f"🛠️ [bold yellow]Development Areas:[/bold yellow]\n{dev_areas_str}"
    )
    details_panel = Panel(details, title="Job Details", border_style="bright_blue")
    cols = Columns([metrics_table, details_panel], equal=True, expand=True)
    console.print(cols)



# ==== JOB EVALUATION & UPDATE FUNCTIONS ==== #

async def evaluate_and_update_job(
        job: Dict[str, Any], model: AIModel, conn: Any, verbose: bool
) -> None:
    """
    Evaluate a job using the AI model and update its scores in the database.

    Args:
        job (Dict[str, Any]): The job details.
        model (AIModel): The AI model instance.
        conn (Any): The database connection.
        verbose (bool): Whether to display evaluation output.
    """
    try:
        recency = calculate_recency_score(job["date"])
        app_count = job.get("applicants_count") or 0

        ev = await evaluate_job(model, job)
        if not ev:
            logger.warning(f"⚠️ No evaluation for job {job['id']}")
            retry_map[job["id"]] = time.time() + 180
            return

        final_score = calculate_preliminary_score(
            ev,
            recency,
            app_count,
            calculate_company_size_score(job.get("company_size_score", 0)),
        )
        final_score_int = int(round(final_score))
        updated = {**job, **ev, "preliminary_score": final_score_int}

        update_job_scores(conn, job["id"], updated)
        logger.info(f"✅ Job {job['id']} updated: Preliminary Score = {final_score_int}")

        if verbose:
            display_evaluation(updated, recency, app_count)

    except Exception as ex:
        logger.error(f"❌ Error processing job {job.get('id', 'Unknown')}: {ex}")
        if job["id"] not in retry_map:
            retry_map[job["id"]] = time.time() + 180



# ==== MAIN PROCESSING LOOP ==== #

async def process_and_rank_jobs(conf: Dict[str, Any], verbose: bool, threads: int) -> None:
    """
    Process and rank jobs using concurrent evaluations.

    Args:
        conf (Dict[str, Any]): Configuration dictionary.
        verbose (bool): Verbosity flag.
        threads (int): Number of concurrent evaluations.
    """
    conn = create_connection(conf)
    provider_manager = ProviderManager(conf)

    mode = conf.get("ai_providers", {}).get("provider_mode", "together").lower()
    if mode == "together":
        key = conf.get("ai_providers", {}).get("together_api_key", "").strip()
        keys = [key] if key else []
    elif mode == "openrouter":
        keys = conf.get("ai_providers", {}).get("openrouter_api_keys", [])
    elif mode == "multi":
        keys = conf.get("ai_providers", {}).get("openrouter_api_keys", [])
        together_key = conf.get("ai_providers", {}).get("together_api_key", "").strip()
        if together_key:
            keys.append(together_key)
    else:
        keys = []

    if not keys:
        logger.error("❌ No AI keys found. Exiting.")
        sys.exit(1)

    models = []
    for i in range(threads):
        sc = dict(conf)
        if mode == "together":
            sc["ai_providers"]["together_api_key"] = keys[i % len(keys)]
        elif mode == "openrouter":
            sc["ai_providers"]["openrouter_api_keys"] = [keys[i % len(keys)]]
        elif mode == "multi":
            sc["ai_providers"]["openrouter_api_keys"] = [keys[i % len(keys)]]
            sc["ai_providers"]["together_api_key"] = keys[i % len(keys)]
        m = AIModel(sc, provider_manager)
        models.append(m)

    while True:
        try:
            raw_jobs = get_jobs_for_scoring(conn, limit=threads)
            now_ts = time.time()
            jobs = []
            for j in raw_jobs:
                if j["id"] in retry_map:
                    if now_ts < retry_map[j["id"]]:
                        continue
                    else:
                        del retry_map[j["id"]]
                jobs.append(j)

            logger.info(f"📥 Retrieved {len(jobs)} jobs for scoring")

            if not jobs:
                console.print("[yellow]No new jobs found. Waiting for 30 seconds...[/yellow]")
                await asyncio.sleep(30)
                continue

            tasks = []
            for i, jb in enumerate(jobs):
                tasks.append(evaluate_and_update_job(jb, models[i % threads], conn, verbose))
            await asyncio.gather(*tasks)
            await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Main loop error: {e}")
            await asyncio.sleep(5)



# ==== MAIN ENTRY POINT ==== #

async def main(config_path: str, verbose: bool, endless: bool, threads: int) -> None:
    """
    Main entry point to start the job processing pipeline.

    Args:
        config_path (str): Path to the configuration file.
        verbose (bool): Verbosity flag.
        endless (bool): If True, run in endless mode.
        threads (int): Number of concurrent evaluations.
    """
    console.print("[bold blue]🚀 Starting Processor[/bold blue]")
    try:
        conf = load_config(config_path)
        set_verbose(verbose)

        if not endless:
            await process_and_rank_jobs(conf, verbose, threads)
        else:
            while True:
                await process_and_rank_jobs(conf, verbose, threads)
                logger.info("🔄 Waiting 60 seconds before next pass...")
                await asyncio.sleep(60)

    except Exception as e:
        logger.error(f"Fatal error in main: {e}")
        sys.exit(1)



if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Process & rank jobs with ASCII gradient + color output"
    )
    parser.add_argument(
        "config",
        nargs="?",
        default="jobfuq/conf/config.toml",
        help="Path to config file",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Increase output verbosity")
    parser.add_argument("--endless", action="store_true", help="Run in endless mode (loop forever)")
    parser.add_argument("--threads", type=int, default=1, help="Number of concurrent evaluations")
    args = parser.parse_args()

    asyncio.run(main(args.config, args.verbose, args.endless, args.threads))