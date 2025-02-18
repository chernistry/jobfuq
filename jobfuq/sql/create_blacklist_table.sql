CREATE TABLE IF NOT EXISTS blacklist (
                                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                                         type TEXT NOT NULL,
                                         value TEXT NOT NULL,
                                         UNIQUE(type, value)
)
