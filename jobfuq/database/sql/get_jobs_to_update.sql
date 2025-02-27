SELECT job_url, last_checked, listed_at
FROM job_listings
WHERE is_posted = 1
  AND (
    (last_checked IS NULL AND ((strftime('%s','now') * 1000) - listed_at) > 86400000)
        OR
    (last_checked IS NOT NULL AND ((strftime('%s','now') * 1000) - last_checked) > 86400000)
    );
