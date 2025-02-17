"""
Database Module

This module contains functions for creating and managing the SQLite database used for job listings.
It supports operations such as creating tables (job_listings and blacklist), inserting/updating job records,
loading blacklist data, and querying jobs for processing while filtering out blacklisted titles.

Blacklist Filtering Logic:
- A job title is filtered out if it contains any term from the blacklist.
- However, if the title also contains any term from the whitelist, the blacklist filter is overridden.
- The SQL queries are stored in a separate sql_queries.toml file and loaded at runtime.
"""

import os
import sqlite3
import time
import re
from typing import Any, Dict, List, Set, Tuple

import toml
from jobfuq.logger import logger

SQL_QUERIES = None

def load_sql_queries() -> Dict[str, str]:
    """
    Load SQL query strings from the sql_queries.toml file.
    """
    global SQL_QUERIES
    if SQL_QUERIES is None:
        path = os.path.join(os.path.dirname(__file__), "conf/sql_queries.toml")
        with open(path, "r") as f:
            data = toml.load(f)
            SQL_QUERIES = data.get("queries", {})
    return SQL_QUERIES

def add_fit_score_columns(conn: sqlite3.Connection) -> None:
    """
    Add scoring-related columns to the job_listings table if they don't already exist.
    """
    columns: List[Tuple[str, str, Any]] = [
        ('skills_match', 'REAL', 0.0),
        ('resume_similarity', 'REAL', 0.0),
        ('final_fit_score', 'REAL', 0.0),
        ('final_score', 'REAL', 0.0),
        ('success_probability', 'REAL', 0.0),
        ('confidence', 'REAL', 0.7),
        ('effort_days_to_fit', 'INTEGER', 14),
        ('critical_skill_mismatch_penalty', 'REAL', 0.0),
        ('last_checked', 'INTEGER', None),
        ('last_reranked', 'INTEGER', None),
        ('areas_for_development', 'TEXT', None),
        ('reasoning', 'TEXT', None)
    ]
    for col, col_type, _ in columns:
        try:
            conn.execute(f'ALTER TABLE job_listings ADD COLUMN {col} {col_type}')
        except sqlite3.OperationalError:
            # Column already exists; ignore error.
            pass
    conn.commit()

def create_blacklist_table(conn: sqlite3.Connection) -> None:
    """
    Create the blacklist table if it does not exist.
    """
    queries = load_sql_queries()
    conn.execute(queries["create_blacklist_table"])
    conn.commit()
    logger.info("Created or verified blacklist table.")

def create_connection(config: Dict[str, Any]) -> sqlite3.Connection:
    """
    Create and return a SQLite database connection using the path specified in the configuration.
    """
    db_path: str = config.get('db_path', "data/test_job_listings.db")
    db_dir: str = os.path.dirname(db_path)
    if not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    return sqlite3.connect(db_path)

def create_table(conn: sqlite3.Connection) -> None:
    """
    Create the job_listings table if it does not exist.
    """
    queries = load_sql_queries()
    conn.execute(queries["create_job_listings_table"])
    conn.commit()

def insert_job(conn: sqlite3.Connection, job: Dict[str, Any]) -> None:
    """
    Insert a new job or update an existing job record in the job_listings table.
    """
    queries = load_sql_queries()
    try:
        conn.execute(queries["insert_job"], (
            job['title'], job['company'], job['company_url'], job['location'], job['description'],
            job['remote_allowed'], job['job_state'], job.get('company_size', 'Unknown'),
            job.get('company_size_score', 0), job['job_url'], job['date'], job['listed_at'],
            job.get('applicants_count', None), job.get('overall_relevance', 0.0),
            job.get('is_posted', 1), job.get('application_status', 'not applied')
        ))
        conn.commit()
        logger.debug(f"Inserted/updated job {job['job_url']}")
    except Exception as e:
        logger.error(f"Error inserting job: {e}\nJob data: {job}")

def is_company_blacklisted(conn: sqlite3.Connection, company_name: str, company_url: str) -> bool:
    """
    Check whether a company is blacklisted based on its name or URL.
    """
    queries = load_sql_queries()
    cursor = conn.execute(queries["is_company_blacklisted"], (company_name, company_url))
    return cursor.fetchone()[0] > 0

def job_exists(conn: sqlite3.Connection, job_url: str) -> bool:
    """
    Check if a job exists in the job_listings table based on its URL.
    """
    queries = load_sql_queries()
    cursor = conn.execute(queries["job_exists"], (job_url,))
    return cursor.fetchone()[0] > 0

def get_jobs_for_scoring(conn: sqlite3.Connection, limit: int = 1) -> List[Dict[str, Any]]:
    """
    Retrieve jobs that need to be scored.
    """
    queries = load_sql_queries()
    query: str = queries["get_jobs_for_scoring"]
    try:
        cursor = conn.execute(query, (limit,))
        results = cursor.fetchmany(limit)
        total_jobs = results[0][-1] if results else 0
        logger.debug(f"Found {total_jobs} jobs available for scoring. Fetching {len(results)} for processing.")
        if results:
            logger.debug(f"Sample job - ID: {results[0][0]}, Title: {results[0][2]} @ {results[0][1]}")
        cols = [col[0] for col in cursor.description]
        # Exclude the last column (total_jobs) when building the dict.
        return [dict(zip(cols[:-1], row[:-1])) for row in results]
    except sqlite3.Error as e:
        logger.error(f"Database error: {e}")
        return []

def load_blacklist(conn: sqlite3.Connection) -> Dict[str, Set[str]]:
    """
    Load blacklist and whitelist data from the database.
    """
    queries = load_sql_queries()
    try:
        cursor = conn.execute(queries["load_blacklist"])
        bl_data: Dict[str, Set[str]] = {"blacklist": set(), "whitelist": set()}
        for typ, val in cursor.fetchall():
            typ = typ.strip().lower()
            val = val.strip()
            if typ in bl_data:
                bl_data[typ].add(val)
            else:
                # If an unknown type is encountered, default it to blacklist.
                bl_data["blacklist"].add(val)
        return bl_data
    except Exception as e:
        logger.error(f"Error loading blacklist: {e}")
        return {"blacklist": set(), "whitelist": set()}

def update_job_scores(conn: sqlite3.Connection, job_id: int, ranked_job: Dict[str, Any]) -> None:
    """
    Update the job scores in the database for a given job.
    """
    queries = load_sql_queries()
    try:
        conn.execute(queries["update_job_scores"], (
            ranked_job.get('final_score', 0.0),
            ranked_job.get('skills_match', 0.0),
            ranked_job.get('resume_similarity', 0.0),
            ranked_job.get('final_fit_score', 0.0),
            ranked_job.get('success_probability', 0.6),
            ranked_job.get('confidence', 0.7),
            ranked_job.get('effort_days_to_fit', 7),
            ranked_job.get('critical_skill_mismatch_penalty', 0.0),
            ranked_job.get('areas_for_development', ''),
            ranked_job.get('reasoning', ''),
            job_id
        ))
        conn.commit()
    except Exception as e:
        logger.error(f"Error updating job scores: {e}")
