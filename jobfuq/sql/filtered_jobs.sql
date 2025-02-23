WITH
    FILTERED_LISTINGS AS (
        SELECT *
        FROM job_listings jl
        WHERE is_posted = 1
          AND NOT EXISTS (
            SELECT 1 FROM blacklisted_companies bc
            WHERE LOWER(jl.company) = LOWER(bc.company)
        )
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
    ),
    JOBS AS (
        SELECT
            CASE
                WHEN skills_match >= 70
                    AND critical_skill_mismatch_penalty <= 10
                    AND success_probability >= 55
                    AND experience_gap <=10
                    THEN 'TOP'
                WHEN skills_match >= 60
                    AND success_probability >= 65
                    AND critical_skill_mismatch_penalty <= 16
                    AND experience_gap <= 10
                    THEN 'GOOD'
                ELSE NULL
                END AS CL,
            id, application_status AS ST, company AS CP, title AS TT, date AS DT, description, reasoning,
            ROUND(model_fit_score, 2) AS FS, skills_match AS SK,
            success_probability AS PR, effort_days_to_fit AS EF,
            critical_skill_mismatch_penalty AS PN, company_size_score AS SZ,
            areas_for_development AS GAPS, scoring_model as model
        FROM FILTERED_LISTINGS
    )
SELECT * FROM JOBS
WHERE CL IS NOT NULL
ORDER BY CASE CL WHEN 'TOP' THEN 1 ELSE 2 END, SK DESC, PR DESC

