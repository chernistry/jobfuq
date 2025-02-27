import difflib

job_filter_config = {
    "roles_positive": [
        "DevOps", "Dev Ops", "Dev-Ops", "Infra", "Application", "Automation",
        "Platform", "Solution", "Quality", "Cloud", "TechOps", "Tech-Ops",
        "Tech Ops", "Reliability", "SRE", "DevSecOps", "MLops", "Data", "Python", "CI/CD", "IT Engineer",
        "AI Engineer", "NLP engineer"

    ],
    "roles_negative": [
        "Embedded", "Mechanical", "Electrical", "Firmware", "Industrial",
        "Hardware", "Security", "SAP", "Oracle", "Designer"
    ],
    "types_positive": ["Engineer", "Developer"],
    "types_negative": [
        "Manager", "TeamLead", "Team Lead", "Group Lead", "Trainer", "Intern", "Researcher", "Scientist",
        "Architect", "Director", "Analyst"
    ],
    "seniority_positive": ["Junior", "Mid", "Senior", "Sr"],
    "seniority_negative": [
        "Principal", "Staff", "Manager", "VP", "Head", "Director", "Chief",
        "Officer", "Intern", "Student"
    ],
    "stop_words": [
        "SAP", "Oracle", "Embedded", "Mechanical", "Electrical", "Firmware", "Design",
        "Industrial", "Hardware", "Security", "Consultant", "Trainer", "Intern",
        "Researcher", "Scientist", "Architect", "Analyst", "Director", "VP", "PCIe",
        "Head", "Chief", "Officer", "Medical", "Healthcare", "Bioinformatics",
        "Pharmaceutical", "Automotive", "Blockchain", "Crypto", "Advertising",
        "Marketing", "Game", "Unity", "Unreal", "3D", "2D", "UX", "UI", "Graphics",
        "SEO", "Finance", "Banking", "Insurance", "Trading", "Quant", "Legal", "Product", "Wordpress",
        "Law", "Patent", "Cortex", "C++", "C#", "Kotlin", "Frontend", "NodeJs", "Node.js", "Java", "CPU Design", "Verification"
    ]
}

def is_mostly_hebrew(text, threshold=0.7):
    """
    Returns True if at least 'threshold' fraction of alphabetic characters in the text
    are in the Hebrew Unicode range.
    """
    if not text:
        return False

    hebrew_count = 0
    letter_count = 0

    for ch in text:
        if ch.isalpha():
            letter_count += 1
            if '\u0590' <= ch <= '\u05FF':  # Hebrew Unicode range
                hebrew_count += 1

    if letter_count == 0:
        return False

    return (hebrew_count / letter_count) >= threshold

def fuzzy_contains(text, pattern, threshold=0.85):
    """
    Returns True if 'pattern' is "present" in 'text' with similarity >= threshold.
    First checks for an exact substring; if not found, it splits the text into words
    and compares each word with the pattern using difflib.SequenceMatcher.
    """
    text = text.lower()
    pattern = pattern.lower()
    if pattern in text:
        return True
    words = text.split()
    for word in words:
        similarity = difflib.SequenceMatcher(None, word, pattern).ratio()
        if similarity >= threshold:
            return True
    return False

def insert_blacklisted_job(conn, title, job_url):
    """
    Inserts a job (with title and URL) into the blacklisted_jobs table,
    after trimming excessive spaces. Uses INSERT OR IGNORE to avoid duplicates.
    """
    clean_title = " ".join(title.strip().split())
    clean_job_url = job_url.strip()
    query = "INSERT OR IGNORE INTO blacklisted_jobs (title, job_url) VALUES (?, ?)"
    conn.execute(query, (clean_title, clean_job_url))
    conn.commit()

def passes_filter(title, description, db_conn=None, job_url=None):
    """
    Applies filters to the job title and description.
    Uses fuzzy matching (with a threshold of 85%) to account for variations or typos.
    If the job fails any filter and a database connection and job_url are provided,
    the job is inserted into the blacklisted_jobs table.
    """
    # Reject if the title or description is mostly Hebrew.
    if is_mostly_hebrew(title, threshold=0.7) or is_mostly_hebrew(description, threshold=0.7):
        if db_conn is not None and job_url is not None:
            insert_blacklisted_job(db_conn, title, job_url)
        return False

    t = title.lower()
    d = description.lower() if description else ""
    cfg = job_filter_config

    # Reject if any stop word fuzzy-matches (85% or higher) in title or description.
    for word in cfg.get("stop_words", []):
        if fuzzy_contains(t, word, 0.85) or fuzzy_contains(d, word, 0.85):
            if db_conn is not None and job_url is not None:
                insert_blacklisted_job(db_conn, title, job_url)
            return False

    # Must have at least one positive role term.
    if not any(fuzzy_contains(t, r, 0.85) for r in cfg.get("roles_positive", [])):
        if db_conn is not None and job_url is not None:
            insert_blacklisted_job(db_conn, title, job_url)
        return False

    # Reject if any negative role term fuzzy-matches.
    if any(fuzzy_contains(t, rn, 0.85) for rn in cfg.get("roles_negative", [])):
        if db_conn is not None and job_url is not None:
            insert_blacklisted_job(db_conn, title, job_url)
        return False

    # Must have at least one positive type term.
    if not any(fuzzy_contains(t, tp, 0.85) for tp in cfg.get("types_positive", [])):
        if db_conn is not None and job_url is not None:
            insert_blacklisted_job(db_conn, title, job_url)
        return False

    # Reject if any negative type term fuzzy-matches.
    if any(fuzzy_contains(t, tn, 0.85) for tn in cfg.get("types_negative", [])):
        if db_conn is not None and job_url is not None:
            insert_blacklisted_job(db_conn, title, job_url)
        return False

    # Reject if any negative seniority term fuzzy-matches.
    if any(fuzzy_contains(t, sn, 0.85) for sn in cfg.get("seniority_negative", [])):
        if db_conn is not None and job_url is not None:
            insert_blacklisted_job(db_conn, title, job_url)
        return False

    return True