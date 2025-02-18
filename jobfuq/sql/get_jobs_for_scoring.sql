WITH RankedJobs AS (
    SELECT
        jl.id, jl.company, jl.title, jl.description, jl.application_status, jl.date,
        jl.applicants_count, jl.company_size_score, jl.listed_at, jl.model_fit_score,
        -- Priority scoring based on common DevOps and related keywords
        CASE
            WHEN LOWER(jl.title) LIKE '%devops%' THEN 10
            WHEN LOWER(jl.title) LIKE '%platform%' THEN 9
            WHEN LOWER(jl.title) LIKE '%sre%' THEN 8
            WHEN LOWER(jl.title) LIKE '%cloud%' THEN 7
            WHEN LOWER(jl.title) LIKE '%aws%' THEN 6
            WHEN LOWER(jl.title) LIKE '%infra%' THEN 5
            WHEN LOWER(jl.title) LIKE '%backend%' THEN 4
            WHEN LOWER(jl.title) LIKE '%mlops%' THEN 3
            WHEN LOWER(jl.title) LIKE '%ai%' THEN 2
            WHEN LOWER(jl.title) LIKE '%python%' THEN 1
            WHEN LOWER(jl.title) LIKE '%site reliability%' THEN 8
            WHEN LOWER(jl.title) LIKE '%kubernetes%' THEN 7
            WHEN LOWER(jl.title) LIKE '%cloud engineer%' THEN 6
            WHEN LOWER(jl.title) LIKE '%automation%' THEN 5
            WHEN LOWER(jl.title) LIKE '%observability%' THEN 4
            WHEN LOWER(jl.title) LIKE '%ansible%' THEN 3
            WHEN LOWER(jl.title) LIKE '%terraform%' THEN 3
            ELSE 0
            END AS priority_score
    FROM job_listings jl
    WHERE jl.is_posted = 1
      AND jl.application_status LIKE 'not applied%'
      AND jl.model_fit_score = 0
      AND jl.company IS NOT NULL
      AND jl.description IS NOT NULL
      AND TRIM(jl.description) <> ''
      -- Exclude blacklisted companies
      AND NOT EXISTS (
        SELECT 1 FROM blacklisted_companies bc
        WHERE LOWER(jl.company) = LOWER(bc.company)
    )
      -- Exclude blacklisted titles unless whitelisted
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
       (SELECT COUNT(*) FROM job_listings WHERE model_fit_score = 0) AS total_jobs
FROM RankedJobs
ORDER BY priority_score DESC
LIMIT ?;
