# JOBFUQ: Jackpot Opportunity Bot for Fixing Unemployment Quick

![banner.png](assets/banner.png)

Tired of tailoring your CV just to be ghosted faster than a bad Tinder date? **JOBFUQ** is your AI-powered, LinkedIn-scraping, career-saving chaos agent—finding jobs that might actually deserve you (because rejection should at least come with effort).
No more doomscrolling through listings that require ten years of experience in a four-year-old framework. **JOBFUQ** adapts to your career missteps, your inflated skills, and even your creative interpretations of “team player.”
And if you’ve ever been too honest on your resume? Yeah, **JOBFUQ** saw that. And it’s judging you.

---

## Why JOBFUQ?

- **Cuts through job board noise like HR cuts budgets!** ✂️💸
- **AI-powered job scoring** that goes beyond boring keyword matching. 🎯🤖
- **Real-time scraping** from LinkedIn with stealth and advanced evasion techniques. 🕵️‍♂️🔍
- **Reality-check feedback** on your skills, gaps, and why you might still be unemployable. 📉💀
- **Database-backed storage** for those who love data but hate rejection. 🗄️📊
- **Dynamic rate limiting** so you don’t get banned before you even get ignored. 🚦🙈

> **Note:** **JOBFUQ** works (mostly). Setup experience inspired by Kafka, but with swearing. Now with smarter scoring — mind the skill gaps. 🧠⚠️

---

## Updated Features & Architecture Overview
> **Note:** We temporarily simplified the graphics for the scraper output during the refactoring process. Rest assured, a refreshed and polished look is coming back soon!

### Project Structure

The project now sports a cleaner, more modular layout:

```
├── CONTRIBUTING.md         # Contribution guidelines for developers
├── LICENSE                 # Licensing information (CC BY-NC 4.0)
├── README.md               # This file
├── assets                  # Assets (e.g., banner image)
├── data                    # SQLite databases (e.g., job_listings.db)
├── jobfuq
│   ├── __init__.py
│   ├── conf              # Configuration files (config.toml, linked_config.toml, config_example.toml)
│   ├── database          # Database logic and SQL queries
│   │   ├── __init__.py
│   │   ├── database.py   # Database connection & table creation logic
│   │   └── sql           # SQL query files for schema and operations
│   ├── graphics          # Graphical output utilities (e.g., ASCII gradients)
│   │   ├── __init__.py
│   │   └── graphics.py
│   ├── llm               # AI integration layer
│   │   ├── __init__.py
│   │   ├── ai_model.py   # AI model integration & evaluation logic
│   │   ├── evaluator.py  # Evaluates job fit using AI
│   │   ├── provider_manager.py  # Manages provider selection (together, openrouter, multi)
│   │   └── models        # AI provider integrations (openrouter.py, together.py)
│   ├── logger            # Logging setup and utilities
│   │   ├── __init__.py
│   │   └── logger.py
│   ├── processing        # Scoring engine & result display
│   │   ├── __init__.py
│   │   └── processor.py  # Processes and ranks jobs using a two-pass AI evaluation:
│   │                      # - First pass: preliminary scoring using a lightweight model.
│   │                      # - Second pass (rescoring): Top vacancies are re-evaluated in-depth.
│   ├── prompts           # Prompt templates for career advice
│   │   ├── deepseek_r1_career_advisor_template.txt
│   │   └── examples      # Sample prompts for various job roles
│   ├── scraper           # LinkedIn scraping engine
│   │   ├── __init__.py
│   │   ├── orchestrator.py  # Orchestrates scraping flows
│   │   ├── flows         # Modular flows: search, details, update
│   │   └── core          # Core scraping utilities (scraper.py, linked_utils.py, filter.py)
│   └── utils             # Helper functions (e.g., config loading)
└── session_store         # Saved sessions for LinkedIn accounts
```

### What’s New?

- **Fully Free AI via OpenRouter:** Now you can use the AI integration entirely for free through OpenRouter, removing previous usage restrictions.
- **Two-Pass Scoring System:**
    - **First Pass:** A lighter, preliminary model quickly scores all scraped jobs.
    - **Second Pass (Rescoring):** Top vacancies are then re-evaluated in-depth using a more advanced model for improved accuracy.
