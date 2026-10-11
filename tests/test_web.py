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
from agentcoder.settings_store import SettingsStore
from agentcoder.web import create_server


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
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
