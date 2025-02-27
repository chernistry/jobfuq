#!/usr/bin/env python3
import asyncio
import argparse
import sys
import time
from datetime import datetime
from typing import Any, Dict, List

from rich.console import Console
from jobfuq.utils.utils import load_config
from jobfuq.llm.provider_manager import ProviderManager
from jobfuq.llm.evaluator import evaluate_job
from jobfuq.llm.ai_model import AIModel
from jobfuq.database.database import (
    create_connection,
    get_job_ids_for_scoring,
    get_job_ids_for_rescoring,
    get_job_by_id,
    update_job_scores
)
from jobfuq.graphics.graphics import render_evaluation
from jobfuq.logger.logger import logger, set_verbose

console: Console = Console()
retry_map: Dict[Any, float] = {}
CONCURRENCY = 1

def calculate_recency_score(job_date: str, max_days: int = 60) -> float:
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
    try:
        c = min(cnt, 500)
        return max(0.0, 100.0 - (c * 0.2))
    except Exception:
        return 50.0

def calculate_preliminary_score(
        e: Dict[str, float], rec: float, applicants: int, csize: float
) -> float:
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

async def evaluate_and_update_job(
        job: Dict[str, Any],
        model: AIModel,
        conn: Any,
        verbose: bool,
        semaphore: asyncio.Semaphore,
        scoring_model: str
) -> Dict[str, Any]:
    async with semaphore:
        try:
            recency = calculate_recency_score(job["date"])
            app_count = job.get("applicants_count") or 0
            ev = await evaluate_job(model, job)
            if not ev:
                logger.warning(f"No evaluation for job {job['id']}")
                retry_map[job["id"]] = time.time() + 180
                return {}
            final_score = calculate_preliminary_score(
                ev, recency, app_count, calculate_company_size_score(job.get("company_size_score", 0))
            )
            final_score_int = int(round(final_score))
            updated = {**job, **ev, "preliminary_score": final_score_int, "scoring_model": scoring_model}
            update_job_scores(conn, job["id"], updated)
            logger.info(f"✅ Job {job['id']} updated: Preliminary Score = {final_score_int}")
            if verbose:
                render_evaluation(updated, recency, app_count)
            return updated
        except Exception as ex:
            logger.error(f"❌ Error processing job {job.get('id', 'Unknown')}: {ex}")
            if job["id"] not in retry_map:
                retry_map[job["id"]] = time.time() + 180
            return {}

async def process_and_rank_jobs(conf: Dict[str, Any], verbose: bool, threads: int, rescore: bool) -> List[Dict[str, Any]]:
    conn = create_connection(conf)
    provider_manager = ProviderManager(conf)
    mode = conf.get("ai_providers", {}).get("provider_mode", "together").strip().lower()
    if mode == "together":
        key = conf.get("ai_providers", {}).get("together_api_key", "").strip()
        keys = [key] if key else []
        if rescore:
            rescoring_model = conf.get("ai_providers", {}).get("together_rescoring_model", "deepseek-ai/DeepSeek-R1")
            conf["ai_providers"]["together_model"] = rescoring_model
    elif mode == "openrouter":
        keys = conf.get("ai_providers", {}).get("openrouter_api_keys", [])
        if rescore:
            rescoring_model = conf.get("ai_providers", {}).get("openrouter_rescoring_model", "defaultRescoringModel")
            conf["ai_providers"]["openrouter_model"] = rescoring_model
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

    if rescore:
        job_ids = get_job_ids_for_rescoring(conn)
        logger.info(f"Rescoring mode: found {len(job_ids)} jobs.")
    else:
        job_ids = get_job_ids_for_scoring(conn)
        logger.info(f"Scoring mode: found {len(job_ids)} jobs.")

    if not job_ids:
        logger.info("No jobs found. Waiting 30 seconds before next pass.")
        await asyncio.sleep(30)
        return []

    if mode == "together":
        scoring_model = f"Together/{conf.get('ai_providers', {}).get('together_model', 'deepseek-ai/DeepSeek-R1')}"
    elif mode == "openrouter":
        scoring_model = f"OpenRouter/{conf.get('ai_providers', {}).get('openrouter_model', 'defaultOpenRouterModel')}"
    else:
        scoring_model = mode.capitalize()

    sc = dict(conf)
    if mode in ["together", "multi"]:
        sc["ai_providers"]["together_api_key"] = keys[0]
    elif mode == "openrouter":
        sc["ai_providers"]["openrouter_api_keys"] = [keys[0]]
    model = AIModel(sc, provider_manager)

    semaphore = asyncio.Semaphore(CONCURRENCY)
    tasks = []
    for job_id in job_ids:
        now_ts = time.time()
        if (job_id in retry_map) and (now_ts < retry_map[job_id]):
            continue
        job = get_job_by_id(conn, job_id)
        if not job:
            continue
        tasks.append(evaluate_and_update_job(job, model, conn, verbose, semaphore, scoring_model))
    results = []
    if tasks:
        results = await asyncio.gather(*tasks)
    else:
        logger.info("No jobs processed in this cycle.")
    await asyncio.sleep(1)
    return [r for r in results if r]

async def main(config_path: str, verbose: bool, endless: bool, threads: int, recipe: str) -> None:
    console.print("[bold blue]🚀 Starting Processor[/bold blue]")
    try:
        conf = load_config(config_path)
        set_verbose(verbose)
        recipe = recipe.lower() if recipe else "all"
        while True:
            if recipe == "scoring":
                scored = await process_and_rank_jobs(conf, verbose, threads, rescore=False)
                logger.info(f"Scoring mode processed {len(scored)} jobs.")
            elif recipe == "rescoring":
                rescored = await process_and_rank_jobs(conf, verbose, threads, rescore=True)
                logger.info(f"Rescoring mode processed {len(rescored)} jobs.")
            elif recipe == "all":
                scoring_task = asyncio.create_task(process_and_rank_jobs(conf, verbose, threads, rescore=False))
                rescoring_task = asyncio.create_task(process_and_rank_jobs(conf, verbose, threads, rescore=True))
                results = await asyncio.gather(scoring_task, rescoring_task)
                scored, rescored = results
                logger.info(f"All mode: Scoring processed {len(scored)} jobs; Rescoring processed {len(rescored)} jobs.")
            else:
                logger.error(f"Unknown recipe '{recipe}'. Use scoring, rescoring, or all.")
                sys.exit(1)
            if not endless:
                break
            logger.info("🔄 Waiting 60 seconds before next pass...")
            await asyncio.sleep(60)
    except Exception as e:
        logger.error(f"Fatal error in main: {e}")
        sys.exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Process & rank jobs with ASCII gradient + color output"
    )
    parser.add_argument("config", nargs="?", default="jobfuq/conf/config.toml", help="Path to config file")
    parser.add_argument("-v", "--verbose", action="store_true", help="Increase output verbosity")
    parser.add_argument("--endless", action="store_true", help="Run in endless mode (loop forever)")
    parser.add_argument("--threads", type=int, default=1, help="Number of concurrent evaluations")
    parser.add_argument("--recipe", type=str, default="all", help="Recipe mode: scoring/rescoring/all [default=all]")
    args = parser.parse_args()

    asyncio.run(main(args.config, args.verbose, args.endless, args.threads, args.recipe))