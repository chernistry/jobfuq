import os
import sqlite3
import time
from typing import Any, Dict, List

from jobfuq.logger import logger

SQL_QUERIES = {}

def load_sql_queries():
    global SQL_QUERIES
    if SQL_QUERIES:
        return SQL_QUERIES
    sql_dir = os.path.join(os.path.dirname(__file__), 'sql')
    if not os.path.isdir(sql_dir):
        raise FileNotFoundError(sql_dir)
    for filename in os.listdir(sql_dir):
        if filename.endswith('.sql'):
            key = os.path.splitext(filename)[0]
            path = os.path.join(sql_dir, filename)
            with open(path, 'r', encoding='utf-8') as f:
                SQL_QUERIES[key] = f.read().strip()
    return SQL_QUERIES

def create_connection(config):
    db_path = config.get('db_path', 'data/test_job_listings.db')
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    return sqlite3.connect(db_path)

def create_table(conn):
    q = load_sql_queries()['create_job_listings_table']
    conn.execute(q)
    conn.commit()

def create_blacklist_table(conn):
    q = load_sql_queries()['create_blacklist_table']
    conn.execute(q)
    conn.commit()
    logger.info('Blacklist table verified.')

def create_blacklisted_companies_table(conn):
    q = load_sql_queries()['create_blacklisted_companies_table']
    conn.execute(q)
    conn.commit()
    logger.info('Blacklisted companies table verified.')

def load_blacklist(conn):
    q = load_sql_queries()['load_blacklist']
    c = conn.execute(q)
    r = {'blacklist': set(), 'whitelist': set()}
    for row in c.fetchall():
        t = row[0].strip().lower()
        v = row[1].strip()
        if t in r:
            r[t].add(v)
        else:
            r['blacklist'].add(v)
    return r

def is_company_blacklisted(conn, company_name, company_url):
    try:
        q = load_sql_queries()['is_company_blacklisted']
        c = conn.execute(q, (company_name,))
        return c.fetchone()[0] > 0
    except:
        return False

def insert_job(conn, job):
    q = load_sql_queries()['insert_job']
    params = (
        job['title'],
        job['company'],
        job['company_url'],
        job['location'],
        job['description'],
        job['remote_allowed'],
        job['job_state'],
        job.get('company_size', 'Unknown'),
        job.get('company_size_score', 0),
        job['job_url'],
        job['date'],
        job['listed_at'],
        job.get('applicants_count', None),
        job.get('overall_relevance', 0.0),
        job.get('is_posted', 1),
        job.get('application_status', 'not applied')
    )
    try:
        conn.execute(q, params)
        conn.commit()
        logger.debug(f"Inserted job {job['job_url']}")
    except Exception as e:
        logger.error(f"Insert error: {e}")

def update_job_scores(conn, job_id, ranked):
    q = load_sql_queries()['update_job_scores']
    params = (
        ranked.get('preliminary_score', 0.0),
        ranked.get('skills_match', 0.0),
        ranked.get('model_fit_score', 0.0),
        ranked.get('success_probability', 50.0),
        ranked.get('role_complexity', 50.0),
        ranked.get('effort_days_to_fit', 0.0),
        ranked.get('critical_skill_mismatch_penalty', 0.0),
        ranked.get('experience_gap', 0.0),
        ranked.get('areas_for_development', ''),
        ranked.get('reasoning', ''),
        int(time.time()),
        job_id
    )
    try:
        conn.execute(q, params)
        conn.commit()
    except Exception as e:
        logger.error(f"Update scores error: {e}")

def job_exists(conn, job_url):
    q = load_sql_queries()['job_exists']
    c = conn.execute(q, (job_url,))
    return c.fetchone()[0] > 0

def get_jobs_for_scoring(conn, limit=1):
    q = load_sql_queries()['get_jobs_for_scoring']
    c = conn.execute(q, (limit,))
    rows = c.fetchmany(limit)
    if not rows:
        return []
    tot = rows[0][-1]
    logger.debug(f"Found {tot} jobs for scoring, returning {len(rows)} now.")
    cols = [desc[0] for desc in c.description]
    out = []
    for r in rows:
        d = dict(zip(cols[:-1], r[:-1]))
        out.append(d)
    return out

# def add_fit_score_columns(conn):
#     # Usually we rely on create_job_listings_table for fresh DB,
#     # or use migrate_schema.py for existing DB. This is optional.
#     pass
