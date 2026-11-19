# Operations and release

## Start

Use Python 3.10 or newer in a dedicated virtual environment. Install core with
`python -m pip install -e .`. Install `[desktop]` for Windows QQ/WeChat adapters,
`[ocr]` for the optional legacy image-reading flow, and `[dev]` for lint tooling.
Start with `streamlit run app.py`. Streamlit remains bound to localhost.

The model provider must support OpenAI-compatible `/chat/completions` function
calling, including `tool_calls` and tool result messages. Provider-specific
parameters may differ. The application does not silently fall back to text-only
execution or claim that an unsupported tool call succeeded.

## Configuration precedence

Process environment takes precedence over root `.env`, then `config/.env`.
`MODEL_API_KEY` takes precedence over legacy `SILICONFLOW_API_KEY`.
`MODEL_BASE_URL` supplies the initial endpoint; saved model settings override
initial model name and endpoint. Clear/edit saved settings from the UI when
switching providers. Keys are not stored by the UI.

`PERSONAL_AGENT_DATA_DIR` overrides legacy `AACWORK_DATA_DIR`; otherwise data
is stored under the repository's `data` directory. Restart after environment
changes. WebBridge host/port configuration retains existing `KIMI_WEBBRIDGE_*`
variables. Optional browser/desktop groups are off by default.

## Data paths

| Location under the data root | Content |
| --- | --- |
| `users/` | Original uploaded documents, shared across tasks |
| `agent/operations/assistant.sqlite3` | Main tasks, action receipts, schedules, memory, usage and policy |
| `agent/operations/assistant.lock` | OS worker ownership lock; an existing file alone does not mean locked |
| `agent/config/runtime_settings.json` | Non-secret model configuration, shared with legacy adapters |
| `agent/artifacts/documents/` | Generated Markdown and Word documents |
| `agent/operations/career/career.sqlite3` | Optional legacy recruitment state |

Other existing `agent/` paths belong to historical features and are preserved.
The primary UI does not merge historical task or memory formats automatically.

## Backup and restore

Stop the application before copying the complete data root, including SQLite
WAL/SHM files if they exist. Back up `.env` separately using private storage.
Restore while the application is stopped. Never publish a data backup to GitHub.
On startup the worker interrupts in-flight tasks and invalidates pending
approvals. Verify external systems before creating a replacement task.

## Failure handling

| Symptom | Action |
| --- | --- |
| Worker lock unavailable | Close the other process using the same data root; don't delete a live lock to bypass ownership |
| API HTTP error | Verify endpoint, key, provider quota and function-calling support |
| Budget reached | Break the task down, reduce attachments or deliberately raise the per-task budget |
| Browser approval expired | Re-read the current page and create a new task; don't replay an old selector |
| UNKNOWN send | Inspect the real conversation before any repeat |
| Missed schedule | Create a new future occurrence; old times are not replayed |

Cancellation is cooperative. An in-progress HTTP request finishes or times out
before cancellation takes effect. It cannot undo an external action. Document
parsing and Windows UI calls are not hard-isolated subprocesses; a hung adapter
can require restarting the app.

## Upload to GitHub

Prefer Git, which respects `.gitignore`. Drag-and-drop uploads do not enforce
ignore rules. For manual uploads, build an allowlisted source archive:

```powershell
python scripts/check_static.py
ruff check . --no-cache
python scripts/build_release.py
```

`dist/personal-agent-assistant-source.zip` contains code, public configuration
templates and documentation. It excludes all data, credentials, screenshots,
IDE configuration and Python caches. Inspect its contents before publication.
An archive is source delivery, not a standalone executable installer.

## Verification before real use

This reconstruction did not launch the UI, call a model, create live schedules,
connect to a browser, or send messages. Before actual use, separately validate:
startup, provider tool calling, document upload/download, schedule timing,
approval rejection/expiry, cancellation, restart recovery, then each external
adapter on a controlled account. Do not claim success rates, uptime or commercial
readiness without those results. The repository intentionally contains no
fabricated performance numbers or screenshots.
