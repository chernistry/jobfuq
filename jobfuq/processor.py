#!/usr/bin/env python3
"""
Process & rank jobs with concurrency, skipping 0.0 extractions & retrying later.

This module defines the main job ranking pipeline, orchestrating:
  - Database retrieval of unscored jobs
  - Concurrent AI evaluation (via AIModel)
  - Final scoring & database updates
  - Automatic retries for extraction failures
"""

import asyncio
import argparse
import math
import sys
import time
from datetime import datetime
from typing import List, Dict, Any

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from .utils import load_config
from .llm_handler import AIModel, evaluate_job, ProviderManager
from .database import (
    create_connection, get_jobs_for_scoring, update_job_scores, add_fit_score_columns
)
from .logger import logger, set_verbose

console = Console()

# Load configuration
config = load_config("jobfuq/conf/config.toml")

# Global in-memory map tracking jobs with extraction failures (job_id -> next eligible timestamp)
retry_map: Dict[int, float] = {}


def calculate_recency_score(job_date: str, max_days: int = 60) -> float:
    try:
        days_diff = (datetime.now() - datetime.strptime(job_date, '%Y-%m-%d')).days
        if days_diff > max_days:
            return 0.0
        return 1.0 / (math.log(days_diff + 1) + 0.5)
    except Exception as e:
        logger.error(f"Error calculating recency score: {e}")
        return 0.0


def calculate_company_size_score(size_score: int) -> float:
    try:
        if 3 <= size_score <= 5:
            return 1.0
        elif size_score == 6:
            return 0.9
        elif size_score == 7:
            return 0.7
        elif size_score >= 8:
            return 0.5
        return max(0.6 + (size_score * 0.1), 0.0)
    except Exception:
        return 0.0


def softened_competition_penalty(applicant_count: int) -> float:
    base = 1.0 / (math.log(applicant_count + 3) + 1)
    return 0.7 + 0.3 * base


def calculate_final_score(
        evaluation: Dict[str, Any],
        recency_score: float,
        applicant_count: int,
        company_size_score: float
) -> float:
    skills_match = evaluation.get('skills_match', 0.0)
    resume_similarity = evaluation.get('resume_similarity', 0.0)
    success_probability = evaluation.get('success_probability', 0.8)
    confidence = evaluation.get('confidence', 0.8)
    effort_days_to_fit = evaluation.get('effort_days_to_fit', 4.0)
    critical_penalty = max(evaluation.get('critical_skill_mismatch_penalty', 0.0), 0.0)

    initial_fit = (skills_match * 0.6) + (resume_similarity * 0.4)
    base = initial_fit * success_probability * confidence

    penalty_factor = 0.2 * (1.0 - 0.5 * success_probability)
    if penalty_factor < 0.05:
        penalty_factor = 0.05
    base -= (critical_penalty * penalty_factor)

    upskill_mult = max(0.90, 1.0 - math.log(effort_days_to_fit + 1) * 0.03)
    base *= upskill_mult

    conf_adjust = 1.0 - ((1.0 - confidence) ** 2 * 0.1)
    base *= conf_adjust

    comp_factor = softened_competition_penalty(applicant_count) * (1.0 + success_probability * 0.2)
    base *= comp_factor

    rec_adj = (recency_score - 0.5) * 0.1
    csize_adj = (company_size_score - 0.5) * 0.1
    base += (rec_adj + csize_adj)

    scaled = base * 1.3
    if scaled <= 0.7:
        final = scaled
    else:
        extra = scaled - 0.7
        damped_extra = 0.3 * (1 - math.exp(-extra * 3))
        final = 0.7 + damped_extra

    ease_factor = 1.0 + ((30 - effort_days_to_fit) / 300)
    final *= ease_factor
    return max(min(final, 1.0), 0.01)


async def evaluate_and_update_job(
        job: Dict[str, Any],
        ai_model: AIModel,
        conn,
        verbose: bool
) -> None:
    try:
        recency = calculate_recency_score(job['date'])
        applicant_count = job.get('applicants_count') or 0

        evaluation = await evaluate_job(ai_model, job)
        if not evaluation:
            logger.warning(f"No evaluation returned for job {job['id']}. Skipping.")
            retry_map[job['id']] = time.time() + 180
            return

        sm = evaluation.get('skills_match', 0.0)
        rs = evaluation.get('resume_similarity', 0.0)
        ffs = evaluation.get('final_fit_score', 0.0)
        reasoning = evaluation.get('reasoning', '').strip()
        dev_areas = evaluation.get('areas_for_development', '').strip()

        logger.debug(f"Job {job['id']} Eval: skills_match={sm}, resume_similarity={rs}, final_fit_score={ffs}")
        logger.debug(f"Job {job['id']} Reasoning: {reasoning}")
        logger.debug(f"Job {job['id']} Dev Areas: {dev_areas}")

        if (sm == 0.0 and rs == 0.0 and ffs == 0.0):
            if not reasoning or "extraction failed" in reasoning.lower() or "error during evaluation" in dev_areas.lower():
                logger.warning(f"Job {job['id']}: zero numeric & no valid reasoning => retry in 3 min.")
                retry_map[job['id']] = time.time() + 180
                return

        company_size_val = calculate_company_size_score(job.get('company_size_score', 0))
        final_score = calculate_final_score(evaluation, recency, applicant_count, company_size_val)
        ranked_job = {**job, **evaluation, 'final_score': final_score}
        update_job_scores(conn, job['id'], ranked_job)
        logger.info(f"Job {job['id']} updated with final score: {final_score:.2f}")

        if verbose:
            table = Table(title=f"Evaluation: {ranked_job['title']} @ {ranked_job['company']}")
            table.add_column("Metric", style="bold")
            table.add_column("Value", style="cyan")
            metrics = {
                "📍Overall Score": ranked_job.get('final_score', 0.0),
                "Success Probability": ranked_job.get('success_probability', 0.8),
                "Confidence": ranked_job.get('confidence', 0.8),
                "Fit Score": ranked_job.get('final_fit_score', 0.0),
                "Skills Match": ranked_job.get('skills_match', 0.0),
                "Resume Similarity": ranked_job.get('resume_similarity', 0.0),
                "Effort Days to Fit": ranked_job.get('effort_days_to_fit', 4),
            }
            for key, val in metrics.items():
                table.add_row(key, f"{val:.2f}" if val is not None else "N/A")

            if 'query_token_count' in evaluation and 'response_token_count' in evaluation:
                table.add_row("Query Tokens", str(evaluation['query_token_count']))
                table.add_row("Response Tokens", str(evaluation['response_token_count']))

            table.add_row("Recency Score", f"{recency:.2f}")
            table.add_row("Company Size Score", f"{company_size_val:.2f}")
            table.add_row("Applicant Competition", f"{applicant_count}")

            panel = Panel(table, title="Detailed Evaluation", expand=False)
            console.print(panel)

    except Exception as ex:
        logger.error(f"Error updating job {job.get('id', 0)}: {ex}")
        if job['id'] not in retry_map:
            retry_map[job['id']] = time.time() + 180


