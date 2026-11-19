# Verification record

Revision: personal assistant reconstruction, 2026-11-19.

## Checks performed

| Check | Scope | Result |
| --- | --- | --- |
| Python source parsing and in-memory compilation | 27 Python source files | Passed |
| Local import modules and named symbols | `src.*`, `config.*` imports | Passed |
| Literal SQL placeholder arity | Statically recognizable `execute(sql, tuple)` calls | Passed |
| Service/UI dependency rule | No direct Streamlit import in main service modules | Passed |
| Ruff error rules | Entire source repository | Passed |
| Ruff F and import ordering rules | New assistant, scripts and main entry | Passed |

Commands:

```text
python scripts/check_static.py
ruff check . --no-cache
ruff check src/assistant scripts app.py --select F,I --no-cache
```

Source compilation uses Python's compiler in memory; it does not import or run
the application. SQL placeholder inspection verifies parameter counts, not SQL
execution or transaction behavior. Import resolution checks local declarations,
not installed dependency compatibility. These distinctions are intentional.

## Manual source review

Reviewed queue claiming, cancellation before invocation, rejection, approval
expiry, snapshot changes, uncertain mutation results, restart interruption,
schedule overlap/misfire handling, bounded file access, and model usage limits.
Reviewed source-release allowlists and credential/data ignore rules.

## Not performed

No application startup, model requests, SQLite runtime lifecycle tests, browser
interaction, QQ/WeChat sends, email sends, UI rendering, package installation,
cross-platform runtime testing, security assessment or performance benchmark.
CI configuration has been added; its remote run result is not yet available.

The following are required before claiming production readiness: reproducible
installation, functional and integration tests, cancellation/recovery fault
injection, browser adapter validation, permission review and measured reliability.
