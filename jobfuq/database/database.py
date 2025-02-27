import os
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, List

from jobfuq.logger.logger import logger

SQL_QUERIES: Dict[str, str] = {}

def load_sql_queries() -> Dict[str, str]:
    """
    Load SQL queries from .sql files located in the 'sql' directory.
    All .sql files are read and cached in a global dictionary.
    """
    global SQL_QUERIES
    if SQL_QUERIES:
        return SQL_QUERIES

    sql_dir: str = os.path.join(os.path.dirname(__file__), "sql")
    if not os.path.isdir(sql_dir):
        raise FileNotFoundError(sql_dir)

    for filename in os.listdir(sql_dir):
        if filename.endswith(".sql"):
            key: str = os.path.splitext(filename)[0]
            path: str = os.path.join(sql_dir, filename)
            with open(path, "r", encoding="utf-8") as f:
                SQL_QUERIES[key] = f.read().strip()
    return SQL_QUERIES

def create_connection(config: Dict[str, Any]) -> sqlite3.Connection:
    """
    Create a SQLite database connection based on the provided config.
    """
    db_path: str = config.get("db_path", "data/test_job_listings.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    return sqlite3.connect(db_path)

def create_table(conn: sqlite3.Connection) -> None:
    """
    Create the job_listings table (if not exists) using the associated SQL query.
    The table now includes a column "scoring_model" for tracking which AI model was used.
    """
    q: str = load_sql_queries()["create_job_listings_table"]
    conn.execute(q)
    conn.commit()

def create_blacklist_table(conn: sqlite3.Connection) -> None:
    """
    Create the blacklist table (if not exists).
    """
    q: str = load_sql_queries()["create_blacklist_table"]
    conn.execute(q)
    conn.commit()
    logger.info("Blacklist table verified.")

def create_blacklisted_companies_table(conn: sqlite3.Connection) -> None:
    """
    Create the blacklisted_companies table (if not exists).
    """
    q: str = load_sql_queries()["create_blacklisted_companies_table"]
    conn.execute(q)
    conn.commit()
    logger.info("Blacklisted companies table verified.")

def load_blacklist(conn: sqlite3.Connection) -> Dict[str, set]:
    """
    Return a dictionary with two sets: {'blacklist': set(...), 'whitelist': set(...)}.
    """
    q: str = load_sql_queries()["load_blacklist"]
    c = conn.execute(q)
    r: Dict[str, set] = {"blacklist": set(), "whitelist": set()}
    for row in c.fetchall():
        t: str = row[0].strip().lower()
        v: str = row[1].strip()
        if t in r:
            r[t].add(v)
        else:
            r["blacklist"].add(v)
    return r

def is_company_blacklisted(conn: sqlite3.Connection, company_name: str, company_url: str) -> bool:
    """
    Check if a company is blacklisted.
    """
    try:
        q: str = load_sql_queries()["is_company_blacklisted"]
        c = conn.execute(q, (company_name,))
        row = c.fetchone()
        return (row[0] > 0) if row else False
    except Exception:
        return False

def insert_job(conn: sqlite3.Connection, job: Dict[str, Any]) -> None:
    """
    Insert or REPLACE a job listing into job_listings.
    """
    q: str = load_sql_queries()["insert_job"]
    params = (
        job["title"],
        job["company"],
        job["company_url"],
        job["location"],
        job["description"],
        job["remote_allowed"],
        job["job_state"],
        job.get("company_size", "Unknown"),
        job.get("company_size_score", 0),
        job["job_url"],
        job["date"],
        job["listed_at"],
        job.get("applicants_count", None),
        job.get("overall_relevance", 0.0),
        job.get("is_posted", 1),
        job.get("application_status", "not applied"),
    )
    try:
        conn.execute(q, params)
        conn.commit()
        logger.debug(f"Inserted job {job['job_url']}")
    except Exception as e:
        logger.error(f"Insert error: {e}")


def insert_job_minimal(conn: sqlite3.Connection, job: dict) -> None:
    """
    Insert a job listing into job_listings with only minimal fields (title and job_url).
    Other fields are set to default values.
    """
    q: str = load_sql_queries()["insert_job"]
    params = (
        job.get("title", "No Title"),
        job.get("company", "No Company"),
        job.get("company_url", ""),
        job.get("location", ""),
        job.get("description", ""),
        job.get("remote_allowed", False),
        job.get("job_state", "ACTIVE"),
        job.get("company_size", "Unknown"),
        job.get("company_size_score", 0),
        job.get("job_url", ""),
        job.get("date", datetime.now().strftime('%Y-%m-%d')),
        job.get("listed_at", int(time.time() * 1000)),
        None,  # applicants_count
        0.0,   # overall_relevance
        1,     # is_posted
        "not applied",
    )
    try:
        conn.execute(q, params)
        conn.commit()
        logger.debug(f"Inserted minimal job {job.get('job_url', 'No URL')}")
    except Exception as e:
        logger.error(f"Insert minimal error: {e}")

def job_exists(conn: sqlite3.Connection, job_url: str) -> bool:
    """
    Check if a job listing with the given job_url already exists.
    """
    q: str = load_sql_queries()["job_exists"]
    c = conn.execute(q, (job_url,))
    row = c.fetchone()
    return (row[0] > 0) if row else False

def update_job_scores(conn: sqlite3.Connection, job_id: Any, ranked: Dict[str, Any]) -> None:
    """
    Update the job_listings row with new scoring data, including the scoring_model.
    """
    q: str = load_sql_queries()["update_job_scores"]
    params = (
        ranked.get("preliminary_score", 0.0),
        ranked.get("skills_match", 0.0),
        ranked.get("model_fit_score", 0.0),
        ranked.get("success_probability", 50.0),
        ranked.get("role_complexity", 50.0),
        ranked.get("effort_days_to_fit", 0.0),
        ranked.get("critical_skill_mismatch_penalty", 0.0),
        ranked.get("experience_gap", 0.0),
        ranked.get("areas_for_development", ""),
        ranked.get("reasoning", ""),
        int(time.time()),
        ranked.get("scoring_model", ""),
        job_id,
    )
    try:
        conn.execute(q, params)
        conn.commit()
    except Exception as e:
        logger.error(f"Update scores error: {e}")

def get_jobs_for_scoring(conn: sqlite3.Connection, limit: int = 1) -> List[Dict[str, Any]]:
    """
    Retrieve up to `limit` jobs to be scored from job_listings.
    """
    q: str = load_sql_queries()["get_jobs_for_scoring"]
    c = conn.execute(q, (limit,))
    rows = c.fetchall()
    logger.debug(f"Returning {len(rows)} jobs for scoring.")
    cols = [desc[0] for desc in c.description]
    # Remove "total_jobs" if present
    if cols and cols[-1].lower() == "total_jobs":
        cols = cols[:-1]
    out = [dict(zip(cols, row[:len(cols)])) for row in rows]
    return out

def get_jobs_for_rescoring(conn: sqlite3.Connection, limit: int = 1) -> List[Dict[str, Any]]:
    """
    Retrieve up to `limit` jobs that qualify for rescoring from job_listings.
    """
    q: str = load_sql_queries()["get_jobs_for_rescoring"]
    c = conn.execute(q, (limit,))
    rows = c.fetchall()
    logger.debug(f"Returning {len(rows)} jobs for rescoring.")
    cols = [desc[0] for desc in c.description]
    if cols and cols[-1].lower() == "total_jobs":
        cols = cols[:-1]
    out = [dict(zip(cols, row[:len(cols)])) for row in rows]
    return out

def get_job_ids_for_scoring(conn: sqlite3.Connection) -> List[int]:
    """
    Return a batch of up to 1000 job IDs for scoring.
    """
    jobs = get_jobs_for_scoring(conn, limit=1000)
    return [job["id"] for job in jobs]

def get_job_ids_for_rescoring(conn: sqlite3.Connection) -> List[int]:
    """
    Return a batch of up to 1000 job IDs for rescoring.
    """
    jobs = get_jobs_for_rescoring(conn, limit=1000)
    return [job["id"] for job in jobs]

def get_job_by_id(conn: sqlite3.Connection, job_id: int) -> Dict[str, Any]:
    """
    Retrieve a single job row by ID from job_listings.
    """
    query = "SELECT * FROM job_listings WHERE id = ?"
    c = conn.execute(query, (job_id,))
    row = c.fetchone()
    if not row:
        return {}
    cols = [desc[0] for desc in c.description]
    return dict(zip(cols, row))

def get_jobs_to_update(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    q: str = load_sql_queries()["get_jobs_to_update"]
    c = conn.execute(q)
    rows = c.fetchall()
    cols = [desc[0] for desc in c.description]
    return [dict(zip(cols, row)) for row in rows]
