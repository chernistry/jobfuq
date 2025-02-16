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
import json
import math
import os
import sys
import time
from datetime import datetime
from typing import List, Dict, Any

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.table import Table
from rich.panel import Panel

from .utils import load_config
from .llm_handler import AIModel, evaluate_job, ProviderManager
from .database import (
    create_connection, get_jobs_for_scoring, update_job_scores, add_fit_score_columns
)
from .logger import logger, set_verbose

console = Console()

config = load_config("jobfuq/conf/config.toml")


# A global in-memory map tracking jobs that had extraction failures,
# mapping job_id -> next_eligible_timestamp.
# We skip these jobs until at least 3 minutes have passed.
retry_map: Dict[int, float] = {}




def calculate_recency_score(job_date: str, max_days: int = 60) -> float:
    """
    Compute how recent the job was posted. Older postings get lower scores.

    :param job_date: A string in YYYY-MM-DD format representing the posting date.
    :param max_days: The maximum days beyond which a job is considered stale (score=0).
    :return: A recency factor between 0.0 (stale) and ~1.0 (very recent).
    """
    try:
        days_diff = (datetime.now() - datetime.strptime(job_date, '%Y-%m-%d')).days
        if days_diff > max_days:
            return 0.0
        return 1.0 / (math.log(days_diff + 1) + 0.5)
    except Exception as e:
        logger.error(f"Error calculating recency score: {e}")
        return 0.0


def calculate_company_size_score(size_score: int) -> float:
    """
    Convert an integer-based company size rating into a 0.0–1.0 score.

    :param size_score: The integer rating indicating company size.
    :return: A score between 0.0 and 1.0 reflecting preference for mid-range sizes.
    """
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
    """
    Penalize jobs with many applicants. The penalty is softened (log-based) to avoid extreme drops.

    :param applicant_count: Number of applicants or “viewed” count from LinkedIn.
    :return: A multiplier typically between 0.7 and 1.0 depending on applicant volume.
    """
    base = 1.0 / (math.log(applicant_count + 3) + 1)
    return 0.7 + 0.3 * base


def calculate_final_score(
        evaluation: Dict[str, Any],
        recency_score: float,
        applicant_count: int,
        company_size_score: float
) -> float:
    """
    Compute a final job rank score from 0.0 to 1.0, aggregating multiple factors.

    :param evaluation: Dictionary of AI-extracted fields (skills_match, etc.).
    :param recency_score: A factor indicating how recent the job posting is.
    :param applicant_count: Number of applicants for the job.
    :param company_size_score: The size-based score for the company.
    :return: A final job fit/rank score in [0.01, 1.0].
    """
    skills_match = evaluation.get('skills_match', 0.0)
    resume_similarity = evaluation.get('resume_similarity', 0.0)
    success_probability = evaluation.get('success_probability', 0.8)
    confidence = evaluation.get('confidence', 0.8)
    effort_days_to_fit = evaluation.get('effort_days_to_fit', 4.0)
    critical_penalty = max(evaluation.get('critical_skill_mismatch_penalty', 0.0), 0.0)

    # 1) Base fit
    initial_fit = (skills_match * 0.6) + (resume_similarity * 0.4)

    # 2) Multiply by success_probability & confidence
    base = initial_fit * success_probability * confidence

    # 3) Scale penalty if success_probability is high
    penalty_factor = 0.2 * (1.0 - 0.5 * success_probability)
    if penalty_factor < 0.05:
        penalty_factor = 0.05
    base -= (critical_penalty * penalty_factor)

    # 4) Slight multiplier for fewer days needed
    upskill_mult = max(0.90, 1.0 - math.log(effort_days_to_fit + 1) * 0.03)
    base *= upskill_mult

    # 5) Additional confidence adjustment
    conf_adjust = 1.0 - ((1.0 - confidence) ** 2 * 0.1)
    base *= conf_adjust

    # 6) Adjust for applicant competition
    comp_factor = softened_competition_penalty(applicant_count) * (1.0 + success_probability * 0.2)
    base *= comp_factor

    # 7) Recency & company size small bumps
    rec_adj = (recency_score - 0.5) * 0.1
    csize_adj = (company_size_score - 0.5) * 0.1
    base += (rec_adj + csize_adj)

    # 8) Non-linear ramp above 0.70
    scaled = base * 1.3
    if scaled <= 0.7:
        final = scaled
    else:
        extra = scaled - 0.7
        damped_extra = 0.3 * (1 - math.exp(-extra * 3))
        final = 0.7 + damped_extra

    # 9) Ease factor
    ease_factor = 1.0 + ((30 - effort_days_to_fit) / 300)
    final *= ease_factor
    return max(min(final, 1.0), 0.01)


