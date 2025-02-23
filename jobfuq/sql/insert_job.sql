INSERT OR REPLACE INTO job_listings (
    title,
    company,
    company_url,
    location,
    description,
    remote_allowed,
    job_state,
    company_size,
    company_size_score,
    job_url,
    date,
    listed_at,
    applicants_count,
    overall_relevance,
    is_posted,
    application_status
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
