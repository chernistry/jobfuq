SELECT COUNT(*) FROM blacklist
WHERE type = 'blacklist'
  AND (LOWER(?) LIKE '%' || LOWER(value) || '%' OR LOWER(?) LIKE '%' || LOWER(value) || '%')