- **Enhanced Configuration Defaults:** Updated `config.toml` now features a longer `time_filter` (e.g., "2419200" for 4 weeks), reduced `concurrent_details`, and a lower `max_postings` value.
- **LinkedIn Scraping Enhancements:** Refined selectors in `linked_config.toml` and improved stealth techniques (randomized mouse physics, dynamic viewport resizing, fake HTTP traffic, etc.).
- **Database Schema Overhaul:** Additional SQL scripts now manage blacklist tables and support the two-pass scoring workflow.
- **Modular Scraping Flows:** Refactored into distinct flows (search, details, update) managed by the orchestrator for better process control.

---

## 🚀 So-called "Quick" start
For now, this process may seem a bit manual—especially in adjusting filters to avoid unwanted jobs. We plan to simplify the process in future releases, but for now, it is what it is.

### 1️⃣ Clone & Install Dependencies

```bash
git clone https://github.com/chernistry/jobfuq.git
cd jobfuq
python3 -m venv venv
source venv/bin/activate  # (Windows: venv\Scripts\activate)
pip install -r requirements.txt
playwright install
```

### 2️⃣ Configure `jobfuq/conf/config.toml`

Edit this file to set up your environment:

- **linkedin_credentials:** Add one or more LinkedIn logins.
- **search_queries:** Define your target job keywords, locations, and filters. **Note:** Adjust the filter settings to exclude jobs you want to avoid.
- **ai_providers:** Configure your AI provider mode (`"together"`, `"openrouter"`, or `"multi"`), API keys, and models.
- **prompt:** Path to your custom career prompt (e.g., `prompts/deepseek_r1_career_advisor_template.txt`).
- **scraping:** Set the mode (`stealth`, `normal`, or `aggressive`) and other options like `user_agents`.
- **time_filter:** Use a relative time filter (e.g., `"2419200"` for 4 weeks) to limit postings.
- **max_postings:** The maximum number of jobs to scrape per query.
- **headless:** Toggle headless mode for browser automation.

> **Warning:** DO NOT use your personal LinkedIn account—use a dummy/test account if you plan on scraping.

### 3️⃣ (Optional) Customize Your Prompt

- Head over to `jobfuq/prompts/` and edit an existing template or create your own.
- Remove `<think>` sections if your chosen model doesn’t support chain-of-thought.
- Update the `prompt` field in your config accordingly.

### 4️⃣ Run JOBFUQ

**Combined Flows (Recommended):**

```bash
python -m jobfuq.scraper.orchestrator --recipe "search+details" --verbose [--hours <num>]
```


**Combined Flows (Recommended):**

```bash
python -m jobfuq.scraper.orchestrator --recipe "search+details" --verbose [--hours <num>]
```

- **Run Separately:**

- **Scraper:**
  ```bash
  python -m jobfuq.scraper.orchestrator [--manual-login] [--debug-single] [--endless] [--verbose] [--hours <num>]
  ```

- **Processor (Two-Pass Scoring):**
  ```bash
  python -m jobfuq.processing.processor [config_path] [--verbose] [--endless] [--threads <num>] [--recipe scoring/rescoring/all]
  ```

> **Tip:** Use the `--hours <num>` flag to limit results to jobs posted within the last `<num>` hours.

### 5️⃣ Inspect & Query Your Results

- All scraped jobs are stored in the SQLite database located at the path specified in your config (e.g., `data/job_listings.db`).
- Use your favorite SQLite GUI (e.g., DB Browser for SQLite, DBeaver, or SQLiteStudio) to inspect the data.

---

## 🗄️ Database Schema Highlights

### Key Tables:

- **job_listings:** Stores all job data along with evaluation metrics.

### Notable Fields in `job_listings`:

- `title`, `company`, `company_url`, `location`, `description`
- `remote_allowed`, `job_state`
- `company_size` & `company_size_score`
- `job_url`, `date`, `listed_at`
- AI-evaluated metrics: `skills_match`, `model_fit_score`, `preliminary_score`, `success_probability`, `role_complexity`, `effort_days_to_fit`, `critical_skill_mismatch_penalty`, `experience_gap`
- `last_checked`, `last_reranked`, `is_posted`, `application_status`

