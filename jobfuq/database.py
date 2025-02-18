import os
import sqlite3
import time
from typing import Any, Dict, List

from jobfuq.logger import logger

SQL_QUERIES: Dict[str, str] = {}


# ==== SQL QUERY LOADING ==== #
def load_sql_queries() -> Dict[str, str]:
    """
    Load SQL queries from .sql files located in the 'sql' directory.

    This function reads all .sql files in the designated directory and stores
    their content in a global dictionary.

    Returns:
        Dict[str, str]: A dictionary where each key is the SQL file name (without
        extension) and the value is the corresponding SQL query.

    Raises:
        FileNotFoundError: If the 'sql' directory does not exist.
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



# ==== DATABASE CONNECTION & TABLE CREATION ==== #
def create_connection(config: Dict[str, Any]) -> sqlite3.Connection:
    """
    Create a SQLite database connection.

    Args:
        config (Dict[str, Any]): Configuration dictionary containing database parameters.

    Returns:
        sqlite3.Connection: A SQLite database connection object.
    """
    db_path: str = config.get("db_path", "data/test_job_listings.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    return sqlite3.connect(db_path)



def create_table(conn: sqlite3.Connection) -> None:
    """
    Create the job listings table in the database.

    Args:
        conn (sqlite3.Connection): SQLite database connection.
    """
    q: str = load_sql_queries()["create_job_listings_table"]
    conn.execute(q)
    conn.commit()



def create_blacklist_table(conn: sqlite3.Connection) -> None:
    """
    Create the blacklist table in the database.

    Args:
        conn (sqlite3.Connection): SQLite database connection.
    """
    q: str = load_sql_queries()["create_blacklist_table"]
    conn.execute(q)
    conn.commit()
    logger.info("Blacklist table verified.")



def create_blacklisted_companies_table(conn: sqlite3.Connection) -> None:
    """
    Create the blacklisted companies table in the database.

    Args:
        conn (sqlite3.Connection): SQLite database connection.
    """
    q: str = load_sql_queries()["create_blacklisted_companies_table"]
    conn.execute(q)
    conn.commit()
    logger.info("Blacklisted companies table verified.")



# ==== BLACKLIST MANAGEMENT ==== #
def load_blacklist(conn: sqlite3.Connection) -> Dict[str, set]:
    """
    Load the blacklist and whitelist from the database.

    Args:
        conn (sqlite3.Connection): SQLite database connection.

    Returns:
        Dict[str, set]: A dictionary containing two sets: 'blacklist' and 'whitelist'.
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



def is_company_blacklisted(
        conn: sqlite3.Connection, company_name: str, company_url: str
) -> bool:
    """
    Check if a company is blacklisted in the database.

    Args:
        conn (sqlite3.Connection): SQLite database connection.
        company_name (str): The name of the company.
        company_url (str): The URL of the company.

    Returns:
        bool: True if the company is blacklisted, False otherwise.
    """
    try:
        q: str = load_sql_queries()["is_company_blacklisted"]
        c = conn.execute(q, (company_name,))
        return c.fetchone()[0] > 0
    except Exception:
        return False



# ==== JOB INSERTION & UPDATION ==== #
def insert_job(conn: sqlite3.Connection, job: Dict[str, Any]) -> None:
    """
    Insert a job listing into the database.

    Args:
        conn (sqlite3.Connection): SQLite database connection.
        job (Dict[str, Any]): Dictionary containing job details.
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
        logger.debug(f'Inserted job {job["job_url"]}')
    except Exception as e:
        logger.error(f"Insert error: {e}")



def update_job_scores(
        conn: sqlite3.Connection, job_id: Any, ranked: Dict[str, Any]
) -> None:
    """
    Update job scores for a given job listing.

    Args:
        conn (sqlite3.Connection): SQLite database connection.
        job_id (Any): The ID of the job listing.
        ranked (Dict[str, Any]): A dictionary containing the new score values.
    """
    q: str = load_sql_queries()["update_job_scores"]
    params = (
        ranked.get("preliminary_score", 0.0),
        ranked.get("skills_match", 0.0),
        ranked.get("model_fit_score", 0.0),
        ranked.get("success_probability", 5e1),
        ranked.get("role_complexity", 5e1),
        ranked.get("effort_days_to_fit", 0.0),
        ranked.get("critical_skill_mismatch_penalty", 0.0),
        ranked.get("experience_gap", 0.0),
        ranked.get("areas_for_development", ""),
        ranked.get("reasoning", ""),
        int(time.time()),
        job_id,
    )
    try:
        conn.execute(q, params)
        conn.commit()
    except Exception as e:
        logger.error(f"Update scores error: {e}")



# ==== JOB QUERYING ==== #
def job_exists(conn: sqlite3.Connection, job_url: str) -> bool:
    """
    Check if a job listing exists in the database.

    Args:
        conn (sqlite3.Connection): SQLite database connection.
        job_url (str): The URL of the job listing.

    Returns:
        bool: True if the job exists, False otherwise.
    """
    q: str = load_sql_queries()["job_exists"]
    c = conn.execute(q, (job_url,))
    return c.fetchone()[0] > 0



def get_jobs_for_scoring(
        conn: sqlite3.Connection, limit: int = 1
) -> List[Dict[str, Any]]:
    """
    Retrieve job listings for scoring.

    Args:
        conn (sqlite3.Connection): SQLite database connection.
        limit (int, optional): Maximum number of jobs to retrieve. Defaults to 1.

    Returns:
        List[Dict[str, Any]]: A list of dictionaries containing job details.
    """
    q: str = load_sql_queries()["get_jobs_for_scoring"]
    c = conn.execute(q, (limit,))
    rows = c.fetchmany(limit)

    if not rows:
        return []

    tot = rows[0][-1]
    logger.debug(f"Found {tot} jobs for scoring, returning {len(rows)} now.")
    cols = [desc[0] for desc in c.description]
    out: List[Dict[str, Any]] = []

    for r in rows:
        d = dict(zip(cols[:-1], r[:-1]))
        out.append(d)

    return out