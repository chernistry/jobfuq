UPDATE job_listings
SET final_score = ?, skills_match = ?, resume_similarity = ?,
    final_fit_score = ?, success_probability = ?, confidence = ?,
    effort_days_to_fit = ?, critical_skill_mismatch_penalty = ?,
    areas_for_development = ?, reasoning = ?
WHERE id = ?;