async def evaluate_and_update_job(
        job: Dict[str, Any],
        ai_model: AIModel,
        conn,
        verbose: bool
) -> None:
    """
    Evaluate a single job with AI, compute a final score, and update the DB.

    If AI extraction fails (0.0 for key fields), we skip and schedule a retry.

    :param job: A job dictionary from the DB.
    :param ai_model: The AIModel instance to run the evaluation.
    :param conn: An open DB connection.
    :param verbose: If True, prints detailed evaluation info to console.
    """
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

        # If all numeric are zero, skip updating & mark retry in 3 min
        if (sm == 0.0 and rs == 0.0 and ffs == 0.0):
            if not reasoning or "extraction failed" in reasoning.lower() or "error during evaluation" in dev_areas.lower():
                logger.warning(f"Job {job['id']}: zero numeric & no valid reasoning => retry in 3 min.")
                retry_map[job['id']] = time.time() + 180
                return

        # Otherwise, compute a final score
        company_size_val = calculate_company_size_score(job.get('company_size_score', 0))
        final_score = calculate_final_score(evaluation, recency, applicant_count, company_size_val)
        ranked_job = {**job, **evaluation, 'final_score': final_score}
        update_job_scores(conn, job['id'], ranked_job)
        logger.info(f"Job {job['id']} updated with final score: {final_score:.2f}")

        # Optional console output if verbose
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
    """
    Main concurrency loop for job processing:

    1. Fetch up to `threads` jobs from DB.
    2. Skip any job in `retry_map` if not yet eligible.
    3. Evaluate + update each job in parallel.
    4. Repeat until no new jobs are found, then sleep & retry.

    :param config: Global config dictionary.
    :param verbose: If True, logs additional info to console.
    :param threads: Number of concurrent evaluations to run.
    """
    conn = create_connection(config)
    add_fit_score_columns(conn)

    provider_manager = ProviderManager(config)
    openrouter_keys = config.get("openrouter_api_keys", [])
    if not openrouter_keys:
        logger.warning("No OpenRouter keys found; concurrency with openrouter not possible.")

    # Prepare multiple AIModels if we want concurrency
    models = []
    for i in range(threads):
        sub_config = dict(config)
        sub_config["openrouter_api_keys"] = [openrouter_keys[i % len(openrouter_keys)]] if openrouter_keys else []
        model = AIModel(sub_config, provider_manager)
        models.append(model)

    while True:
        try:
            raw_jobs = get_jobs_for_scoring(conn, limit=threads)
            # Filter out jobs that are in retry_map & not yet eligible
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
    """
    Primary entrypoint for job ranking. Loads config, sets verbosity,
    then runs the main concurrency loop either once or in endless mode.

    :param config_path: File path to the JSON config file.
    :param verbose: If True, enable debug-level logging and rich console output.
    :param endless: If True, repeats processing in a loop until interrupted.
    :param threads: Number of concurrent evaluations.
    """
    console.print("[bold blue]Starting job processing and ranking with concurrency (skip+retry on extraction fails)")
    try:
        config = load_config(config_path)
        set_verbose(verbose)

        if not endless:
            # Single pass
            await process_and_rank_jobs(config, verbose, threads)
        else:
            # Endless loop
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
        default="jobfuq/config.json",
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
        help="Number of concurrent evaluations (each with unique OpenRouter key)"
    )
    args = parser.parse_args()

    asyncio.run(main(
        config_path=args.config,
        verbose=args.verbose,
        endless=args.endless,
        threads=args.threads
    ))
