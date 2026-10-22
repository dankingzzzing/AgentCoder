import copy
import io
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zipfile import ZipFile

from agentcoder.continuation import continue_run
from agentcoder.delivery import make_bundle
from agentcoder.errors import AgentError
from agentcoder.evidence import build_evidence
from agentcoder.providers import Reply, ToolCall
from agentcoder.state import RunStore, WorkflowState
from agentcoder.web import create_server
from agentcoder.workflow import WorkflowRunner

ORIGINAL = 'from pathlib import Path\n\ndef write_text_file(path, text):\n    Path(path).write_text(text, encoding="utf-8")\n\n'
FIXED = 'from pathlib import Path\n\ndef write_text_file(path, text):\n    target = Path(path)\n    target.parent.mkdir(parents=True, exist_ok=True)\n    target.write_text(text, encoding="utf-8")\n'
BASE_TESTS = '''import tempfile
import unittest
from pathlib import Path
from solution import write_text_file
class OriginalTests(unittest.TestCase):
    def test_existing_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'message.txt'
            write_text_file(path, 'hello')
            self.assertEqual(path.read_text(encoding='utf-8'), 'hello')
'''
EXTRA_TESTS = BASE_TESTS.replace('OriginalTests', 'NewTests').replace('test_existing_folder', 'test_nested_folder').replace("Path(folder) / 'message.txt'", "Path(folder) / 'new' / 'nested' / 'message.txt'")


