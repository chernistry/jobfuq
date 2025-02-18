SELECT COUNT(*)
FROM blacklisted_companies
WHERE LOWER(company) = LOWER(?)