---

## 🤖 AI Integration & Scoring

### Provider Configuration

The `[ai_providers]` section in `config.toml` lets you choose between:

- **together:** Runs models via Together.ai (e.g., `Meta-Llama-3.1-405B-Instruct-Turbo`).
- **openrouter:** Now supports fully free usage for AI evaluation (e.g., `deepseek/deepseek-r1:free`).
- **multi:** Alternates between providers for load balancing.

> **Note:** The **multi** mode is experimental. If you’re feeling brave, fork it and improve it; otherwise, stick to a single provider.

Example configuration:

```toml
[ai_providers]
provider_mode = "together"
threads = 4
together_api_key = "your_together_api_key_here"
together_model = "meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo"
together_rpm = 58
prompt = "prompts/deepseek_r1_career_advisor_template.txt"
```

### Two-Pass Evaluation Workflow

- **First Pass (Preliminary Scoring):**
    - A lightweight model quickly scores all scraped jobs.
- **Second Pass (Rescoring):**
    - Top vacancies (as identified by the preliminary pass) are re-evaluated in-depth by a more advanced model.
    - This results in more accurate and reliable job fit assessments.

### Scoring Metrics

- **Skills Match:** Alignment between candidate skills and job requirements.
- **Experience Gap:** Difference between candidate experience and role expectations.
- **Model Fit Score:** AI’s overall evaluation of job suitability.
- **Success Probability:** Likelihood of landing the job.
- **Role Complexity:** Demands of the position.
- **Effort Days to Fit:** Estimated days to bridge skill gaps.
- **Critical Skill Mismatch Penalty:** Penalties for missing key qualifications.

The final **Preliminary Score** is computed in `jobfuq/processing/processor.py` using these metrics, with adjustments for recency and company size.

---

## ⚙️ Advanced Configuration & Customization

### Configuration Files

- **Main Config:** `jobfuq/conf/config.toml`
    - Controls scraping behavior, AI provider settings, and database paths.
- **LinkedIn Config:** `jobfuq/conf/linked_config.toml`
    - Contains selectors, pagination rules, and stealth settings for LinkedIn scraping.
- **SQL Queries:** Located in `jobfuq/database/sql/`
    - Modify these if you need to adjust the schema or query logic.

### Customizable Options

- **Scraping Mode:**
    - Options: `"stealth"` (default), `"normal"`, or `"aggressive"`.
- **User Agents:**
    - A list of modern browser signatures to reduce detection.
- **Timeouts & Delays:**
    - Adjust `selector_timeout`, `get_text_timeout`, and `retry_delay` to fine-tune performance.
- **Session Management:**
    - Sessions are stored in the `session_store` directory. Use manual login (`--manual-login`) if needed.
- **Two-Pass Scoring Parameters:**
    - Configure thresholds and model-specific settings for both the preliminary and rescoring passes.

---

## 🛡️ Anti-Detection & Stealth Features

JOBFUQ now comes equipped with a range of anti-detection measures:

- **Randomized Mouse Movements & Scrolling Patterns:** Simulates human behavior.
- **Dynamic Viewport Resizing & Network Throttling:** Adjusts to avoid detection.
- **Fake HTTP Traffic Generation:** Keeps your scraping footprint under the radar.
- **Manual Captcha Handling:** Automatically switches to headful mode when necessary.

All these features are implemented in `jobfuq/scraper/core/linked_utils.py` and can be fine-tuned via `jobfuq/conf/linked_config.toml`.

---

## 📊 Viewing & Analyzing Results

### Output Metrics

- **Preliminary Score:** Overall job fit from the first pass.
- **Rescored Value:** Final, in-depth evaluation after the second pass.
- **Skills Match:** How well your skills align.
- **Model Fit Score:** AI’s evaluation of job suitability.
- **Success Probability:** Your chance of landing the job.
- **Effort Days to Fit:** Estimated days to bridge any skill gaps.
- **Critical Skill Mismatch Penalty:** Higher values indicate bigger gaps.

---

## 🛠️ Running in Different Modes

### Continuous (Endless) Mode

Run the scraper or processor in an endless loop with the `--endless` flag:

```bash
python -m jobfuq.scraper.orchestrator --endless --verbose
```

