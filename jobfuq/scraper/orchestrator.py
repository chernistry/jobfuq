#!/usr/bin/env python3
"""
orchestrator.py

This orchestrator allows you to run one or more scraper flows based on a chosen recipe.
Available flows: search, details, update.
Combine them using delimiters (e.g., "--recipe=search+details" or "--recipe=all").
It accepts common arguments like --hours, --manual-login, --endless, --verbose, --debug-single, and extra.
The updated config (including time_filter from --hours) is passed to each flow.
"""

import argparse
import asyncio
from jobfuq.logger.logger import set_verbose
from jobfuq.utils.utils import load_config

# Import the main functions from each flow module.
from jobfuq.scraper.flows.search import main as search_main
from jobfuq.scraper.flows.details import main as details_main
from jobfuq.scraper.flows.update import main as update_main

async def orchestrate(args):
    config = load_config('jobfuq/conf/config.toml')
    set_verbose(args.verbose)

    # Set time_filter based on --hours parameter.
    if args.hours is not None:
        config['time_filter'] = f"r{args.hours * 3600}"
    else:
        config.setdefault('time_filter', 'r604800')

    # Передаем остальные параметры запуска.
    config['manual_login'] = args.manual_login
    config['endless'] = args.endless
    config['debug_single'] = args.debug_single
    config['extra'] = args.extra

    # Определяем, какие flow запускать, исходя из параметра --recipe.
    recipe = args.recipe.lower().strip()
    valid_flows = {'search', 'details', 'update'}
    if recipe == "all":
        flows_to_run = valid_flows
    else:
        for delim in ["/", "+", ","]:
            if delim in recipe:
                flows_to_run = {flow.strip() for flow in recipe.split(delim)}
                break
        else:
            flows_to_run = {recipe}
        flows_to_run = flows_to_run & valid_flows

    if not flows_to_run:
        print("No valid recipe selected. Valid options: search, details, update, or combinations (e.g. search+details).")
        return

    print(f"Running flows: {', '.join(sorted(flows_to_run))}")

    if "search" in flows_to_run:
        await search_main(config)
    if "details" in flows_to_run:
        await details_main(config)
    if "update" in flows_to_run:
        await update_main(config)

def main():
    parser = argparse.ArgumentParser(description="LinkedIn Scraper Orchestrator")
    # Убираем короткий ключ -h для hours (конфликтует с help)
    parser.add_argument('--hours', type=int, default=None, help='Time filter in hours')
    parser.add_argument('--manual-login', action='store_true', help='Manual login')
    parser.add_argument('--endless', action='store_true', help='Scrape continuously')
    parser.add_argument('--verbose', '-v', action='store_true', help='Enable verbose logging')
    parser.add_argument('--debug-single', action='store_true', help='Scrape a single job and exit for debugging')
    parser.add_argument('extra', nargs='*', help='Optional job URL for debug-single mode')
    parser.add_argument('--recipe', type=str, default="all", help='Recipe to run: search, details, update, or combinations like search+details')
    args = parser.parse_args()
    asyncio.run(orchestrate(args))

if __name__ == "__main__":
    main()