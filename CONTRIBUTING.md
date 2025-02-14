Contributing to JobFuq

Thanks for your interest in JobFuq! Your contributions help refine job search automation, AI-powered ranking, and scraping efficiency. Please review these guidelines before submitting code, bug reports, or feature ideas.

Contribution Guidelines

Issues
- Search existing issues before opening a new one to avoid duplicates.
- Clearly describe the problem: include steps to reproduce, expected vs. actual behavior, and logs/screenshots when relevant.
- If proposing an enhancement, explain the problem it solves and its expected impact.

Pull Requests
- Keep PRs focused: one feature, fix, or enhancement per PR.
- Reference the related issue (e.g., "Fixes #123") if applicable.
- Ensure compatibility with Python 3.11+ and adhere to PEP 8, PEP 20, and PEP 257.
- Use type hints, docstrings, and meaningful commit messages.
- Avoid unnecessary dependencies; optimize imports and code structure.
- Performance matters: optimize DB queries, concurrency (asyncio, multiprocessing), and minimize API rate-limit issues.
- Test before submitting: run unit tests (pytest) and integration tests (Playwright for scraping features).

Code Quality & Design
- Follow SOLID principles and use appropriate design patterns.
- When modifying AI-based ranking, justify changes in scoring logic with data-backed reasoning.
- Ensure new features don’t degrade job-matching accuracy or introduce unintended biases.
- Use lazy evaluation techniques (e.g., generators, async iterators) to handle large datasets efficiently.

Security & API Best Practices
- No hardcoded credentials. Use environment variables or .env files for sensitive data.
- Prevent bot detection pitfalls.
- Secure API calls with parameterized queries and input validation.
- Encrypt or hash stored credentials (bcrypt, secrets), and follow OAuth/token best practices.

Community & Communication
- Be respectful and constructive in discussions.
- Use GitHub Issues for bug reports and feature suggestions.
- For deeper technical discussions, consider opening a GitHub Discussion.

License Agreement
By contributing to this repository, you agree that your contributions will be licensed under the same license as the project.

Thank you for helping make JobFuq better!
