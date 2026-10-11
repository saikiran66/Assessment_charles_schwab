import unittest
from pathlib import Path

from shortener.database import Database
from shortener.orchestrator import WorkflowEngine


class WorkflowEngineTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path.cwd() / ".test-data" / f"workflow-{self._testMethodName}.sqlite3"
        self.db_path.parent.mkdir(exist_ok=True)
        self.database = Database(self.db_path)
        self.database.initialize()
        self.engine = WorkflowEngine(self.database, max_retries=1)

    def tearDown(self):
        for path in (self.db_path, Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")):
            path.unlink(missing_ok=True)

    def test_scenario_waits_for_approval_then_completes(self):
        run = self.engine.start("greenfield")
        self.assertEqual(run["status"], "awaiting_approval")
        self.assertEqual(next(t for t in run["tasks"] if t["task_id"] == "release_approval")["status"], "awaiting_approval")
        completed = self.engine.resume(run["run_id"], approve_release=True)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["metrics"]["retry_count"], 0)

    def test_transient_failure_retries_and_is_audited(self):
        run = self.engine.start("brownfield", approve_release=True, fail_once="implementation")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["metrics"]["retry_count"], 1)
        implementation = next(t for t in run["tasks"] if t["task_id"] == "implementation")
        self.assertEqual(implementation["attempts"], 2)

    def test_replan_invalidates_descendants_and_rollback_safe_stops(self):
        run = self.engine.start("ambiguous")
        replanned = self.engine.replan(run["run_id"], "requirements", {"normalized_requirement": "Clarified scope"})
        self.assertEqual(replanned["status"], "awaiting_approval")
        self.assertTrue(any(event["event_type"] == "upstream_changed_replan" for event in replanned["events"]))
        design = next(task for task in replanned["tasks"] if task["task_id"] == "design")
        self.assertEqual(design["output"]["inputs"]["requirements"]["normalized_requirement"], "Clarified scope")
        rolled = self.engine.rollback(run["run_id"], "implementation")
        self.assertEqual(rolled["status"], "rolled_back")
        self.assertTrue(any(event["event_type"] == "rollback" for event in rolled["events"]))


if __name__ == "__main__":
    unittest.main()
