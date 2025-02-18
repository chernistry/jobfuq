CREATE TABLE IF NOT EXISTS blacklisted_companies (
                                                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                                                     company TEXT NOT NULL UNIQUE
)
