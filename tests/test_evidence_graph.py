import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from agentcoder.agents import CodingAgent, RepairAgent, ReviewAgent
from agentcoder.delivery import make_bundle
from agentcoder.errors import AgentError, ProviderError
from agentcoder.evaluation import OptimisticReview, run_evaluation
from agentcoder.evidence import build_evidence
from agentcoder.fixtures import CASES, get_case
from agentcoder.graph import BUILTIN_KINDS, GraphStore, audited_graph, default_graph, validate_graph
from agentcoder.report import make_report
from agentcoder.state import RunStore, WorkflowState
from agentcoder.workflow import AgentRegistry, WorkflowRunner


class EvidenceGraphTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = RunStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def state(self, **options):
        return WorkflowState.create(get_case().task, allow_exec=True, **options)

    def registry(self, review=None):
        return AgentRegistry().register("coding", CodingAgent()).register("review", review or ReviewAgent()).register("repair", RepairAgent())

    def test_each_teaching_task_runs_real_tests_and_keeps_every_revision(self):
        for example in CASES:
            with self.subTest(example=example):
                state = WorkflowRunner(self.store).run(self.state(example=example))
                evidence = build_evidence(state)
                self.assertEqual(evidence["acceptance"], "verified")
                self.assertEqual([r["tests"]["status"] for r in state.reviews], ["failed", "passed"])
                self.assertEqual(evidence["tests_run"], 8)
                self.assertEqual([r["index"] for r in state.revisions], [0, 1])
                self.assertEqual(state.revisions[0]["source"], get_case(example).buggy)
                self.assertIn("--- revision-0/solution.py", evidence["revisions"][1]["diff"])
                self.assertEqual(state.reviews[0]["code_sha256"], state.revisions[0]["code_sha256"])
                self.assertEqual(self.store.load(state.id).revisions, state.revisions)
                self.assertTrue((self.store.directory(state.id)/"evidence.json").is_file())

    def test_checkpoint_rejects_changed_source_hash_and_frozen_test_hash(self):
        state = WorkflowRunner(self.store).run(self.state())
        for key in ("source", "code_sha256", "tests_sha256"):
            with self.subTest(key=key):
                tampered = copy.deepcopy(state)
                tampered.revisions[0][key] += "changed"
                with self.assertRaises(AgentError):
                    self.store.save(tampered)
        state.artifacts["solution.py"] += "# changed outside a node\n"
        with self.assertRaises(AgentError):
            state.validate()

    def test_controller_vetoes_registered_review_that_ignores_failed_tests(self):
        state = WorkflowRunner(self.store, registry=self.registry(OptimisticReview())).run(self.state())
        self.assertEqual(state.status, "completed")
        self.assertFalse(state.reviews[0]["approved"])
        self.assertEqual(state.cycle, 1)
        self.assertEqual(build_evidence(state)["acceptance"], "verified")

    def test_observer_cannot_change_live_artifacts(self):
        class MutatingObserver:
            def run(self, state, context):
                state.artifacts["solution.py"] = "silently corrupted"
                return {"summary": "corrupted"}
        registry = self.registry().register("audit", MutatingObserver())
        state = WorkflowRunner(self.store, registry=registry).run(self.state(graph=audited_graph()))
        self.assertEqual(state.status, "failed")
        self.assertIn("只能读取", state.error)
        self.assertEqual(state.artifacts["solution.py"], get_case().fixed)
        self.assertEqual(build_evidence(state)["acceptance"], "unfinished")
        state.validate()

    def test_observer_can_emit_public_progress_without_mutating_its_copy(self):
        class LoggingObserver:
            def run(self, state, context):
                context.emit("observation_progress", "audit underway", agent=state.phase)
                return {"summary": "read-only observation"}
        state = WorkflowRunner(self.store, registry=self.registry().register("audit", LoggingObserver())).run(self.state(graph=audited_graph()))
        self.assertEqual(state.status, "completed")
        self.assertIn("observation_progress", [e["kind"] for e in state.events])

    def test_old_checkpoint_resumes_without_inventing_lost_versions(self):
        class FailedReview:
            def run(self, state, context):
                raise ProviderError("interrupted")
        state = WorkflowRunner(self.store, registry=self.registry(FailedReview())).run(self.state())
        checkpoint = state.public()
        checkpoint.pop("revisions")
        checkpoint.pop("example")
        self.store.write(state.id, "state.json", json.dumps(checkpoint))
        loaded = self.store.load(state.id)
        WorkflowRunner(self.store).run(loaded)
        self.assertEqual(loaded.status, "completed")
        self.assertEqual(loaded.revisions[0]["origin"], "legacy_checkpoint")
        self.assertEqual(sum(m["kind"] == "code_ready" for m in loaded.messages), 1)
        self.assertEqual(loaded.revisions[0]["source"], get_case().buggy)

    def test_graph_rejects_bypass_recoding_dead_nodes_and_observer_loops(self):
        invalid = []
        graph = default_graph(); graph["nodes"][0]["next"] = "done"; invalid.append(graph)
        graph = default_graph(); graph["nodes"][2]["next"] = "coding"; invalid.append(graph)
        graph = default_graph(); graph["nodes"].append({"id":"dead", "agent":"audit", "kind":"observer", "next":"done"}); invalid.append(graph)
        graph = audited_graph(); graph["nodes"][-1]["next"] = "delivery"; invalid.append(graph)
        graph = default_graph(); graph["nodes"][1]["next"] = "repair"; invalid.append(graph)
        graph = default_graph(); graph["nodes"][0]["agent"] = "repair"; invalid.append(graph)
        for graph in invalid:
            with self.subTest(graph=graph), self.assertRaises(AgentError):
                validate_graph(graph, bindings=BUILTIN_KINDS)

    def test_two_role_graph_stops_rejected_code_without_claiming_budget_was_used(self):
        graph = default_graph()
        graph["nodes"] = graph["nodes"][:2]
        graph["nodes"][1]["revise"] = "done"
        state = WorkflowRunner(self.store).run(self.state(graph=graph))
        self.assertEqual(state.status, "exhausted")
        self.assertEqual(state.cycle, 0)
        self.assertEqual(build_evidence(state)["acceptance"], "unfinished")
        self.assertIn("未配置修复分支", make_report(state))

    def test_saved_graph_validation_preserves_the_last_good_definition(self):
        graph_store = GraphStore(self.root)
        graph_store.save(audited_graph())
        bad = default_graph(); bad["nodes"][2]["next"] = "coding"
        with self.assertRaises(AgentError):
            graph_store.save(bad)
        self.assertEqual(graph_store.load(), audited_graph())

    def test_bundle_exports_one_run_and_can_reproduce_its_actual_tests(self):
        (self.root/".env").write_text("PRIVATE_NEIGHBOR_SECRET")
        state = WorkflowRunner(self.store).run(self.state(example="words"))
        payload = make_bundle(state)
        with ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(set(archive.namelist()), {"solution.py","test_solution.py","report.md","state.json","evidence.json","workflow.json","README.md"})
            self.assertNotIn(b"PRIVATE_NEIGHBOR_SECRET", b"".join(archive.read(name) for name in archive.namelist()))
            self.assertEqual(json.loads(archive.read("evidence.json"))["acceptance"], "verified")
            self.assertEqual(archive.read("solution.py").decode(), get_case("words").fixed)

    def test_static_only_report_does_not_claim_verified_acceptance(self):
        state = WorkflowRunner(self.store).run(WorkflowState.create(get_case().task, scenario="clean"))
        self.assertEqual(build_evidence(state)["acceptance"], "unverified")
        self.assertIn("尚未获得实际测试通过", make_report(state))

    def test_disconnect_after_final_commit_keeps_completed_delivery(self):
        def disconnect(event):
            if event["kind"] == "workflow_done":
                raise BrokenPipeError("client closed")
        state = self.state()
        with self.assertRaises(BrokenPipeError):
            WorkflowRunner(self.store, on_event=disconnect).run(state)
        restored = self.store.load(state.id)
        self.assertEqual(restored.status, "completed")
        self.assertEqual(build_evidence(restored)["acceptance"], "verified")
        self.assertTrue((self.store.directory(state.id)/"evidence.json").is_file())

    def test_disconnect_after_code_commit_resumes_without_an_extra_coding_message(self):
        def disconnect(event):
            if event["kind"] == "node_done" and event["agent"] == "coding":
                raise ConnectionResetError("client closed")
        state = self.state()
        with self.assertRaises(ConnectionResetError):
            WorkflowRunner(self.store, on_event=disconnect).run(state)
        restored = self.store.load(state.id)
        self.assertEqual(restored.status, "cancelled")
        self.assertEqual(restored.phase, "review")
        WorkflowRunner(self.store).run(restored)
        self.assertEqual(sum(m["kind"] == "code_ready" for m in restored.messages), 1)
        self.assertEqual(len(restored.revisions), 2)

    def test_ten_scenario_acceptance_includes_expected_failures_and_recovery(self):
        result = run_evaluation(self.root)
        self.assertTrue(result["all_passed"], result)
        self.assertEqual(result["passed"], 10)
        rows = {row["id"]: row for row in result["cases"]}
        self.assertEqual(rows["repair_budget"]["status"], "exhausted")
        self.assertEqual(rows["static_only"]["acceptance"], "unverified")
        self.assertTrue(rows["checkpoint_resume"]["recovered_without_recoding"])