async def process_and_rank_jobs(
        config: Dict[str, Any],
        verbose: bool,
        threads: int
) -> None:
    conn = create_connection(config)
    add_fit_score_columns(conn)

    provider_manager = ProviderManager(config)
    # Force default provider_mode to "together" if not set.
    provider_mode = (config.get("ai_providers", {}).get("provider_mode", "together")).lower()
    logger.debug(f"Provider mode from config: {provider_mode}")

    if provider_mode == "together":
        # Use .strip() in case there are any extraneous spaces.
        together_key = config.get("ai_providers", {}).get("together_api_key", "").strip()
        keys = [together_key] if together_key else []
    elif provider_mode == "openrouter":
        keys = config.get("ai_providers", {}).get("openrouter_api_keys", [])
    elif provider_mode == "multi":
        keys = []
        openrouter_keys = config.get("ai_providers", {}).get("openrouter_api_keys", [])
        keys.extend(openrouter_keys)
        together_key = config.get("ai_providers", {}).get("together_api_key", "").strip()
        if together_key:
            keys.append(together_key)
    else:
        keys = []

    logger.debug(f"API keys found: {keys}")
    if not keys:
        logger.error(f"No API keys found for provider mode '{provider_mode}'. Exiting.")
        sys.exit(1)

    models = []
    for i in range(threads):
        sub_config = dict(config)
        if provider_mode == "together":
            sub_config["ai_providers"]["together_api_key"] = keys[i % len(keys)]
        elif provider_mode == "openrouter":
            sub_config["ai_providers"]["openrouter_api_keys"] = [keys[i % len(keys)]]
        elif provider_mode == "multi":
            sub_config["ai_providers"]["openrouter_api_keys"] = [keys[i % len(keys)]]
            sub_config["ai_providers"]["together_api_key"] = keys[i % len(keys)]
        model = AIModel(sub_config, provider_manager)
        models.append(model)

    while True:
        try:
            raw_jobs = get_jobs_for_scoring(conn, limit=threads)
            jobs: List[Dict[str, Any]] = []
            now_ts = time.time()
            for j in raw_jobs:
                if j['id'] in retry_map:
                    if now_ts < retry_map[j['id']]:
                        logger.debug(f"Skipping job {j['id']} until {retry_map[j['id']]:.1f}")
                        continue
                    else:
                        del retry_map[j['id']]
                jobs.append(j)

            logger.info(f"Retrieved {len(jobs)} jobs for scoring after retry filter")

            if not jobs:
                console.print("[yellow]No new jobs found for scoring. Waiting for 30 sec...")
                await asyncio.sleep(30)
                continue

            tasks = []
            for i, job in enumerate(jobs):
                ai_model = models[i % threads]
                tasks.append(evaluate_and_update_job(job, ai_model, conn, verbose))

            await asyncio.gather(*tasks)
            await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Error in main processing loop: {e}")
            await asyncio.sleep(5)


async def main(
        config_path: str,
        verbose: bool,
        endless: bool,
        threads: int
) -> None:
    console.print("[bold blue]Starting job processing and ranking with concurrency (skip+retry on extraction fails)")
    try:
        config = load_config(config_path)
        # print("DEBUG => FULL CONFIG:\n", config)
        # print("DEBUG => together_api_key =", repr(config.get("together_api_key")))

        set_verbose(verbose)

        if not endless:
            await process_and_rank_jobs(config, verbose, threads)
        else:
            while True:
                await process_and_rank_jobs(config, verbose, threads)
                logger.info("Waiting 60 sec before next pass in endless mode.")
                await asyncio.sleep(60)
    except Exception as e:
        logger.error(f"Fatal error in main: {e}")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Process & rank jobs with concurrency, skipping 0.0 extractions & retrying later"
    )
    parser.add_argument(
        "config",
        nargs="?",
        default="/Users/sasha/IdeaProjects/nomorejobfuckery/jobfuq/conf/config.toml",
        help="Path to config file"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Increase output verbosity"
    )
    parser.add_argument(
        "--endless",
        action="store_true",
        help="Run in endless mode (loop forever)"
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=1,
        help="Number of concurrent evaluations (each with unique API key)"
    )
    args = parser.parse_args()

    asyncio.run(main(
        config_path=args.config,
        verbose=args.verbose,
        endless=args.endless,
        threads=args.threads
    ))