### Debug Mode

For debugging a single job, use:

```bash
python -m jobfuq.scraper.orchestrator --debug-single [job_URL]
```

This is perfect if you just want to see what’s wrong with a particular posting.

---

## 📜 SQL Queries Cheat Sheet

### Common Queries

- **Insert Job:** Inserts or updates a job listing.
- **Get Jobs for Scoring:** Retrieves unprocessed jobs for AI evaluation.
- **Update Job Scores:** Writes the AI evaluation back to the database.
- **Blacklist Checks:** Ensures unwanted jobs are filtered out.

All SQL files are located in `jobfuq/database/sql/` and are dynamically loaded by the database module.

---

## 🚨 Troubleshooting Guide

| **Problem**                   | **Solution**                                                                                  |
|-------------------------------|-----------------------------------------------------------------------------------------------|
| **Playwright Errors**         | Ensure Playwright is installed (`playwright install`) and verify your network connection.       |
| **Login Failures**            | Check your `linkedin_credentials` in `config.toml` or try running with `--manual-login`.         |
| **Captcha/Checkpoint Issues** | If a captcha appears in headless mode, the tool automatically switches to headful mode for manual solving. |
| **AI Provider Rate Limits**   | Verify your API keys and adjust `together_rpm`/`openrouter_rpm` in your config as needed.       |
| **Database Lock Issues**      | Ensure the database (e.g., `data/job_listings.db`) isn’t open in another application.            |
| **Timeouts/Skipped Jobs**     | Increase timeout values in `config.toml` or run with `--verbose` to diagnose the issue.         |

---

## 💰 AI Costs & Free Options

### Free Use (OpenRouter)

- **OpenRouter (Free):** Use the fully free AI endpoint (e.g., `deepseek/deepseek-r1:free`) with no cost, subject to API usage limits.
- Upgrade or switch to Together.ai if you require higher throughput or additional features.

### Model Pricing (Per Million Tokens)

| **Provider**          | **Model**                            | **Cost**  | **Speed** | **Quality** |
|-----------------------|--------------------------------------|-----------|-----------|-------------|
| **Together**          | Meta-Llama-3.1-405B-Instruct-Turbo   | ~$3.50    | ⚡⚡       | ⭐⭐⭐⭐       |
| **Together**          | Llama-3.3-70B-Instruct-Turbo         | ~$0.88    | ⚡⚡⚡      | ⭐⭐⭐        |
| **Together**          | DeepSeek-R1                          | ~$7.00    | ⚡         | ⭐⭐⭐⭐⭐     |
| **OpenRouter (Free)** | deepseek/deepseek-r1:free            | $0.00     | ⚡         | ⭐⭐⭐⭐      |

---

## 🤝 Contribute & Maintain

### How to Contribute

- **Fork & Branch:**
  ```bash
  git checkout -b feature/your-idea
  ```
- **Commit & Push:** Open a pull request and describe your changes.
- **Keep it Real:** Maintain the original sarcastic tone. If it says “bum,” let it be bum.

### Developer Tips

- All configurations and prompt templates are editable.
- Review `jobfuq/processing/processor.py` and the modules under `jobfuq/llm/` (especially `ai_model.py` and `provider_manager.py`) for scoring and AI logic.
- Check SQL files in `jobfuq/database/sql/` for schema changes.
- Explore the modular scraping flows in `jobfuq/scraper/flows` and core utilities in `jobfuq/scraper/core` for scraping enhancements.
- The logging in `jobfuq/logger/logger.py` is your friend—set `--verbose` for more insights.

### Upcoming Enhancements

- Expanded job site integrations (e.g., Indeed, Glassdoor).
- Docker Compose setup for one-command deployment.
- Alternative LLM support (Mistral, Claude, etc.) for improved AI evaluations.
- A web-based UI to manage scraping and view results.

---

## 🖥️ SQLite GUI Tools Recommendation

Here are some tools to inspect your `job_listings.db`:

