# Contributing

## Development boundaries

The primary application is `app.py`; `career_app.py` is an optional legacy workflow.
Keep presentation in `src/assistant/ui.py`, state transitions in `runtime.py`,
transactional writes in `store.py`, and capability adapters in `tools.py`.
Use `PathManager` for persistent paths. Never import Streamlit from a service module.

## Local checks

```powershell
python -m pip install -e ".[dev]"
python scripts/check_static.py
ruff check . --no-cache
```

These checks parse Python and detect selected lint errors. They are not runtime,
integration, security or performance tests. A pull request should state which
behavior was actually verified and which remains unverified.

## Adding a tool

Register a unique name, human-readable label, bounded JSON schema and handler.
Read-only tools must not send messages or write user state. Tools that modify
files, pages, schedules, memory or external systems require approval. Do not
expose arbitrary shell execution, arbitrary Python evaluation or unrestricted
filesystem paths to the model. Do not add automatic retry to a mutation.

## Review expectations

Explain the user problem, state transitions, failure handling and data stored.
Include screenshots only from synthetic accounts, redact personal information,
and never commit `.env`, SQLite databases, chat logs or browser profiles.
Changes to action semantics should include a proposed manual verification plan;
do not run real sends or job applications as routine checks.