class ContractProvider:
    def __init__(self):
        self.inputs = []

    def stream_complete(self, messages, tools, on_text, **options):
        value = json.loads(messages[-1]['content'])
        name = options['tool_choice']
        self.inputs.append((name, value))
        if name == 'submit_code':
            result = {'solution':FIXED, 'tests':EXTRA_TESTS, 'explanation':'为新增目录契约补充测试。'}
        elif name == 'submit_review':
            result = {'decision':'revise' if value['actual_tests']['passed'] is False else 'approve',
                      'issues':[], 'summary':'依据实际测试交接。'}
        else:
            result = {'solution':FIXED, 'explanation':'创建父目录，保持历史测试不变。'}
        return Reply(calls=[ToolCall('call', name, json.dumps(result))])


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = RunStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def parent(self):
        parent = WorkflowState.create('实现 write_text_file(path, text)，按 UTF-8 写入文件。', mode='llm', workdir=str(self.root))
        parent.artifacts = {'solution.py':ORIGINAL, 'test_solution.py':BASE_TESTS}
        parent.baseline_tests = BASE_TESTS
        parent.status, parent.phase = 'completed', 'done'
        parent.nodes = {'coding':'completed', 'review':'completed', 'repair':'skipped'}
        self.store.save(parent)
        return parent

    def test_old_source_is_reviewed_before_repair_and_all_original_tests_are_preserved(self):
        parent = self.parent()
        before = copy.deepcopy(parent.public())
        provider = ContractProvider()
        child = continue_run(parent, '父目录不存在时自动创建。', allow_exec=True)
        WorkflowRunner(self.store, provider=provider).run(child)
        self.assertEqual(child.status, 'completed', child.error)
        self.assertNotEqual(child.id, parent.id)
        self.assertEqual(child.parent_run_id, parent.id)
        self.assertEqual(child.origin, 'continue')
        self.assertEqual(provider.inputs[0][1]['initial_source'], ORIGINAL)
        reviews = [value for name, value in provider.inputs if name == 'submit_review']
        self.assertEqual(reviews[0]['solution'], ORIGINAL)
        self.assertEqual([r['tests']['status'] for r in child.reviews], ['failed', 'passed'])
        self.assertEqual(child.reviews[-1]['tests']['tests_run'], 2)
        self.assertEqual(child.cycle, 1)
        self.assertEqual(child.baseline_tests, EXTRA_TESTS)
        self.assertEqual((self.store.directory(child.id)/'test_inherited_01.py').read_text(encoding='utf-8'), BASE_TESTS)
        self.assertEqual(child.inherited_tests[0]['source'], BASE_TESTS)
        self.assertEqual(before, parent.public())
        self.assertEqual(self.store.load(parent.id).public(), before)
        self.assertEqual(build_evidence(child)['acceptance'], 'verified')

    def test_second_generation_and_download_include_every_frozen_test_file(self):
        parent = self.parent()
        first = continue_run(parent, '父目录不存在时创建。', allow_exec=True)
        WorkflowRunner(self.store, provider=ContractProvider()).run(first)
        self.assertEqual(first.status, 'completed', first.error)
        second = continue_run(first, '保留上一轮写文件行为。', allow_exec=True)
        WorkflowRunner(self.store, provider=ContractProvider()).run(second)
        self.assertEqual(second.parent_run_id, first.id)
        self.assertEqual(len(second.inherited_tests), 2)
        self.assertEqual(second.reviews[-1]['tests']['tests_run'], 3)
        with ZipFile(io.BytesIO(make_bundle(second))) as bundle:
            self.assertEqual(bundle.read('test_inherited_01.py').decode(), BASE_TESTS)
            self.assertEqual(bundle.read('test_inherited_02.py').decode(), EXTRA_TESTS)
            self.assertIn(first.id, bundle.read('report.md').decode())
            folder = self.root/'export';folder.mkdir()
            bundle.extractall(folder)
        from agentcoder.testing import execute_tests
        self.assertEqual(execute_tests(folder, allowed=True)['tests_run'], 3)
        self.assertTrue(execute_tests(folder, allowed=True)['passed'])

    def test_static_mode_does_not_claim_tests_passed_or_force_a_repair(self):
        child = continue_run(self.parent(), '创建父目录。')
        WorkflowRunner(self.store, provider=ContractProvider()).run(child)
        self.assertFalse(child.allow_exec)
        self.assertEqual(child.reviews[-1]['tests']['status'], 'not_run')
        self.assertEqual(build_evidence(child)['acceptance'], 'unverified')
        self.assertEqual(child.cycle, 0)

    def test_inherited_source_and_tests_reject_tampering_and_legacy_states_still_load(self):
        parent = self.parent()
        child = continue_run(parent, '创建父目录。')
        bad = copy.deepcopy(child);bad.initial_source = FIXED
        with self.assertRaises(AgentError):bad.validate()
        bad = copy.deepcopy(child);bad.inherited_tests[0]['source'] += '\n# changed'
        with self.assertRaises(AgentError):bad.validate()
        legacy = parent.public()
        for field in ('parent_run_id', 'origin', 'initial_source_from', 'inherited_tests'):
            legacy.pop(field)
        WorkflowState(**legacy).validate()
        parent.status = 'failed'
        with self.assertRaises(AgentError):continue_run(parent, 'more')
        with self.assertRaises(AgentError):continue_run(self.parent(), 'more', mode='demo')

    @contextmanager
    def web(self):
        server = create_server(self.root, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        with urlopen(base+'/api/config') as response:
            token = json.load(response)['csrf_token']
        def post(path, payload, stream=False):
            request = Request(base+path, data=json.dumps(payload).encode(), headers={'Content-Type':'application/json', 'X-Agent-Token':token})
            with urlopen(request, timeout=10) as response:
                return [json.loads(line) for line in response][-1]['data'] if stream else json.load(response)
        try:
            with patch('agentcoder.web.LLMProvider.check_connection', return_value={}):
                post('/api/settings', {'api_key':'test-key', 'model':'test-model', 'base_url':'https://example.com/v1', 'remember':False})
            yield base, post
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2)

    def test_web_continue_uses_saved_parent_and_cannot_replace_it_with_client_source(self):
        parent = self.parent()
        original = (self.store.directory(parent.id)/'state.json').read_bytes()
        with self.web() as (base, post), patch('agentcoder.web.LLMProvider', return_value=ContractProvider()):
            with self.assertRaises(HTTPError):post('/api/continue', {'parent_run_id':parent.id, 'task':'more', 'initial_source':FIXED})
            with self.assertRaises(HTTPError):post('/api/resume', {'id':parent.id})
            final = post('/api/continue', {'parent_run_id':parent.id, 'task':'自动创建父目录。', 'allow_exec':True}, stream=True)
            self.assertEqual(final['state']['parent_run_id'], parent.id)
            self.assertEqual(final['state']['status'], 'completed')
            self.assertEqual(final['evidence']['tests_run'], 2)
            self.assertEqual((self.store.directory(parent.id)/'state.json').read_bytes(), original)
            with urlopen(base+'/api/history') as response:
                rows = json.load(response)['runs']
            self.assertEqual(rows[0]['parent_run_id'], parent.id)
            self.assertTrue(rows[0]['has_source'])


if __name__ == '__main__':
    unittest.main()
