UPDATE job_listings
SET
    preliminary_score = ?,
    skills_match = ?,
    model_fit_score = ?,
    success_probability = ?,
    role_complexity = ?,
    effort_days_to_fit = ?,
    critical_skill_mismatch_penalty = ?,
    experience_gap = ?,
    areas_for_development = ?,
    reasoning = ?,
    last_reranked = ?,
    scoring_model = ?
WHERE id = ?;