| **Tool**                  | **Platform**      | **Links**                                                                                   |
|---------------------------|-------------------|---------------------------------------------------------------------------------------------|
| DB Browser for SQLite     | Win, Mac, Linux   | [GitHub](https://github.com/sqlitebrowser/sqlitebrowser) / [Website](https://sqlitebrowser.org/) |
| Beekeeper Studio          | Win, Mac, Linux   | [GitHub](https://github.com/beekeeper-studio/beekeeper-studio) / [Website](https://www.beekeeperstudio.io/) |
| SQLiteStudio              | Win, Mac, Linux   | [GitHub](https://github.com/pawelsalawa/sqlitestudio) / [Website](https://sqlitestudio.pl/)   |
| DBeaver                   | Win, Mac, Linux   | [GitHub](https://github.com/dbeaver/dbeaver) / [Website](https://dbeaver.io/)                 |
| SQLite Expert             | Windows           | [Website](https://www.sqliteexpert.com/)                                                    |
| Ducklet                   | Mac               | [Website](https://ducklet.app/)                                                              |
| CuteSqlite                | Windows           | [GitHub](https://github.com/shinehanx/CuteSqlite) / [Website](https://github.com/shinehanx/CuteSqlite) |
| wxSQLitePlus              | Win, Mac, Linux   | [GitHub](https://github.com/guanlisheng/wxsqliteplus) / [Website](https://github.com/guanlisheng/wxsqliteplus) |

---

## 📬 Contact & Support

- **Email:** For job seekers and contributors, contact [endless@loop.in.net](mailto:endless@loop.in.net).
- **GitHub Issues:** Report bugs or request features on the repository’s Issues page.

> **Disclaimer:** Use JOBFUQ at your own risk. Scraping LinkedIn may violate their terms—so don’t blame us if you get banned.

---

## 🎭 Final Thoughts

JOBFUQ: because job hunting shouldn’t feel like a psychological thriller with no plot.

Whether you’re a washed-up tech bro, a job board burnout, or just here for the memes, this AI-fueled chaos engine has your back.

Tweak the settings, let the bot judge your skills, and watch it serve up jobs that *might* actually deserve you.

Now go forth and get employed — or at least pretend you tried. 🏆

---

## Appendix: Detailed File References

- **jobfuq/conf/config.toml:** Main configuration for scraping, AI integration, and database settings.
- **jobfuq/conf/linked_config.toml:** Contains selectors, pagination rules, and stealth settings for LinkedIn scraping.
- **jobfuq/database:** Manages SQLite connections, table creation, and SQL query loading (see `jobfuq/database/sql/`).
- **jobfuq/llm:** AI integration layer (includes `ai_model.py`, `provider_manager.py`, and models in `jobfuq/llm/models/`).
- **jobfuq/logger/logger.py:** Logging setup and utilities.
- **jobfuq/processing/processor.py:** Contains the scoring algorithm, ASCII visualization, and two-pass job ranking logic.
- **jobfuq/scraper/orchestrator.py:** Orchestrates scraping flows.
- **jobfuq/scraper/flows:** Houses distinct scraping flows (search, details, update).
- **jobfuq/scraper/core:** Core scraping functions and utilities (including LinkedIn-specific modules).
- **jobfuq/prompts:** Contains prompt templates for career evaluations and sample prompts.
- **jobfuq/utils:** Helper functions for configuration loading and miscellaneous tasks.

---

## Changelog

- **v2.0:**
    - Overhauled directory structure for enhanced modularity.
    - Introduced a two-pass scoring system with preliminary scoring and in-depth rescoring.
    - Fully integrated free AI usage via OpenRouter.
    - Refactored scraping engine into dedicated flows under `jobfuq/scraper/flows` and added an orchestrator.
    - Updated configuration defaults (e.g., extended `time_filter`, reduced `max_postings`).

- **v1.9:**
    - Updated SQL schema with new fields.
    - Added manual captcha handling and session management.
    - Improved logging and error handling.

---

## License & Ethics

- **License:** CC BY-NC 4.0 — Free for non-commercial use.
- **Ethical Scraping:** JOBFUQ is designed with rate limiting and delays to minimize impact on target websites.
- **Data Privacy:** No personal data is stored. Use responsibly.

---

## Final Note

Remember: JOBFUQ is as much a tool as it is a statement—a no-nonsense, brutally honest way to cut through job board BS.

If you love it, fork it. If you hate it, fork it anyway and fix what’s broken.

Happy job hunting!
