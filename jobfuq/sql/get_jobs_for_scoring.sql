WITH RankedJobs AS (
    SELECT
        jl.id,
        jl.company,
        jl.title,
        jl.description,
        jl.application_status,
        jl.date,
        jl.applicants_count,
        jl.company_size_score,
        jl.listed_at,
        (
            (CASE WHEN LOWER(jl.title) LIKE '%devops%' THEN 10 ELSE 0 END) +
            (CASE WHEN LOWER(jl.title) LIKE '%platform%' THEN 9 ELSE 0 END) +
            (CASE WHEN LOWER(jl.title) LIKE '%sre%' THEN 8 ELSE 0 END) +
            (CASE WHEN LOWER(jl.title) LIKE '%cloud%' THEN 7 ELSE 0 END) +
            (CASE WHEN LOWER(jl.title) LIKE '%aws%' THEN 6 ELSE 0 END) +
            (CASE WHEN LOWER(jl.title) LIKE '%infra%' THEN 5 ELSE 0 END) +
            (CASE WHEN LOWER(jl.title) LIKE '%backend%' THEN 4 ELSE 0 END) +
            (CASE WHEN LOWER(jl.title) LIKE '%mlops%' THEN 3 ELSE 0 END) +
            (CASE WHEN LOWER(jl.title) LIKE '%ai%' THEN 2 ELSE 0 END) +
            (CASE WHEN LOWER(jl.title) LIKE '%python%' THEN 1 ELSE 0 END)
            ) AS priority_score
    FROM job_listings jl
    WHERE jl.is_posted = 1
      AND jl.application_status LIKE 'not applied%'
      AND jl.job_state LIKE 'active'
      AND jl.final_score IS NULL
      AND jl.company IS NOT NULL
      AND jl.description IS NOT NULL
      AND TRIM(jl.description) <> ''
      AND jl.description <> 'About the job'
      -- Исключаем компании из нового blacklist
      AND NOT EXISTS (
        SELECT 1 FROM blacklisted_companies bc
        WHERE LOWER(jl.company) = LOWER(bc.company)
    )
      -- Исключаем вакансии по ключевым словам из blacklist, но учитываем whitelist
      AND NOT EXISTS (
        SELECT 1 FROM blacklist bl
        WHERE bl.type = 'blacklist'
          AND LOWER(jl.title) LIKE '%' || LOWER(bl.value) || '%'
          AND NOT EXISTS (
            SELECT 1 FROM blacklist wl
            WHERE wl.type = 'whitelist'
              AND LOWER(jl.title) LIKE '%' || LOWER(wl.value) || '%'
        )
    )
)
SELECT *,
       (SELECT COUNT(*) FROM job_listings jl
        WHERE jl.is_posted = 1
          AND jl.application_status LIKE 'not applied%'
          AND jl.company IS NOT NULL
          AND jl.final_score IS NULL
          -- Исключаем компании из нового blacklist
          AND NOT EXISTS (
            SELECT 1 FROM blacklisted_companies bc
            WHERE LOWER(jl.company) = LOWER(bc.company)
        )
          -- Исключаем вакансии по ключевым словам из blacklist, но учитываем whitelist
          AND NOT EXISTS (
            SELECT 1 FROM blacklist bl
            WHERE bl.type = 'blacklist'
              AND LOWER(jl.title) LIKE '%' || LOWER(bl.value) || '%'
              AND NOT EXISTS (
                SELECT 1 FROM blacklist wl
                WHERE wl.type = 'whitelist'
                  AND LOWER(jl.title) LIKE '%' || LOWER(wl.value) || '%'
            )
        )
       ) AS total_jobs
FROM RankedJobs
ORDER BY priority_score DESC
LIMIT ?;
