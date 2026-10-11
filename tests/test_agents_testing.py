import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentcoder import demo
from agentcoder.agents import CodingAgent, ReviewAgent, validate_review
from agentcoder.analysis import analyze_source
from agentcoder.errors import ProviderError
from agentcoder.inputs import read_source
from agentcoder.providers import Reply, ToolCall
from agentcoder.state import WorkflowState
from agentcoder.testing import execute_tests


class StubProvider:
    def __init__(self, name, value):
        self.name,self.value = name,value
        self.inputs=[]
    def stream_complete(self, messages, tools, on_text, **options):
        self.inputs.append((messages,tools,options))
        return Reply(calls=[ToolCall("c1",self.name,json.dumps(self.value))])


class Context:
    def activity(self, role):
        pass
    def inspect(self, source, state):
        return {"syntax_ok": True, "findings": []}, {"status":"not_run","passed":None,"tests_run":None,"output":"","exit_code":None}


class AgentTestingTests(unittest.TestCase):
    def test_quote_alignment_supports_multiline_and_corrects_unique_positions(self):
        source="value=1\n\ndef read(raw):\n    return json.loads(raw)\n"
        issue={"severity":"medium","line":1,"title":"missing import","evidence":"def read(raw):\n    return json.loads(raw)",
               "detail":"NameError when called","fix":"import json","test":"call read"}
        result={"summary":"checked","decision":"revise","issues":[issue]}
        validate_review(result,source)
        self.assertEqual(issue["line"],3)

    def test_formatted_evidence_keeps_only_verified_code(self):
        issue={"severity":"medium","line":2,"title":"missing import",
               "evidence":"第 2 行：`return json.loads(raw)`；错误是 `NameError: json`。",
               "detail":"fails when called","fix":"import json","test":"invoke"}
        validate_review({"summary":"checked","decision":"revise","issues":[issue]},"def read(raw):\n    return json.loads(raw)\n")
        self.assertEqual(issue["evidence"],"return json.loads(raw)")
        issue["evidence"]="第 2 行：return json.loads(raw)；实际错误为 NameError。"
        validate_review({"summary":"checked","decision":"revise","issues":[issue]},"def read(raw):\n    return json.loads(raw)\n")
        self.assertEqual(issue["evidence"],"return json.loads(raw)")

    def test_undefined_global_candidates_respect_imports_and_local_arguments(self):
        missing=analyze_source("def read(raw):\n    return json.loads(raw)\n","x.py")
        self.assertTrue(any(f["rule"]=="undefined-global" for f in missing["findings"]))
        defined=analyze_source("import json\ndef read(raw):\n    return json.loads(raw)\n","x.py")
        local=analyze_source("def read(json):\n    return json\n","x.py")
        self.assertFalse(any(f["rule"]=="undefined-global" for f in defined["findings"]+local["findings"]))
    def test_coding_keeps_selected_source_and_passes_distinct_prompt(self):
        provider=StubProvider("submit_code",{"solution":demo.FIXED,"tests":demo.TESTS,"explanation":"candidate"})
        state=WorkflowState.create(demo.TASK,"llm",initial_source=demo.BUGGY)
        result=CodingAgent(provider).run(state,Context())
        self.assertEqual(result["solution"],demo.BUGGY)
        self.assertEqual(provider.inputs[0][2]["tool_choice"],"submit_code")
        self.assertIn("编码 Agent",provider.inputs[0][0][0]["content"])

    def test_review_rejects_invented_source_evidence(self):
        issue={"severity":"high","line":12,"title":"unsafe","evidence":"os.system(raw)",
               "detail":"trigger","fix":"parse","test":"regression"}
        provider=StubProvider("submit_review",{"summary":"check","decision":"revise","issues":[issue]})
        state=WorkflowState.create(demo.TASK,"llm")
        state.artifacts={"solution.py":demo.BUGGY,"test_solution.py":demo.TESTS}
        state.baseline_tests=demo.TESTS
        with self.assertRaises(ProviderError):
            ReviewAgent(provider).run(state,Context())

    def test_actual_test_failure_overrides_model_approval(self):
        provider=StubProvider("submit_review",{"summary":"check","decision":"approve","issues":[]})
        class FailedContext(Context):
            def inspect(self,source,state):
                static,tests=super().inspect(source,state)
                tests.update(status="failed",passed=False)
                return static,tests
        state=WorkflowState.create(demo.TASK,"llm")
        state.artifacts={"solution.py":demo.BUGGY}
        result=ReviewAgent(provider).run(state,FailedContext())
        self.assertFalse(result["approved"])

    def test_source_selection_is_read_only_and_excludes_private_paths(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            original=root/"source.py"
            original.write_text(demo.BUGGY)
            self.assertEqual(read_source(str(original),root)["content"],demo.BUGGY)
            self.assertEqual(original.read_text(),demo.BUGGY)
            (root/".agentcoder").mkdir()
            private=root/".agentcoder"/"secret.py"
            private.write_text("secret")
            from agentcoder.errors import AgentError
            with self.assertRaises(AgentError):
                read_source(str(private),root)

    def test_timeout_and_no_tests_are_explicit_failures(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            (root/"test_solution.py").write_text("while True: pass\n")
            result=execute_tests(root,allowed=True,timeout=.2)
            self.assertEqual(result["status"],"timeout")
            self.assertFalse(result["passed"])
            (root/"test_solution.py").write_text("value = 1\n")
            result=execute_tests(root,allowed=True)
            self.assertEqual(result["status"],"no_tests")
            self.assertFalse(result["passed"])

    def test_execution_filters_private_environment(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            (root/"test_solution.py").write_text('import os,unittest\nclass T(unittest.TestCase):\n def test_environment(self):\n  self.assertNotIn("AGENTCODER_SECRET_TEST",os.environ)\n')
            with patch.dict(os.environ,{"AGENTCODER_SECRET_TEST":"not-a-real-key"}):
                result=execute_tests(root,allowed=True)
            self.assertTrue(result["passed"])
