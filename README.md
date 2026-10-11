# Agentic URL Shortener

A runnable, dependency-free Python prototype for the interview assignment. It pairs a URL shortener with a governed engineering workflow engine. Workflow workers are deterministic local agents, so the prototype runs without model credentials; the worker interface can later be replaced with real model-backed agents without changing the scheduler or policy boundaries.

## Requirements and run

- Python 3.11 or newer
- No third-party runtime packages

From the repository root:

```powershell
python -m shortener.cli serve
```

The default server listens on `http://127.0.0.1:8000` and stores state in `data/shortener.sqlite3`. Override `SHORTENER_DB`, `SHORTENER_HOST`, `SHORTENER_PORT`, `SHORTENER_BASE_URL`, `SHORTENER_MAX_URL_LENGTH`, and `ORCHESTRATOR_MAX_RETRIES` as needed. `Ctrl+C` stops the server.

Run a workflow demonstration from another shell:

```powershell
python -m shortener.cli demo greenfield
python -m shortener.cli demo brownfield --approve
python -m shortener.cli demo ambiguous --inject-failure implementation
```

Without `--approve`, the run pauses at the release approval gate. With `--approve`, the CLI records a human approval and completes. The three scenarios highlight a new service, a change to an existing service, and an ambiguous requirement with visible questions and declared assumptions.

Run validation:

```powershell
python -m unittest discover -s tests -v
```

## URL API

All request bodies are JSON objects. The shortener only accepts absolute HTTP(S) URLs and rejects URL credentials. Codes are 4–12 ASCII alphanumeric characters; absent a custom code, an opaque random code is generated. Optional expirations must be future ISO-8601 timestamps with a timezone.

| Method | Path | Behavior |
| --- | --- | --- |
| `POST` | `/api/links` | Create a link. Body: `{"url":"https://example.com/path","custom_code":"demo42","expires_at":"2027-01-01T00:00:00Z"}`. `custom_code` and `expires_at` are optional. |
| `GET` | `/{code}` | Record a click and redirect with HTTP 302. |
| `GET` | `/api/links/{code}/analytics` | Return link details, total clicks, and daily counts. |
| `GET` | `/healthz` | Liveness check. |
| `GET` | `/api/metrics` | Link, click, and workflow counts and workflow completion rate. |

Example:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/links `
  -ContentType 'application/json' -Body '{"url":"https://example.com"}'
```

The redirect endpoint intentionally accepts only a path code, not a destination parameter, preventing open-redirect behavior. Analytics retain referrer and user-agent strings in click records, truncated to bounded lengths; deployments that consider those fields personal data should redact or disable them and define a retention policy.

## Workflow API and execution model

| Method | Path | Behavior |
| --- | --- | --- |
| `POST` | `/api/workflows` | Start a scenario. Body: `{"scenario":"greenfield"}`. Optional `inject_failure` demonstrates a transient retry. |
| `GET` | `/api/workflows/{run_id}` | Read tasks, outputs, lineage events, and reliability metrics. |
| `POST` | `/api/workflows/{run_id}/approve` | Record human approval and resume release readiness. Body: `{}`. |
| `POST` | `/api/workflows/{run_id}/rollback` | Safe-stop and roll back the selected task and descendants. Body: `{"task_id":"implementation"}`. |
| `POST` | `/api/workflows/{run_id}/replan` | Replace an upstream task output and invalidate/recompute its descendants. Body: `{"task_id":"requirements","output":{"normalized_requirement":"..."}}`. |

The graph is `requirements → design → (implementation, tests, documentation) → validation → release approval`. Independent implementation, testing, and documentation work runs concurrently and joins at validation. Every attempt, retry, output, approval, failure, and rollback is persisted in SQLite. Failed tasks get at most `ORCHESTRATOR_MAX_RETRIES` retries; the workflow stops safely after exhaustion. High-impact release readiness cannot complete without a recorded approval. Replanning retains the replacement decision and invalidates dependent results before recomputing. Rollback marks a task and its descendants as rolled back and puts the run into a terminal safe-stop state.

The API workflow start always pauses for approval; approval is a separate authenticated-by-deployment human action. The demo CLI offers `--approve` to show a complete happy path. The prototype has no authentication or authorization layer, so do not expose it to an untrusted network.

## Architecture

```text
HTTP handler / CLI
        ├── ShortenerService ── SQLite (links, clicks)
        └── WorkflowEngine ──── SQLite (runs, task state, event lineage)
                 ├── dependency scheduler / parallel worker pool
                 ├── local role workers (analyst, architect, engineer, QA, reviewer)
                 └── policy gate, retries, replan, rollback, human approval
```

SQLite transactions protect individual persistence operations; foreign keys and a click index keep records consistent and analytics practical for a local prototype. The design intentionally avoids embedding credentials or invoking real model APIs. For production, add identity-based access controls, rate limiting, secrets management, model/tool sandboxing, durable queue workers, migrations, backups, retention controls, abuse/malware screening, and horizontally scalable storage.

## Risks, trade-offs, and limitations

- Codes use random generation and database uniqueness; a collision returns a validation error instead of silently replacing a mapping.
- SQLite is a single-node store suitable for a prototype, not high-write multi-region traffic.
- Worker outputs are structured demonstration artifacts rather than generated source patches. They show stateful orchestration and governance, not autonomous code editing.
- Ambiguous requirements remain explicit in the requirements output. The demo records assumptions so a reviewer can judge them; a real deployment should block affected work until an authorized person resolves material questions.
- The approval route has no built-in identity check. The server defaults to loopback; add authentication before any network exposure.
- Click metadata is bounded but not anonymized, and there is no retention job. Apply privacy requirements before collecting real user traffic.
- Release approval governs the workflow's release state; it does not deploy code or mutate production systems.

## Test coverage

The test suite covers URL validation, collisions, expiry, redirect recording, analytics, scenario DAG completion, required approval, retry limits, re-planning invalidation, rollback safe-stop, and an HTTP create/redirect/analytics round trip.
