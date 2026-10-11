import json
import tempfile
import threading
import unittest
from pathlib import Path

from agentcoder import demo
from agentcoder.agents import CodingAgent, ReviewAgent, RepairAgent
from agentcoder.errors import AgentError, ProviderError
from agentcoder.graph import default_graph, validate_graph
from agentcoder.report import make_report
from agentcoder.state import WorkflowState, RunStore
from agentcoder.workflow import WorkflowRunner, AgentRegistry


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = RunStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def registry(self, coding=None, review=None, repair=None):
        return AgentRegistry().register("coding", coding or CodingAgent()).register("review", review or ReviewAgent()).register("repair", repair or RepairAgent())

    def state(self, **options):
        return WorkflowState.create(demo.TASK, "demo", **options)

    def test_complete_three_agent_collaboration_preserves_tests_and_real_evidence(self):
        state = WorkflowRunner(self.store).run(self.state(allow_exec=True))
        self.assertEqual(state.status, "completed")
        self.assertEqual(state.cycle, 1)
        self.assertEqual(set(state.nodes.values()), {"completed"})
        self.assertEqual([m["sender"] for m in state.messages], ["coding", "review", "repair", "review"])
        self.assertEqual([r["tests"]["status"] for r in state.reviews], ["failed", "passed"])
        self.assertEqual(state.reviews[-1]["tests"]["tests_run"], 8)
        self.assertEqual(state.artifacts["test_solution.py"], demo.TESTS)
        self.assertEqual(state.baseline_tests, demo.TESTS)
        self.assertNotEqual(state.messages[0]["payload"]["code_sha256"], state.messages[2]["payload"]["code_sha256"])
        self.assertEqual(state.messages[0]["payload"]["tests_sha256"], state.messages[2]["payload"]["tests_sha256"])
        restored = self.store.load(state.id)
        self.assertEqual(restored.public(), state.public())
        report = (self.store.directory(state.id)/"report.md").read_text(encoding="utf-8")
        self.assertIn("solution.py:12", report)
        self.assertIn("failed", report)
        self.assertIn("passed", report)

    def test_clean_branch_skips_repair_and_no_exec_is_not_a_fake_pass(self):
        state = WorkflowRunner(self.store).run(self.state(scenario="clean"))
        self.assertEqual(state.status, "completed")
        self.assertEqual(state.nodes["repair"], "skipped")
        self.assertEqual(state.cycle, 0)
        self.assertEqual(len(state.messages), 2)
        self.assertEqual(state.reviews[-1]["tests"]["status"], "not_run")
        self.assertIsNone(state.reviews[-1]["tests"]["passed"])
        self.assertIn("not_run", make_report(state))

    def test_repair_limit_stops_without_claiming_success(self):
        class UnchangedRepair:
            def run(self, state, context):
                return {"solution": demo.BUGGY, "explanation": "no effective repair"}
        state = WorkflowRunner(self.store, registry=self.registry(repair=UnchangedRepair())).run(self.state(max_cycles=2))
        self.assertEqual(state.status, "exhausted")
        self.assertEqual(state.cycle, 2)
        self.assertEqual(len(state.reviews), 3)
        self.assertFalse(state.reviews[-1]["approved"])
        self.assertIn("未解决问题", make_report(state))

    def test_checkpoint_resume_does_not_repeat_completed_coding(self):
        calls = []
        class CountCoding(CodingAgent):
            def run(self, state, context):
                calls.append("coding")
                return super().run(state, context)
        class FailedReview:
            def run(self, state, context):
                raise ProviderError("transient review failure")
        state = self.state()
        WorkflowRunner(self.store, registry=self.registry(coding=CountCoding(), review=FailedReview())).run(state)
        self.assertEqual(state.status, "failed")
        self.assertEqual(state.phase, "review")
        restored = self.store.load(state.id)
        WorkflowRunner(self.store, registry=self.registry(coding=CountCoding())).run(restored)
        self.assertEqual(restored.status, "completed")
        self.assertEqual(calls, ["coding"])
        self.assertEqual(restored.baseline_tests, demo.TESTS)

    def test_cancel_and_resume_preserve_a_valid_checkpoint(self):
        cancel = threading.Event()
        class CancelCoding(CodingAgent):
            def run(self, state, context):
                cancel.set()
                return super().run(state, context)
        state = self.state()
        WorkflowRunner(self.store, registry=self.registry(coding=CancelCoding()), cancelled=cancel).run(state)
        self.assertEqual(state.status, "cancelled")
        self.assertEqual(state.phase, "coding")
        restored = self.store.load(state.id)
        WorkflowRunner(self.store).run(restored)
        self.assertEqual(restored.status, "completed")

    def test_custom_graph_and_registered_observer_are_executed(self):
        observed = []
        class Observer:
            def run(self, state, context):
                observed.append(state.reviews[-1]["approved"])
                return {"summary": "final audit finished"}
        graph = default_graph()
        graph["nodes"][1]["next"] = "audit"
        graph["nodes"].append({"id":"audit", "agent":"audit", "kind":"observer", "next":"done"})
        registry = self.registry().register("audit", Observer())
        state = WorkflowRunner(self.store, registry=registry).run(self.state(scenario="clean", graph=graph))
        self.assertEqual(state.status, "completed")
        self.assertEqual(observed, [True])
        self.assertEqual(state.nodes["audit"], "completed")
        self.assertEqual(state.messages[-1]["sender"], "audit")

    def test_invalid_graph_state_id_and_frozen_tests_are_rejected(self):
        for cycles in (0, 6, True, "3"):
            with self.assertRaises(AgentError):
                self.state(max_cycles=cycles)
        graph = default_graph()
        graph["nodes"][0]["next"] = "missing"
        with self.assertRaises(AgentError):
            validate_graph(graph)
        with self.assertRaises(AgentError):
            self.store.directory("../secret")
        state = self.state()
        state.baseline_tests = "original"
        state.artifacts["test_solution.py"] = "changed"
        with self.assertRaises(AgentError):
            self.store.save(state)

    def test_corrupted_checkpoint_is_not_used(self):
        state = self.state()
        self.store.save(state)
        (self.store.directory(state.id)/"state.json").write_text('{"invalid":true}')
        with self.assertRaises(AgentError):
            self.store.load(state.id)
