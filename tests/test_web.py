import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from agentcoder.agents import CodingAgent
from agentcoder.errors import ProviderError
from agentcoder.graph import audited_graph, default_graph
from agentcoder.settings_store import SettingsStore
from agentcoder.web import create_server


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name).resolve()
        self.start()
    def start(self):
        with patch.dict("os.environ",{},clear=True):
            self.server=create_server(self.root,port=0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base=f"http://127.0.0.1:{self.server.server_port}"
        self.config=self.get("/api/config")
        self.token=self.config["csrf_token"]
    def stop(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
    def tearDown(self):
        self.stop();self.temp.cleanup()
    def get(self,path):
        with urlopen(self.base+path,timeout=5) as response:
            return json.load(response)
    def request(self,path,value,token=None):
        return Request(self.base+path,data=json.dumps(value).encode(),headers={"Content-Type":"application/json","X-Agent-Token":self.token if token is None else token})
    def post(self,path,value):
        with urlopen(self.request(path,value),timeout=5) as response:
            return json.load(response)
    def test_verified_settings_restore_after_restart_and_never_return_key(self):
        value={"api_key":"not-a-real-secret","model":"test","base_url":"https://provider.example/v1","remember":True}
        with patch("agentcoder.web.LLMProvider.check_connection",return_value={}):
            self.post("/api/settings",value)
        self.assertNotIn(value["api_key"],json.dumps(self.get("/api/config")))
        self.assertTrue(self.get("/api/config")["configured"])
        self.stop();self.start()
        self.assertTrue(self.config["configured"])
        self.assertEqual(self.config["model"],"test")
        self.assertEqual(SettingsStore(self.root).load()[0].api_key,value["api_key"])
        with patch("agentcoder.web.LLMProvider.check_connection",side_effect=ProviderError("bad key",http_status=401)):
            with self.assertRaises(HTTPError) as error:
                self.post("/api/settings",{**value,"api_key":"bad"})
        self.assertTrue(json.load(error.exception)["needs_settings"])
        self.assertEqual(SettingsStore(self.root).load()[0].api_key,value["api_key"])
    def test_demo_stream_contains_three_agents_and_history_survives_restart(self):
        self.post("/api/settings",{"demo":True,"remember":True})
        with urlopen(self.request("/api/run",{"allow_exec":True}),timeout=5) as response:
            frames=[json.loads(line) for line in response]
        self.assertEqual(frames[0]["type"],"run")
        self.assertEqual(frames[-1]["type"],"result")
        state=frames[-1]["data"]["state"]
        self.assertEqual(state["status"],"completed")
        self.assertEqual(state["reviews"][-1]["tests"]["tests_run"],8)
        self.stop();self.start()
        self.assertEqual(self.get("/api/history")["runs"][0]["id"],state["id"])
        self.assertEqual(self.get("/api/runs/"+state["id"])["state"]["nodes"]["repair"],"completed")
    def test_live_frames_and_cancellation_before_node_completion(self):
        self.post("/api/settings",{"demo":True,"remember":False})
        gate=threading.Event()
        original=CodingAgent.run
        def delayed(agent,state,context):
            if not gate.wait(5):raise AssertionError("no live frame received")
            return original(agent,state,context)
        try:
            with patch.object(CodingAgent,"run",delayed):
                with urlopen(self.request("/api/run",{}),timeout=5) as response:
                    first=json.loads(response.readline())
                    self.assertEqual(first["type"],"run")
                    self.assertFalse(gate.is_set())
                    self.post("/api/cancel",{"id":first["data"]["id"]})
                    gate.set()
                    rest=[json.loads(line) for line in response]
            self.assertEqual(rest[-1]["data"]["state"]["status"],"cancelled")
            run_id=first["data"]["id"]
            with urlopen(self.request("/api/resume",{"id":run_id}),timeout=5) as response:
                final=[json.loads(line) for line in response][-1]
            self.assertEqual(final["data"]["state"]["status"],"completed")
        finally:gate.set()
    def test_csrf_private_source_and_oversized_request(self):
        with self.assertRaises(HTTPError) as error:
            with urlopen(self.request("/api/settings",{"demo":True},token="wrong"),timeout=5):pass
        self.assertEqual(error.exception.code,403)
        self.post("/api/settings",{"demo":True,"remember":False})
        (self.root/".agentcoder").mkdir(exist_ok=True)
        private=self.root/".agentcoder"/"secret.py"
        private.write_text("value=1")
        with self.assertRaises(HTTPError):self.post("/api/source",{"path":str(private)})
        with self.assertRaises(HTTPError) as error:self.post("/api/run",{"task":"x"*200000})
        self.assertEqual(error.exception.code,413)

    def test_graph_preview_save_restart_and_historical_run_snapshot(self):
        original=self.get("/api/graph")["graph"]
        candidate=audited_graph()
        self.assertTrue(self.post("/api/graph/validate",{"graph":candidate})["valid"])
        self.assertEqual(self.get("/api/graph")["graph"],original)
        self.post("/api/graph",{"graph":candidate})
        self.post("/api/settings",{"demo":True,"remember":True})
        with urlopen(self.request("/api/run",{"example":"prices","allow_exec":True}),timeout=5) as response:
            final=[json.loads(line) for line in response][-1]["data"]
        self.assertEqual(final["state"]["graph"],candidate)
        self.assertEqual(final["state"]["nodes"]["delivery"],"completed")
        self.assertEqual(final["evidence"]["acceptance"],"verified")
        self.post("/api/graph",{"graph":default_graph()})
        self.assertEqual(self.get("/api/runs/"+final["state"]["id"])["state"]["graph"],candidate)
        self.stop();self.start()
        self.assertEqual(self.get("/api/graph")["graph"],default_graph())

    def test_invalid_graph_binding_or_route_is_rejected_before_starting(self):
        bad=default_graph();bad["nodes"][0]["agent"]="unregistered"
        for path in ("/api/graph","/api/graph/validate"):
            with self.assertRaises(HTTPError) as error:self.post(path,{"graph":bad})
            self.assertEqual(error.exception.code,400)
        self.post("/api/settings",{"demo":True,"remember":False})
        with self.assertRaises(HTTPError):self.post("/api/run",{"graph":bad})
        self.assertEqual(self.get("/api/history")["runs"],[])
        self.assertEqual(self.get("/api/graph")["graph"],default_graph())

    def test_delivery_endpoint_downloads_just_the_requested_run(self):
        import io
        from zipfile import ZipFile
        self.post("/api/settings",{"demo":True,"remember":False})
        with urlopen(self.request("/api/run",{"allow_exec":True}),timeout=5) as response:
            final=[json.loads(line) for line in response][-1]["data"]
        with urlopen(self.base+"/api/runs/"+final["state"]["id"]+"/bundle",timeout=5) as response:
            self.assertEqual(response.headers["Content-Type"],"application/zip")
            self.assertIn("attachment",response.headers["Content-Disposition"])
            with ZipFile(io.BytesIO(response.read())) as archive:
                self.assertEqual(json.loads(archive.read("state.json"))["id"],final["state"]["id"])
                self.assertNotIn(".env",archive.namelist())

    def test_workspace_creation_output_location_and_old_history_after_switch(self):
        chosen=self.root/'local-code';chosen.mkdir()
        value=self.post('/api/workspaces/select',{'path':str(chosen)})
        source=self.post('/api/workspaces/create-file',{'workspace_id':value['id'],'name':'我的代码.py','content':'value=1\n'})
        self.assertEqual(source['path'],str(chosen/'我的代码.py'))
        with self.assertRaises(HTTPError):self.post('/api/workspaces/create-file',{'workspace_id':value['id'],'name':'我的代码.py','content':'overwrite'})
        self.post('/api/settings',{'demo':True,'remember':True})
        with urlopen(self.request('/api/run',{'workspace_id':value['id'],'allow_exec':True}),timeout=5) as response:
            final=[json.loads(line) for line in response][-1]['data']
        run_id=final['state']['id']
        self.assertEqual(final['state']['workdir'],str(chosen))
        self.assertTrue((chosen/'.agentcoder/runs'/run_id/'solution.py').exists())
        self.assertEqual((chosen/'我的代码.py').read_text(encoding='utf-8'),'value=1\n')
        self.post('/api/workspaces/select',{'path':str(self.root)})
        self.stop();self.start()
        self.assertEqual(self.get('/api/runs/'+run_id)['state']['status'],'completed')
        self.assertEqual(self.get('/api/history')['runs'][0]['workdir'],str(chosen))

    def test_native_directory_selection_and_cancel_do_not_change_current_directory(self):
        chosen=self.root/'native-choice';chosen.mkdir()
        with patch('agentcoder.web.pick_local_path',return_value=str(chosen)) as picker:
            selected=self.post('/api/workspaces/pick',{})
            self.assertEqual(picker.call_args.args[0],'directory')
        with patch('agentcoder.web.pick_local_path',return_value=None):
            self.assertTrue(self.post('/api/workspaces/pick',{})['cancelled'])
        self.assertEqual(self.get('/api/workspaces')['id'],selected['id'])

    def test_workspace_path_traversal_is_rejected_and_page_contains_no_personal_footer(self):
        current=self.get('/api/workspaces')
        with self.assertRaises(HTTPError):self.post('/api/workspaces/create-file',{'workspace_id':current['id'],'name':'../escaped.py','content':'x=1'})
        self.assertFalse((self.root.parent/'escaped.py').exists())
        with urlopen(self.base+'/',timeout=5) as response:html=response.read().decode()
        self.assertIn('<footer><span>AgentCoder</span>',html)

    def test_legacy_settings_restore_with_new_network_defaults(self):
        with patch('agentcoder.web.LLMProvider.check_connection',return_value={}):
            self.post('/api/settings',{'api_key':'test-key','model':'test','base_url':'https://example.com/v1','remember':True})
        path=self.root/'.agentcoder/web-settings.json'
        saved=json.loads(path.read_text(encoding='utf-8'));saved['config'].pop('stream_mode');saved['config'].pop('stream_idle_timeout')
        path.write_text(json.dumps(saved),encoding='utf-8')
        self.stop();self.start()
        self.assertTrue(self.config['configured'])
        self.assertEqual(self.config['stream_mode'],'auto')
        self.assertEqual(self.config['stream_idle_timeout'],120)
