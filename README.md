# MCP assessment

Four independent Python 3.12 projects, one per task. Each folder has its own
`pyproject.toml`, tests, demo script and README with the design decisions,
security notes and test evidence.

| Folder | Task | Tests |
|---|---|---|
| [`task1-mcp-server`](task1-mcp-server/) | MCP stdio server exposing customer lookup and refund tools | 41 |
| [`task2-security-gateway`](task2-security-gateway/) | HTTP gateway in front of an MCP server: bearer tokens / signed JWTs, blocks `admin_*` tools for non-admins | 79 |
| [`task3-pii-redaction`](task3-pii-redaction/) | Streaming gateway that redacts PII (emails, phones, SSNs, cards…) from LLM responses, OpenAI and Anthropic formats | 138 |
| [`task4-llm-router`](task4-llm-router/) | LLM router with per-key token budgets in SQLite, rate limits and failover to a backup provider | 58 |

## Running a task

```bash
cd task1-mcp-server            # or any other task folder
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -q
scripts/demo.sh
```

Tasks 3 and 4 can call a real provider (tested with xAI `grok-4.3`): copy
`.env.example` to `.env` in that folder and add your key. `.env` files are
git-ignored.

CI (`.github/workflows/tests.yml`) runs every task's tests and demo on
Ubuntu and macOS.
