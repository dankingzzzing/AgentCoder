import io
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from agentcoder import demo
from agentcoder.agents import CodingAgent
from agentcoder.config import Config
from agentcoder.errors import AgentError, Cancelled, ProviderError
from agentcoder.providers import LLMProvider, Reply, ToolCall
from agentcoder.agents import ReviewAgent,RepairAgent
from agentcoder.workflow import AgentRegistry
from agentcoder.delivery import make_bundle
from zipfile import ZipFile
from agentcoder.report import make_report
from agentcoder.state import RunStore, WorkflowState
from agentcoder.testing import execute_tests
from agentcoder.workflow import WorkflowRunner
from agentcoder.workspaces import Workspaces


class CancellationWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_cancel_interrupts_header_wait_and_partial_stream_without_waiting_for_timeout(self):
        for partial in (False, True):
            started, release = threading.Event(), threading.Event()
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_POST(self):
                    self.rfile.read(int(self.headers['Content-Length']))
                    if partial:
                        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                        self.wfile.write(b'data: {"choices":[{"delta":{"reasoning_content":"private-thought","tool_calls":[{"index":0,"id":"c1","function":{"name":"submit_code","arguments":"{\\\"solution\\\":\\\"def f():\\\\n    return 1\\\\n"}}]}}]}\n\n')
                        self.wfile.flush()
                    started.set();release.wait(5)
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
            serving=threading.Thread(target=server.serve_forever,daemon=True);serving.start()
            token=threading.Event();captured=[];signals=[];fragments=[]
            provider=LLMProvider(Config(api_key='test-key',model='test',base_url=f'http://127.0.0.1:{server.server_port}',api_style='chat_completions',max_retries=0))
            def call():
                try:provider.stream_complete([{'role':'user','content':'x'}],[],lambda text:None,cancelled=token,on_progress=signals.append,on_tool_delta=lambda name,args:fragments.append(args))
                except Exception as exc:captured.append(exc)
            worker=threading.Thread(target=call);worker.start()
            try:
                self.assertTrue(started.wait(3))
                if partial:
                    end=time.monotonic()+2
                    while not fragments and time.monotonic()<end:time.sleep(.01)
                    self.assertTrue(fragments)
                    self.assertNotIn('private-thought',json.dumps(fragments+signals))
                instant=time.monotonic();token.set();worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
                self.assertIsInstance(captured[0],Cancelled)
                self.assertLess(time.monotonic()-instant,2)
            finally:
                release.set();server.shutdown();server.server_close();serving.join(timeout=2);worker.join(timeout=2)

    def test_unterminated_final_sse_frame_is_accepted(self):
        provider=LLMProvider(Config(api_key='test',model='test',max_retries=0,api_style='chat_completions'))
        body=b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}'
        with patch.object(provider.opener,'open',return_value=io.BytesIO(body)):
            self.assertEqual(provider.stream_complete([{'role':'user','content':'x'}],[],lambda text:None).content,'ok')

    def test_timeout_and_invalid_sse_have_distinct_safe_error_codes(self):
        class TimeoutResponse(io.BytesIO):
            def __next__(self):raise TimeoutError('private transport details')
        for response, code in ((TimeoutResponse(), 'stream_timeout'),(io.BytesIO(b'data: not-json\n\n'),'sse_format')):
            provider=LLMProvider(Config(api_key='test',model='test',max_retries=0,api_style='chat_completions'))
            with patch.object(provider.opener,'open',return_value=response),self.assertRaises(ProviderError) as failure:
                provider.stream_complete([{'role':'user','content':'x'}],[],lambda text:None)
            self.assertEqual(failure.exception.code,code)
            self.assertNotIn('private transport details',str(failure.exception))

    def test_json_body_from_stream_request_works_when_gateway_ignores_stream_flag(self):
        class Response(io.BytesIO):
            headers={'Content-Type':'application/json'}
        provider=LLMProvider(Config(api_key='test',model='test',max_retries=0,api_style='chat_completions'))
        body=json.dumps({'choices':[{'message':{'content':'ok'},'finish_reason':'stop'}]}).encode()
        with patch.object(provider.opener,'open',return_value=Response(body)):
            self.assertEqual(provider.stream_complete([{'role':'user','content':'x'}],[],lambda text:None).content,'ok')

    def test_stream_failures_retry_current_node_then_use_buffered_without_committing_drafts(self):
        calls=[]
        class Provider:
            config=Config(api_key='test-key',model='test',max_retries=2)
            def stream_complete(self,*args,**options):
                calls.append('stream')
                options['on_tool_delta']('submit_code','{"solution":"partially generated')
                raise ProviderError('read timeout',code='stream_timeout',retryable=True)
            def buffered_complete(self,*args,**options):
                calls.append('buffered')
                self.assert_no_commit()
                return Reply(calls=[ToolCall('c','submit_code',json.dumps({'solution':demo.FIXED,'tests':demo.TESTS,'explanation':'public summary'}))])
        state=WorkflowState.create(demo.TASK,'llm')
        provider=Provider();provider.assert_no_commit=lambda:self.assertEqual(state.messages,[])
        context=WorkflowRunner(RunStore(self.root),provider=provider);context.state=state
        result=CodingAgent(provider).run(state,context)
        self.assertEqual(calls,['stream','stream','buffered'])
        self.assertEqual(result['solution'],demo.FIXED)
        self.assertEqual(state.messages,[])
        self.assertEqual(sum(e['kind']=='request_retry' for e in state.events),2)

    def test_partial_public_preview_and_cancellation_preserve_only_uncommitted_drafts(self):
        gate=threading.Event()
        class Provider:
            config=Config(api_key='test-key',model='test',max_retries=0)
            def stream_complete(self,*args,**options):
                options['on_tool_delta']('submit_code','{"solution":"def value():\\n    return 1\\n')
                gate.set();options['cancelled'].wait(3)
                raise Cancelled('stopped')
        state=WorkflowState.create('implement value','llm');token=threading.Event();previews=[]
        runner=WorkflowRunner(RunStore(self.root),provider=Provider(),cancelled=token,on_preview=previews.append)
        worker=threading.Thread(target=runner.run,args=(state,));worker.start()
        self.assertTrue(gate.wait(2));token.set();worker.join(timeout=2)
        self.assertEqual(state.status,'cancelled')
        self.assertEqual(state.drafts['solution.py'],'def value():\n    return 1\n')
        self.assertEqual(state.artifacts,{})
        self.assertEqual(state.messages,[])
        self.assertTrue(previews)
        self.assertIn('尚未进入审查',make_report(state))
        self.assertNotIn('- `solution.py`：最新模块',make_report(state))

    def test_cancel_kills_generated_test_process_before_its_timeout(self):
        (self.root/'test_solution.py').write_text('while True: pass\n')
        token=threading.Event();result=[]
        worker=threading.Thread(target=lambda:result.append(execute_tests(self.root,allowed=True,timeout=30,cancelled=token)));worker.start()
        time.sleep(.15);instant=time.monotonic();token.set();worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]['status'],'cancelled')
        self.assertLess(time.monotonic()-instant,3)

    def test_selected_directory_and_new_python_file_restore_and_preserve_originals(self):
        target=self.root/'selected';target.mkdir()
        workspaces=Workspaces(self.root)
        selected=workspaces.select(str(target))
        new=workspaces.create_file(selected['id'],'我的模块.py','value=7\n')
        self.assertEqual(Path(new['path']),target/'我的模块.py')
        self.assertEqual(Workspaces(self.root).get(),target)
        with self.assertRaises(AgentError):workspaces.create_file(selected['id'],'我的模块.py','changed')
        self.assertEqual((target/'我的模块.py').read_text(encoding='utf-8'),'value=7\n')
        for name in ('../escape.py','CON.py','.env.py','bad.txt','json.py','JSON.py','agentcoder.py'):
            with self.subTest(name=name),self.assertRaises(AgentError):workspaces.create_file(selected['id'],name,'x=1')
        with self.assertRaises(AgentError):workspaces.create_file('not-selected','ok.py','x=1')

    def test_run_lookup_uses_saved_directory_after_switching_workspace(self):
        target=self.root/'selected';target.mkdir();other=self.root/'other';other.mkdir()
        workspaces=Workspaces(self.root);workspaces.select(str(target))
        state=WorkflowRunner(RunStore(target)).run(WorkflowState.create(demo.TASK))
        workspaces.select(str(other))
        restored=Workspaces(self.root)
        self.assertEqual(restored.locate_run(state.id).root,target)
        self.assertEqual(restored.locate_run(state.id).load(state.id).status,'completed')
        self.assertEqual(restored.history()[0]['workdir'],str(target))

    def test_test_file_is_frozen_as_tests_and_original_import_name_gets_a_real_module(self):
        original=demo.TESTS.replace('from solution import','from calculator import')+'\n'
        inputs=[]
        class Provider:
            def stream_complete(self,messages,tools,on_text,**options):
                inputs.append(json.loads(messages[-1]['content']))
                name=options['tool_choice']
                value=({'solution':demo.FIXED,'tests':'THIS GENERATED TEST MUST NOT REPLACE USER TESTS','explanation':'implements contract'}
                       if name=='submit_code' else {'summary':'checked','decision':'approve','issues':[]})
                return Reply(calls=[ToolCall('c',name,json.dumps(value))])
        state=WorkflowState.create(demo.TASK,'llm',initial_source=original,allow_exec=True)
        self.assertEqual(state.initial_source,'')
        self.assertEqual(state.initial_tests,original)
        provider=Provider();result=WorkflowRunner(RunStore(self.root),provider=provider).run(state)
        self.assertEqual(result.status,'completed')
        self.assertEqual(result.baseline_tests,original)
        self.assertEqual(inputs[0]['initial_tests'],original)
        self.assertTrue(result.reviews[0]['tests']['passed'])
        self.assertEqual((RunStore(self.root).directory(state.id)/'calculator.py').read_text(),demo.FIXED)
        with ZipFile(io.BytesIO(make_bundle(state))) as archive:
            self.assertEqual(archive.read('calculator.py'),archive.read('solution.py'))

    def test_test_alias_cannot_shadow_standard_library_or_escape_run_directory(self):
        for target in ('json','JSON','os','../escape','CON','agentcoder'):
            with self.subTest(target=target),self.assertRaises(AgentError):
                WorkflowState.create('x',test_module=target)
