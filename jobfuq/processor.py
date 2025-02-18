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
        if d < 0: d = 0
        if d > max_days: return 0.0
        return max(0.0, min(100.0, 100.0 * (1.0 - d/float(max_days))))
    except:
        return 0.0

def calculate_company_size_score(sz):
    try:
        if 3 <= sz <= 5: return 100.0
        if sz == 6: return 80.0
        if sz == 7: return 60.0
        if sz >= 8: return 40.0
        return max(0.0, 50.0 - (sz*5))
    except:
        return 0.0

def softened_competition_penalty(cnt):
    try:
        c = min(cnt, 500)
        return max(0.0, 100.0 - (c*0.2))
    except:
        return 50.0

def calculate_preliminary_score(e: Dict[str, float], rec: float, applicants: int, csize: float) -> float:
    sm = e.get('skills_match', 0.0)
    xp_gap = e.get('experience_gap', 0.0)
    mod_fit = e.get('model_fit_score', 0.0)
    sp = e.get('success_probability', 50.0)
    cpen = e.get('critical_skill_mismatch_penalty', 0.0)
    p = min(80.0, xp_gap + cpen)
    base = sm - p
    if base < 1.0: base = 1.0
    f = base * (sp/100.0)
    comp = softened_competition_penalty(applicants)/100.0
    f *= comp
    ra = (rec - 50.0)*0.2
    ca = (csize - 50.0)*0.1
    f += ra + ca
    return max(0.0, min(f, 100.0))

async def evaluate_and_update_job(job: Dict[str, Any], model: AIModel, conn, verbose: bool):
    try:
        recency = calculate_recency_score(job['date'])
        app_count = job.get('applicants_count') or 0
        ev = await evaluate_job(model, job)
        if not ev:
            logger.warning(f"No eval for job {job['id']}")
            retry_map[job['id']] = time.time()+180
            return
        sm = ev.get('skills_match', 0.0)
        mf = ev.get('model_fit_score', 0.0)
        reasoning = ev.get('reasoning','')
        if sm==0 and mf==0 and not reasoning:
            logger.warning(f"Job {job['id']} -> suspicious zero extraction")
            retry_map[job['id']] = time.time()+180
            return
        final_score = calculate_preliminary_score(ev, recency, app_count, calculate_company_size_score(job.get('company_size_score',0)))
        updated = {**job, **ev, 'preliminary_score': final_score}
        update_job_scores(conn, job['id'], updated)
        logger.info(f"Job {job['id']} => preliminary_score={final_score:.2f}")
        if verbose:
            t = Table(title=f"Job {job['id']}")
            t.add_column("Metric")
            t.add_column("Value")
            items = {
                "Preliminary Score": final_score,
                "Skills Match": sm,
                "Model Fit Score": mf,
                "Success Probability": ev.get('success_probability',50.0),
                "Experience Gap": ev.get('experience_gap',0.0),
                "Critical Penalty": ev.get('critical_skill_mismatch_penalty',0.0),
                "role_complexity": ev.get('role_complexity',0.0),
            }
            for k,v in items.items():
                t.add_row(k, f"{v:.1f}")
            t.add_row("Recency Score", f"{recency:.1f}")
            t.add_row("Applicants", str(app_count))
            p = Panel(t)
            console.print(p)
            console.print(f"[bold]Reasoning:[/bold] {ev.get('reasoning','')}\n[bold]Areas:[/bold] {ev.get('areas_for_development','')}")
    except Exception as ex:
        logger.error(f"Err job {job.get('id')}: {ex}")
        if job['id'] not in retry_map:
            retry_map[job['id']] = time.time()+180

async def process_and_rank_jobs(conf, verbose, threads):
    conn = create_connection(conf)
    # add_fit_score_columns(conn)
    pm = ProviderManager(conf)
    mode = conf.get('ai_providers',{}).get('provider_mode','together')
    if mode=='together':
        k = conf.get('ai_providers',{}).get('together_api_key','').strip()
        keys = [k] if k else []
    elif mode=='openrouter':
        keys = conf.get('ai_providers',{}).get('openrouter_api_keys',[])
    elif mode=='multi':
        keys = []
        open_k = conf.get('ai_providers',{}).get('openrouter_api_keys',[])
        keys.extend(open_k)
        tk = conf.get('ai_providers',{}).get('together_api_key','').strip()
        if tk: keys.append(tk)
    else:
        keys = []
    if not keys:
        logger.error("No AI keys found.")
        sys.exit(1)
    models=[]
    for i in range(threads):
        sc = dict(conf)
        if mode=='together':
            sc['ai_providers']['together_api_key'] = keys[i%len(keys)]
        elif mode=='openrouter':
            sc['ai_providers']['openrouter_api_keys'] = [keys[i%len(keys)]]
        elif mode=='multi':
            sc['ai_providers']['openrouter_api_keys'] = [keys[i%len(keys)]]
            sc['ai_providers']['together_api_key'] = keys[i%len(keys)]
        m = AIModel(sc, pm)
        models.append(m)
    while True:
        try:
            raw = get_jobs_for_scoring(conn, limit=threads)
            now = time.time()
            jobs=[]
            for j in raw:
                if j['id'] in retry_map:
                    if now<retry_map[j['id']]:
                        continue
                    else:
                        del retry_map[j['id']]
                jobs.append(j)
            logger.info(f"{len(jobs)} jobs for scoring")
            if not jobs:
                console.print("[yellow]No new jobs. Sleep 30s")
                await asyncio.sleep(30)
                continue
            tasks=[]
            for i,jb in enumerate(jobs):
                tasks.append(evaluate_and_update_job(jb, models[i%threads], conn, verbose))
            await asyncio.gather(*tasks)
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Main loop: {e}")
            await asyncio.sleep(5)

async def main(config_path, verbose, endless, threads):
    console.print("[bold blue]Starting Processor")
    try:
        c = load_config(config_path)
        set_verbose(verbose)
        if not endless:
            await process_and_rank_jobs(c, verbose, threads)
        else:
            while True:
                await process_and_rank_jobs(c, verbose, threads)
                logger.info("Looping again in 60s.")
                await asyncio.sleep(60)
    except:
        sys.exit(1)

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('config',nargs='?',default='jobfuq/conf/config.toml')
    p.add_argument('-v','--verbose',action='store_true')
    p.add_argument('--endless',action='store_true')
    p.add_argument('--threads',type=int,default=1)
    a=p.parse_args()
    asyncio.run(main(a.config,a.verbose,a.endless,a.threads))
