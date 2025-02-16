"""
Database Module

This module contains functions for creating and managing the SQLite database used for job listings.
It supports operations such as creating tables, inserting/updating job records, and loading a blacklist.
"""

import os
import json
import sqlite3
import time
import re
from typing import Any, Dict, List, Set, Tuple

from jobfuq.logger import logger


def add_fit_score_columns(conn: sqlite3.Connection) -> None:
    """
    Add columns for scoring to the job_listings table if they don't exist (for backward compatibility).

    The columns added are:
        - skills_match (REAL)
        - resume_similarity (REAL)
        - final_fit_score (REAL)
        - final_score (REAL)
        - success_probability (REAL)
        - confidence (REAL)
        - effort_days_to_fit (INTEGER)
        - critical_skill_mismatch_penalty (REAL)
        - last_checked (INTEGER)
        - last_reranked (INTEGER)
        - areas_for_development (TEXT)
        - reasoning (TEXT)
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
            # Column already exists, ignore the error.
            pass
    conn.commit()


def create_blacklist_table(conn: sqlite3.Connection) -> None:
    """
    Create the blacklist table if it does not exist.

    The blacklist table contains:
        - id (INTEGER PRIMARY KEY AUTOINCREMENT)
        - type (TEXT, e.g. 'blacklist' or 'whitelist')
        - value (TEXT)
        - reason (TEXT, optional)
        - date_added (TIMESTAMP, defaults to current timestamp)
        Unique constraint is enforced on (type, value).
    """
    conn.execute('''CREATE TABLE IF NOT EXISTS blacklist (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        type TEXT NOT NULL, 
        value TEXT NOT NULL,
        reason TEXT, 
        date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP, 
        UNIQUE(type, value)
    )''')
    conn.commit()
    logger.info("Created or verified blacklist table.")


def create_connection(config: Dict[str, Any]) -> sqlite3.Connection:
    """
    Create and return a SQLite database connection.

    The database path is taken from the configuration using the key 'test_job_listings.db'.
    If not provided, it defaults to "data/test_job_listings.db". If the directory does not exist,
    it will be created.

    :param config: A dictionary containing configuration values.
    :return: A SQLite database connection.
    """
    db_path: str = config.get('db_path', "data/test_job_listings.db")
    db_dir: str = os.path.dirname(db_path)
    if not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    return sqlite3.connect(db_path)


def create_table(conn: sqlite3.Connection) -> None:
    """
    Create the job_listings table if it does not exist.

    The table stores various attributes of each job, including scoring metrics and status flags.
    """
    conn.execute('''CREATE TABLE IF NOT EXISTS job_listings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        title TEXT, 
        company TEXT, 
        company_url TEXT,
        location TEXT, 
        description TEXT, 
        remote_allowed BOOLEAN, 
        job_state TEXT, 
        company_size TEXT,
        company_size_score INTEGER, 
        job_url TEXT UNIQUE, 
        date TEXT, 
        listed_at INTEGER, 
        applicants_count INTEGER,
        skills_match REAL, 
        resume_similarity REAL, 
        final_fit_score REAL, 
        areas_for_development TEXT,
        reasoning TEXT, 
        recency_score REAL, 
        applicant_score REAL, 
        final_score REAL,
        overall_relevance REAL DEFAULT 0.0, 
        last_checked INTEGER, 
        last_reranked INTEGER,
        last_relevance_check INTEGER, 
        is_posted INTEGER DEFAULT 1, 
        application_status TEXT DEFAULT 'not applied'
    )''')
    conn.commit()


def insert_job(conn: sqlite3.Connection, job: Dict[str, Any]) -> None:
    """
    Insert a new job or update an existing job record in the job_listings table.

    Uses a parameterized query to safely insert or replace a job record.

    :param conn: SQLite database connection.
    :param job: A dictionary containing job data.
    """
    try:
        conn.execute('''INSERT OR REPLACE INTO job_listings (
            title, company, company_url, location, description, remote_allowed, job_state,
            company_size, company_size_score, job_url, date, listed_at, applicants_count,
            overall_relevance, is_posted, application_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
            job['title'], job['company'], job['company_url'], job['location'], job['description'],
            job['remote_allowed'], job['job_state'], job.get('company_size', 'Unknown'),
            job.get('company_size_score', 0), job['job_url'], job['date'], job['listed_at'],
            job.get('applicants_count', None), job.get('overall_relevance', False),
            job.get('is_posted', 1), job.get('application_status', 'not applied')
        ))
        conn.commit()
        logger.debug(f"Inserted/updated job {job['job_url']}")
    except Exception as e:
        logger.error(f"Error inserting job: {e}\n{job}")


def is_company_blacklisted(conn: sqlite3.Connection, company_name: str, company_url: str) -> bool:
    """
    Check whether a company is blacklisted.

    :param conn: SQLite database connection.
    :param company_name: Name of the company.
    :param company_url: URL identifier for the company.
    :return: True if the company is blacklisted, False otherwise.
    """
    cursor = conn.execute(
        "SELECT COUNT(*) FROM blacklist WHERE type = 'blacklist' AND (LOWER(?) LIKE '%' || LOWER(value) || '%' OR LOWER(?) LIKE '%' || LOWER(value) || '%')",
        (company_name, company_url)
    )
    return cursor.fetchone()[0] > 0


def job_exists(conn: sqlite3.Connection, job_url: str) -> bool:
    """
    Check if a job exists in the job_listings table by its URL.

    :param conn: SQLite database connection.
    :param job_url: The URL of the job.
    :return: True if the job exists, False otherwise.
    """
    cursor = conn.execute("SELECT COUNT(*) FROM job_listings WHERE job_url = ?", (job_url,))
    return cursor.fetchone()[0] > 0


def get_jobs_for_scoring(conn: sqlite3.Connection, limit: int = 1) -> List[Dict[str, Any]]:
    """
    Retrieve jobs that need to be scored.

    This query selects jobs that are posted, not yet applied, and have no final score,
    while also counting total available jobs for scoring.

    :param conn: SQLite database connection.
    :param limit: Maximum number of jobs to retrieve.
    :return: A list of dictionaries representing the job records.
    """
    query: str = '''
        SELECT id, company, title, description, application_status, date, 
               applicants_count, company_size_score, listed_at,
               (SELECT COUNT(*) FROM job_listings jl 
                WHERE is_posted = 1 AND application_status LIKE 'not applied%' AND final_score IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM blacklist bl WHERE bl.type = 'blacklist'
                        AND LOWER(jl.title) LIKE '%' || LOWER(bl.value) || '%'
                        AND NOT EXISTS (
                            SELECT 1 FROM blacklist wl WHERE wl.type = 'whitelist'
                              AND LOWER(jl.title) LIKE '%' || LOWER(wl.value) || '%'
                        )
                  )
               ) AS total_jobs
        FROM job_listings
        WHERE is_posted = 1 AND application_status LIKE 'not applied%' AND final_score IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM blacklist bl WHERE bl.type = 'blacklist'
                AND LOWER(title) LIKE '%' || LOWER(bl.value) || '%'
                AND NOT EXISTS (
                    SELECT 1 FROM blacklist wl WHERE wl.type = 'whitelist'
                      AND LOWER(title) LIKE '%' || LOWER(wl.value) || '%'
                )
          )
        ORDER BY skills_match DESC
        LIMIT ?
    '''
    try:
        cursor = conn.execute(query, (limit,))
        results = cursor.fetchmany(limit)
        total_jobs = results[0][-1] if results else 0
        logger.debug(f"Found {total_jobs} jobs available for scoring. Fetching {len(results)} for processing.")
        if results:
            logger.debug(f"Sample job - ID: {results[0][0]}, Title: {results[0][2]} @ {results[0][1]}")
        cols = [col[0] for col in cursor.description]
        # Exclude the total_jobs field from the mapping
        return [dict(zip(cols[:-1], row[:-1])) for row in results]
    except sqlite3.Error as e:
        logger.error(f"Database error: {e}")
        return []


def load_blacklist(conn: sqlite3.Connection) -> Dict[str, Set[str]]:
    """
    Load blacklist and whitelist data from the database.

    :param conn: SQLite database connection.
    :return: A dictionary with keys 'blacklist' and 'whitelist', each containing a set of strings.
    """
    try:
        cursor = conn.execute("SELECT type, value FROM blacklist")
        bl_data: Dict[str, Set[str]] = {"blacklist": set(), "whitelist": set()}
        for typ, val in cursor.fetchall():
            typ = typ.strip().lower()
            val = val.strip()
            if typ in bl_data:
                bl_data[typ].add(val)
            else:
                bl_data["blacklist"].add(val)
        return bl_data
    except Exception as e:
        logger.error(f"Error loading blacklist: {e}")
        return {"blacklist": set(), "whitelist": set()}


def update_job_scores(conn: sqlite3.Connection, job_id: int, ranked_job: Dict[str, Any]) -> None:
    """
    Update the job scores in the database for a given job.

    :param conn: SQLite database connection.
    :param job_id: The ID of the job to update.
    :param ranked_job: A dictionary containing the updated scoring metrics and job details.
    """
    try:
        conn.execute('''UPDATE job_listings SET final_score = ?, skills_match = ?, resume_similarity = ?,
            final_fit_score = ?, success_probability = ?, confidence = ?, effort_days_to_fit = ?,
            critical_skill_mismatch_penalty = ?, areas_for_development = ?, reasoning = ? WHERE id = ?''',
                     (ranked_job.get('final_score', 0.0), ranked_job.get('skills_match', 0.0),
                      ranked_job.get('resume_similarity', 0.0), ranked_job.get('final_fit_score', 0.0),
                      ranked_job.get('success_probability', 0.6), ranked_job.get('confidence', 0.7),
                      ranked_job.get('effort_days_to_fit', 7), ranked_job.get('critical_skill_mismatch_penalty', 0.0),
                      ranked_job.get('areas_for_development', ''), ranked_job.get('reasoning', ''), job_id)
                     )
        conn.commit()
    except Exception as e:
        logger.error(f"Error updating job scores: {e}")
