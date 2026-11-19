# Security and privacy

This is a local, single-user application. Streamlit binds to `127.0.0.1` by
default. There is no account system or tenant isolation. Do not expose it to
the public internet or use it as a shared hosted service without adding those
boundaries and performing a security review.

## Data flow

- API keys remain in environment variables / local `.env` files.
- Prompts, selected document text and tool results are sent to the configured model provider.
- Task history, approvals, notes and usage are stored locally in SQLite, without application-level encryption.
- Browser and desktop integrations inherit the user's logged-in session. Enable only needed tool groups.
- Web content and documents are untrusted input. System prompts alone cannot eliminate prompt injection.

## Action controls

The model selects only registered tools with validated parameters. Mutating
tools wait for approval of exact arguments. Approvals expire after 30 minutes
and after process restart. Interrupted or uncertain mutations are not replayed.
Browser click/fill approvals include a snapshot fingerprint; page changes stop
execution. This is a conservative check, not a guarantee against all page races.

## Reporting

Use the repository's private vulnerability reporting feature if the owner has
enabled it. Otherwise contact the owner privately before posting exploitable
details. Do not include API keys, personal messages or user documents in issues.

## Release status

This revision is statically checked only. No penetration test, external account
integration test, browser compatibility certification or production SLA is claimed.
