"""Stateful, policy-governed workflow orchestration for engineering tasks."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import time
import uuid
from typing import Any

from .database import Database
from .service import utc_now


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    stage: str
    depends_on: tuple[str, ...]
    role: str
    high_impact: bool = False


TASKS = (
    TaskSpec("requirements", "requirements", (), "analyst"),
    TaskSpec("design", "architecture", ("requirements",), "architect"),
    TaskSpec("implementation", "implementation", ("design",), "engineer"),
    TaskSpec("tests", "testing", ("design",), "qa"),
    TaskSpec("documentation", "documentation", ("design",), "technical_writer"),
    TaskSpec("validation", "validation", ("implementation", "tests", "documentation"), "reviewer"),
    TaskSpec("release_approval", "release_readiness", ("validation",), "change_controller", True),
)
TASK_BY_ID = {task.task_id: task for task in TASKS}
SCENARIOS = {
    "greenfield": {
        "requirement": "Create a secure URL shortener with redirects, analytics, validation, and operational documentation.",
        "mode": "greenfield",
    },
    "brownfield": {
        "requirement": "Improve an existing shortener by adding click analytics and safer URL validation without breaking redirects.",
        "mode": "brownfield",
    },
    "ambiguous": {
        "requirement": "Make links safer and more useful for customers.",
        "mode": "ambiguous",
        "open_questions": [
            "What does safer mean: expiry, access control, malware screening, or all three?",
            "Which customer analytics and retention period are allowed?",
        ],
        "assumptions": ["Prototype scope means URL validation and aggregate click counts only."],
    },
}


class WorkflowError(RuntimeError):
    pass


class WorkflowEngine:
    def __init__(self, database: Database, max_retries: int = 2, max_workers: int = 4):
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self.database = database
        self.max_retries = max_retries
        self.max_workers = max(1, max_workers)

    def start(self, scenario: str, approve_release: bool = False, fail_once: str | None = None) -> dict:
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario '{scenario}'; choose from {', '.join(SCENARIOS)}")
        if fail_once is not None and fail_once not in TASK_BY_ID:
            raise ValueError("inject_failure must identify a workflow task")
        run_id = str(uuid.uuid4())
        definition = SCENARIOS[scenario]
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO workflow_runs(run_id,scenario,status,started_at,context_json) VALUES(?,?,?,?,?)",
                (run_id, scenario, "running", utc_now(), json.dumps(definition)),
            )
            for task in TASKS:
                connection.execute(
                    "INSERT INTO workflow_tasks(run_id,task_id,status) VALUES(?,?,?)",
                    (run_id, task.task_id, "pending"),
                )
        self._event(run_id, None, "run_started", {"scenario": scenario, "requirement": definition["requirement"]})
        self._drive(run_id, definition, approve_release, {fail_once} if fail_once else set())
        return self.get_run(run_id)

    def resume(self, run_id: str, approve_release: bool = False) -> dict:
        run, context = self._load_run(run_id)
        if run["status"] not in {"awaiting_approval", "running", "failed"}:
            raise WorkflowError(f"run cannot be resumed from status '{run['status']}'")
        if approve_release:
            self._event(run_id, "release_approval", "human_approval", {"approved": True})
        self._drive(run_id, context, approve_release, set())
        return self.get_run(run_id)

    def replan(self, run_id: str, task_id: str, replacement_output: dict) -> dict:
        if task_id not in TASK_BY_ID or task_id == "release_approval":
            raise WorkflowError("task_id must identify a re-plannable workflow task")
        if not isinstance(replacement_output, dict):
            raise WorkflowError("replacement output must be a JSON object")
        self._load_run(run_id)
        descendants = self._descendants(task_id)
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE workflow_tasks SET status='completed',output_json=?,error=NULL,finished_at=? WHERE run_id=? AND task_id=?",
                (json.dumps(replacement_output), now, run_id, task_id),
            )
            for dependent in descendants:
                connection.execute(
                    "UPDATE workflow_tasks SET status='pending',output_json=NULL,error=NULL,started_at=NULL,finished_at=NULL WHERE run_id=? AND task_id=?",
                    (run_id, dependent),
                )
        self._event(run_id, task_id, "upstream_changed_replan", {"invalidated_tasks": sorted(descendants)})
        run, context = self._load_run(run_id)
        self._drive(run_id, context, False, set())
        return self.get_run(run_id)

    def rollback(self, run_id: str, task_id: str) -> dict:
        if task_id not in TASK_BY_ID or task_id == "release_approval":
            raise WorkflowError("task_id must identify a rollback-capable workflow task")
        self._load_run(run_id)
        affected = {task_id} | self._descendants(task_id)
        with self.database.connect() as connection:
            for task in affected:
                connection.execute(
                    "UPDATE workflow_tasks SET status='rolled_back',output_json=NULL,error=NULL,finished_at=? WHERE run_id=? AND task_id=?",
                    (utc_now(), run_id, task),
                )
        self._event(run_id, task_id, "rollback", {"affected_tasks": sorted(affected), "safe_stop": True})
        with self.database.connect() as connection:
            connection.execute("UPDATE workflow_runs SET status='rolled_back',finished_at=? WHERE run_id=?", (utc_now(), run_id))
        return self.get_run(run_id)

    def _drive(self, run_id: str, context: dict, approve_release: bool, fail_once: set[str]) -> None:
        start_ns = time.monotonic_ns()
        with self.database.connect() as connection:
            connection.execute("UPDATE workflow_runs SET status='running',finished_at=NULL WHERE run_id=?", (run_id,))
        while True:
            tasks = self._task_rows(run_id)
            statuses = {row["task_id"]: row["status"] for row in tasks}
            if statuses.get("release_approval") == "awaiting_approval" and approve_release:
                self._complete_approval(run_id)
                continue
            if statuses.get("release_approval") == "pending":
                spec = TASK_BY_ID["release_approval"]
                if all(statuses.get(parent) == "completed" for parent in spec.depends_on):
                    if approve_release:
                        self._complete_approval(run_id)
                        continue
                    self._set_task(run_id, spec.task_id, "awaiting_approval", started=True)
                    self._event(run_id, spec.task_id, "approval_required", {"action": "release readiness", "risk": "high-impact change"})
                    break
            ready = [
                spec for spec in TASKS
                if not spec.high_impact and statuses.get(spec.task_id) == "pending"
                and all(statuses.get(parent) == "completed" for parent in spec.depends_on)
            ]
            if not ready:
                if any(status == "failed" for status in statuses.values()):
                    break
                if all(status == "completed" for status in statuses.values()):
                    break
                # A waiting approval or rolled back stage is an intentional stop.
                if any(status in {"awaiting_approval", "rolled_back"} for status in statuses.values()):
                    break
                break
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                futures = {
                    pool.submit(self._execute_with_retry, run_id, spec, context, fail_once): spec
                    for spec in ready
                }
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception:
                        pass  # Failure is persisted by _execute_with_retry; independent branches still finish.
        rows = self._task_rows(run_id)
        statuses = {row["task_id"]: row["status"] for row in rows}
        if statuses.get("release_approval") == "awaiting_approval":
            status = "awaiting_approval"
        elif statuses.get("release_approval") == "completed":
            status = "completed"
        elif any(value == "failed" for value in statuses.values()):
            status = "failed"
        elif any(value == "rolled_back" for value in statuses.values()):
            status = "rolled_back"
        else:
            status = "running"
        elapsed_ms = round((time.monotonic_ns() - start_ns) / 1_000_000, 2)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE workflow_runs SET status=?,finished_at=? WHERE run_id=?",
                (status, utc_now() if status in {"completed", "failed", "rolled_back"} else None, run_id),
            )
        self._event(run_id, None, "run_finished", {"status": status, "elapsed_ms": elapsed_ms})

    def _execute_with_retry(self, run_id: str, spec: TaskSpec, context: dict, fail_once: set[str]) -> None:
        self._set_task(run_id, spec.task_id, "running", started=True)
        for attempt in range(1, self.max_retries + 2):
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE workflow_tasks SET attempts=attempts+1 WHERE run_id=? AND task_id=?",
                    (run_id, spec.task_id),
                )
            self._event(run_id, spec.task_id, "task_attempt", {"attempt": attempt, "role": spec.role})
            try:
                if spec.task_id in fail_once:
                    fail_once.remove(spec.task_id)
                    raise RuntimeError("injected transient worker failure")
                output = self._worker_output(spec, context, self._outputs(run_id, spec.depends_on))
                self._set_task(run_id, spec.task_id, "completed", output=output, finished=True)
                self._event(run_id, spec.task_id, "task_completed", {"attempt": attempt, "artifact": output["artifact"]})
                return
            except Exception as exc:
                self._event(run_id, spec.task_id, "task_retry" if attempt <= self.max_retries else "task_failed",
                            {"attempt": attempt, "error": str(exc), "retry_limit": self.max_retries})
                if attempt > self.max_retries:
                    self._set_task(run_id, spec.task_id, "failed", error=str(exc), finished=True)
                    raise

    @staticmethod
    def _worker_output(spec: TaskSpec, context: dict, upstream: dict) -> dict:
        if spec.task_id == "requirements":
            output = {"normalized_requirement": context["requirement"], "mode": context["mode"],
                      "acceptance_criteria": ["Valid URL can be shortened", "Short code redirects", "Clicks are measurable", "Input and failure cases are handled"]}
            if context.get("open_questions"):
                output["open_questions"] = context["open_questions"]
                output["assumptions"] = context.get("assumptions", [])
        elif spec.task_id == "design":
            output = {"components": ["HTTP API", "shortener service", "SQLite repository", "workflow engine", "audit log"],
                      "decisions": ["stdlib HTTP server", "SQLite persistence", "opaque random codes", "aggregate analytics"]}
        elif spec.task_id == "implementation":
            output = {"artifacts": ["POST /api/links", "GET /{code}", "GET /api/links/{code}/analytics"],
                      "controls": ["HTTP(S)-only destinations", "credential URL rejection", "expiry validation", "parameterized SQL"]}
        elif spec.task_id == "tests":
            output = {"coverage_plan": ["URL validation", "redirect tracking", "analytics", "DAG dependencies", "approval gate", "retry and rollback"],
                      "evidence_required": ["unit test results", "API integration results"]}
        elif spec.task_id == "documentation":
            output = {"documents": ["README setup", "architecture overview", "API reference", "scenario walkthroughs", "risk and limitation register"]}
        else:  # validation
            output = {"gate_results": {"security": "pass", "requirements_traceability": "pass", "tests": "evidence required", "release_readiness": "pending human approval"},
                      "test_execution": "not run by deterministic demo workers; execute the repository test command for evidence",
                      "risks": ["SQLite is a single-node store", "local workers are deterministic and do not call an LLM", "ambiguous scope uses declared assumptions"]}
        return {"task_id": spec.task_id, "stage": spec.stage, "role": spec.role,
                "artifact": spec.task_id, "inputs": upstream, **output}

    def _outputs(self, run_id: str, task_ids: tuple[str, ...]) -> dict:
        if not task_ids:
            return {}
        placeholders = ",".join("?" for _ in task_ids)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT task_id,output_json FROM workflow_tasks WHERE run_id=? AND task_id IN ({placeholders})",
                (run_id, *task_ids),
            ).fetchall()
        return {row["task_id"]: json.loads(row["output_json"]) for row in rows if row["output_json"]}

    def _complete_approval(self, run_id: str) -> None:
        self._set_task(run_id, "release_approval", "completed", output={"approved": True, "approved_by": "human"}, finished=True)
        self._event(run_id, "release_approval", "task_completed", {"approved": True})

    def _descendants(self, task_id: str) -> set[str]:
        result = set()
        changed = True
        while changed:
            changed = False
            for spec in TASKS:
                if spec.task_id not in result and (task_id in spec.depends_on or result.intersection(spec.depends_on)):
                    result.add(spec.task_id)
                    changed = True
        return result

    def _set_task(self, run_id: str, task_id: str, status: str, output: dict | None = None,
                  error: str | None = None, started: bool = False, finished: bool = False) -> None:
        fields = ["status=?"]
        values: list[Any] = [status]
        if output is not None:
            fields.append("output_json=?")
            values.append(json.dumps(output))
        if error is not None:
            fields.append("error=?")
            values.append(error)
        if started:
            fields.append("started_at=?")
            values.append(utc_now())
        if finished:
            fields.append("finished_at=?")
            values.append(utc_now())
        values.extend([run_id, task_id])
        with self.database.connect() as connection:
            connection.execute(f"UPDATE workflow_tasks SET {','.join(fields)} WHERE run_id=? AND task_id=?", values)

    def _event(self, run_id: str, task_id: str | None, event_type: str, details: dict) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO workflow_events(run_id,task_id,event_type,occurred_at,details_json) VALUES(?,?,?,?,?)",
                (run_id, task_id, event_type, utc_now(), json.dumps(details)),
            )

    def _task_rows(self, run_id: str):
        with self.database.connect() as connection:
            return connection.execute("SELECT * FROM workflow_tasks WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()

    def _load_run(self, run_id: str):
        with self.database.connect() as connection:
            run = connection.execute("SELECT * FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise WorkflowError("workflow run not found")
        return run, json.loads(run["context_json"])

    def get_run(self, run_id: str) -> dict:
        run, _ = self._load_run(run_id)
        with self.database.connect() as connection:
            tasks = connection.execute("SELECT * FROM workflow_tasks WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
            events = connection.execute("SELECT * FROM workflow_events WHERE run_id=? ORDER BY event_id", (run_id,)).fetchall()
        task_data = []
        for row in tasks:
            item = dict(row)
            item["output"] = json.loads(item.pop("output_json")) if item["output_json"] else None
            task_data.append(item)
        event_data = []
        for row in events:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            event_data.append(item)
        attempts = sum(task["attempts"] for task in task_data)
        retry_events = sum(event["event_type"] == "task_retry" for event in event_data)
        rollback_events = sum(event["event_type"] == "rollback" for event in event_data)
        recovery_ms = []
        retry_started = {}
        for event in event_data:
            if event["event_type"] == "task_retry":
                retry_started[event.get("task_id")] = datetime.fromisoformat(event["occurred_at"])
            elif event["event_type"] == "task_completed" and event.get("task_id") in retry_started:
                recovery_ms.append((datetime.fromisoformat(event["occurred_at"]) - retry_started.pop(event["task_id"])).total_seconds() * 1000)
        return {"run_id": run_id, "scenario": run["scenario"], "status": run["status"],
                "started_at": run["started_at"], "finished_at": run["finished_at"], "tasks": task_data,
                "events": event_data, "metrics": {"task_success_rate": round(sum(t["status"] == "completed" for t in task_data) / len(task_data), 3),
                "attempts": attempts, "retry_count": retry_events, "rollback_count": rollback_events,
                "retry_frequency": round(retry_events / attempts, 3) if attempts else 0,
                "rollback_frequency": round(rollback_events / max(1, len(task_data)), 3),
                "mttr_ms": round(sum(recovery_ms) / len(recovery_ms), 2) if recovery_ms else 0,
                "end_to_end_latency_ms": _elapsed_ms(event_data)}}


def _elapsed_ms(events: list[dict]) -> float | None:
    start = next((e["occurred_at"] for e in events if e["event_type"] == "run_started"), None)
    finish = next((e["occurred_at"] for e in reversed(events) if e["event_type"] == "run_finished"), None)
    if not start or not finish:
        return None
    first = datetime.fromisoformat(start)
    last = datetime.fromisoformat(finish)
    return round((last - first).total_seconds() * 1000, 2)
