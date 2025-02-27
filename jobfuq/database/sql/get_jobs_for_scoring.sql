WITH RankedJobs AS (
    SELECT jl.id,
           jl.company,
           jl.title,
           jl.description,
           jl.application_status,
           jl.date,
           jl.applicants_count,
           jl.company_size_score,
           jl.listed_at,
           jl.model_fit_score,
           jl.preliminary_score,
           jl.success_probability,
           jl.role_complexity,
           jl.effort_days_to_fit,
           jl.critical_skill_mismatch_penalty,
           jl.areas_for_development,
           jl.reasoning,
           jl.overall_relevance,
           jl.last_reranked
    FROM job_listings jl
    WHERE jl.is_posted = 1
      AND jl.application_status LIKE '%not applied%'
      AND (
        (
            jl.last_reranked IS NULL
                OR jl.last_reranked < strftime('%s','now') - 2592000
            )
            OR (
                   (CASE WHEN jl.preliminary_score IS NULL OR jl.preliminary_score = 0 THEN 1 ELSE 0 END) +
                   (CASE WHEN jl.model_fit_score IS NULL OR jl.model_fit_score = 0 THEN 1 ELSE 0 END) +
                   (CASE WHEN jl.success_probability IS NULL OR jl.success_probability = 0 THEN 1 ELSE 0 END) +
                   (CASE WHEN jl.role_complexity IS NULL OR jl.role_complexity = 0 THEN 1 ELSE 0 END) +
                   (CASE WHEN jl.effort_days_to_fit IS NULL OR jl.effort_days_to_fit = 0 THEN 1 ELSE 0 END) +
                   (CASE WHEN jl.critical_skill_mismatch_penalty IS NULL OR jl.critical_skill_mismatch_penalty = 0 THEN 1 ELSE 0 END) +
                   (CASE WHEN jl.areas_for_development IS NULL OR jl.areas_for_development = 0 THEN 1 ELSE 0 END) +
                   (CASE WHEN jl.reasoning IS NULL OR jl.reasoning = 0 THEN 1 ELSE 0 END)
                   ) > 1
        )
      AND jl.company IS NOT NULL
      AND jl.description IS NOT NULL
      AND TRIM(jl.description) <> ''
      AND jl.title IS NOT NULL
)
SELECT *,
       (SELECT COUNT(*)
        FROM job_listings jl
        WHERE jl.is_posted = 1
          AND jl.application_status LIKE '%not applied%'
          AND (
            (
                jl.last_reranked IS NULL
                    OR jl.last_reranked < strftime('%s','now') - 2592000
                )
                OR (
                       (CASE WHEN jl.preliminary_score IS NULL OR jl.preliminary_score = 0 THEN 1 ELSE 0 END) +
                       (CASE WHEN jl.model_fit_score IS NULL OR jl.model_fit_score = 0 THEN 1 ELSE 0 END) +
                       (CASE WHEN jl.success_probability IS NULL OR jl.success_probability = 0 THEN 1 ELSE 0 END) +
                       (CASE WHEN jl.role_complexity IS NULL OR jl.role_complexity = 0 THEN 1 ELSE 0 END) +
                       (CASE WHEN jl.effort_days_to_fit IS NULL OR jl.effort_days_to_fit = 0 THEN 1 ELSE 0 END) +
                       (CASE WHEN jl.critical_skill_mismatch_penalty IS NULL OR jl.critical_skill_mismatch_penalty = 0 THEN 1 ELSE 0 END) +
                       (CASE WHEN jl.areas_for_development IS NULL OR jl.areas_for_development = 0 THEN 1 ELSE 0 END) +
                       (CASE WHEN jl.reasoning IS NULL OR jl.reasoning = 0 THEN 1 ELSE 0 END)
                       ) > 2
            )
          AND jl.company IS NOT NULL
          AND jl.description IS NOT NULL
          AND TRIM(jl.description) <> ''
          AND jl.title IS NOT NULL
       ) AS total_jobs
FROM RankedJobs
LIMIT ?;
