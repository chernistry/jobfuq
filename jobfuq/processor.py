#!/usr/bin/env python3
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
from .database import (
    create_connection,
    get_jobs_for_scoring,
    update_job_scores,
)
from .logger import logger, set_verbose

console = Console()
retry_map = {}

def calculate_recency_score(job_date, max_days=60):
    try:
        d = (datetime.now() - datetime.strptime(job_date, '%Y-%m-%d')).days
        if d < 0:
            d = 0
        if d > max_days:
            return 0.0
        # Scale: 0 (old) to 100 (fresh)
        return max(0.0, min(100.0, 100.0 * (1.0 - d / float(max_days))))
    except Exception as e:
        logger.error(f"Error calculating recency score: {e}")
        return 0.0

def calculate_company_size_score(sz):
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

def softened_competition_penalty(cnt):
    try:
        c = min(cnt, 500)
        return max(0.0, 100.0 - (c * 0.2))
    except Exception:
        return 50.0

def calculate_preliminary_score(e: Dict[str, float], rec: float, applicants: int, csize: float) -> float:
    sm = e.get('skills_match', 0.0)
    xp_gap = e.get('experience_gap', 0.0)
    mod_fit = e.get('model_fit_score', 0.0)
    sp = e.get('success_probability', 50.0)
    cpen = e.get('critical_skill_mismatch_penalty', 0.0)
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

def display_evaluation(updated: Dict[str, Any], recency: float, app_count: int) -> None:
    # Left column: Key Metrics with emojis
    metrics_table = Table(title="[bold blue]Metrics[/bold blue]", show_header=True, header_style="bold magenta")
    metrics_table.add_column("Metric", style="cyan")
    metrics_table.add_column("Value", style="green")
    metrics_table.add_row("🏆 Preliminary Score", f"{updated.get('preliminary_score', 0.0):.1f}")
    metrics_table.add_row("✅ Skills Match", f"{updated.get('skills_match', 0.0):.1f}")
    metrics_table.add_row("📊 Model Fit", f"{updated.get('model_fit_score', 0.0):.1f}")
    metrics_table.add_row("🎯 Success Prob.", f"{updated.get('success_probability', 50.0):.1f}")
    metrics_table.add_row("⚠️ Experience Gap", f"{updated.get('experience_gap', 0.0):.1f}")
    metrics_table.add_row("🚫 Crit. Penalty", f"{updated.get('critical_skill_mismatch_penalty', 0.0):.1f}")
    metrics_table.add_row("🤔 Role Complexity", f"{updated.get('role_complexity', 0.0):.1f}")
    metrics_table.add_row("⏳ Effort Days", f"{updated.get('effort_days_to_fit', 0.0):.1f}")
    metrics_table.add_row("🕒 Recency", f"{recency:.1f}")
    metrics_table.add_row("👥 Applicants", str(app_count))

    # Right column: Job Details
    title = updated.get('title', 'N/A')
    company = updated.get('company', 'N/A')
    reasoning = updated.get('reasoning', 'No reasoning provided.')
    dev_areas = updated.get('areas_for_development', 'None specified.')
    details = (
        f"[bold blue]💼 {title}[/bold blue] @ [bold green]{company}[/bold green]\n\n"
        f"[bold yellow]📝 Reasoning:[/bold yellow]\n{reasoning}\n\n"
        f"[bold yellow]🛠️ Development Areas:[/bold yellow]\n{dev_areas}"
    )
    details_panel = Panel(details, title="Job Details", border_style="bright_blue")

    # Display side-by-side columns
    cols = Columns([metrics_table, details_panel])
    console.print(cols)

async def evaluate_and_update_job(job: Dict[str, Any], model: AIModel, conn, verbose: bool):
    try:
        recency = calculate_recency_score(job['date'])
        app_count = job.get('applicants_count') or 0
        ev = await evaluate_job(model, job)
        if not ev:
            logger.warning(f"⚠️ No evaluation for job {job['id']}")
            retry_map[job['id']] = time.time() + 180
            return
        sm = ev.get('skills_match', 0.0)
        mf = ev.get('model_fit_score', 0.0)
        reasoning = ev.get('reasoning', '')
        if sm == 0 and mf == 0 and not reasoning:
            logger.warning(f"⚠️ Job {job['id']} has zero extraction; scheduling retry.")
            retry_map[job['id']] = time.time() + 180
            return
        final_score = calculate_preliminary_score(ev, recency, app_count, calculate_company_size_score(job.get('company_size_score', 0)))
        updated = {**job, **ev, 'preliminary_score': final_score}
        update_job_scores(conn, job['id'], updated)
        logger.info(f"✅ Job {job['id']} updated: Preliminary Score = {final_score:.2f}")
        if verbose:
            display_evaluation(updated, recency, app_count)
    except Exception as ex:
        logger.error(f"❌ Error processing job {job.get('id', 'Unknown')}: {ex}")
        if job['id'] not in retry_map:
            retry_map[job['id']] = time.time() + 180

async def process_and_rank_jobs(conf, verbose, threads):
    conn = create_connection(conf)
    provider_manager = ProviderManager(conf)
    mode = conf.get('ai_providers', {}).get('provider_mode', 'together').lower()
    if mode == 'together':
        key = conf.get('ai_providers', {}).get('together_api_key', '').strip()
        keys = [key] if key else []
    elif mode == 'openrouter':
        keys = conf.get('ai_providers', {}).get('openrouter_api_keys', [])
    elif mode == 'multi':
        keys = conf.get('ai_providers', {}).get('openrouter_api_keys', [])
        together_key = conf.get('ai_providers', {}).get('together_api_key', '').strip()
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
        if mode == 'together':
            sc['ai_providers']['together_api_key'] = keys[i % len(keys)]
        elif mode == 'openrouter':
            sc['ai_providers']['openrouter_api_keys'] = [keys[i % len(keys)]]
        elif mode == 'multi':
            sc['ai_providers']['openrouter_api_keys'] = [keys[i % len(keys)]]
            sc['ai_providers']['together_api_key'] = keys[i % len(keys)]
        m = AIModel(sc, provider_manager)
        models.append(m)
    while True:
        try:
            raw_jobs = get_jobs_for_scoring(conn, limit=threads)
            now_ts = time.time()
            jobs = []
            for j in raw_jobs:
                if j['id'] in retry_map:
                    if now_ts < retry_map[j['id']]:
                        continue
                    else:
                        del retry_map[j['id']]
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

async def main(config_path, verbose, endless, threads):
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

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Process & rank jobs with enhanced UI output')
    parser.add_argument('config', nargs='?', default='jobfuq/conf/config.toml', help='Path to config file')
    parser.add_argument('-v', '--verbose', action='store_true', help='Increase output verbosity')
    parser.add_argument('--endless', action='store_true', help='Run in endless mode (loop forever)')
    parser.add_argument('--threads', type=int, default=1, help='Number of concurrent evaluations')
    args = parser.parse_args()
    asyncio.run(main(args.config, args.verbose, args.endless, args.threads))
