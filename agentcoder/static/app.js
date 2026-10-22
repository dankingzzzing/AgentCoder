"use strict";
const el=id=>document.getElementById(id);
const roleLabels={coding:"编码 Agent",review:"审查 Agent",repair:"修复 Agent",audit:"交付验收（规则）"};
const roles={coding:"交付实现与原始测试",review:"核对源码证据与测试结果",repair:"修复代码，保留原始测试",audit:"核对冻结测试与最终交付"};
const statuses={pending:"等待",running:"执行中",completed:"完成",failed:"失败",skipped:"跳过"};
let config=null,token="",runId=null,state=null,report="",busy=false,lastSeq=0,visibleStep=0;
let activeController=null,cancelRequested=false,cancelTimer=null;

function message(text,error=false){el("status").textContent=text;el("status").classList.toggle("error",error);}
function setBusy(value){
  busy=value;
  for(const id of ["run","new-run","pick-source","read-source","scenario","example","max-cycles","allow-exec","static-only","resume","continue-run","settings","edit-workflow","use-workspace","pick-workspace","known-workspaces","create-file"])el(id).disabled=value||(id==="run"&&!config?.configured);
  for(const id of ["source","source-path","workspace-path","new-filename","new-file-content"])el(id).readOnly=value;
  el("task").readOnly=value||Boolean(iterationBase)||config?.mode==="demo";el("iteration-request").readOnly=value;
  for(const button of el("workspace-files").querySelectorAll("button"))button.disabled=value;
  for(const button of el("history").querySelectorAll("button"))button.disabled=value||button.dataset.locked==="true";
  for(const link of el("history").querySelectorAll("a"))link.setAttribute("aria-disabled",String(value||link.dataset.locked==="true"));
  el("download-bundle").disabled=value;
  el("cancel").hidden=!value;el("cancel").disabled=value&&cancelRequested;el("cancel").textContent=cancelRequested?"正在停止…":"停止运行";
  el("resume").hidden=value||!["failed","cancelled","running"].includes(state?.status);
  el("continue-run").hidden=value||!state?.artifacts?.["solution.py"]||!["completed","exhausted"].includes(state?.status);
  updateRunSummary();
}
function graph(value){
  drawRoutes(value.graph);
  el("graph").replaceChildren();
  for(const node of value.graph.nodes){
    const item=document.createElement("div");item.className="node";item.dataset.node=node.id;item.dataset.state=value.nodes[node.id]||"pending";
    const title=document.createElement("strong"),description=document.createElement("span"),badge=document.createElement("span");
    title.textContent=roleLabels[node.agent]||node.agent;description.textContent=roles[node.agent]||"扩展节点";
    badge.className="node-state";badge.textContent=statuses[value.nodes[node.id]]||value.nodes[node.id];
    item.append(title,description,document.createElement("br"),badge);el("graph").append(item);
  }
  el("cycle").textContent=`修复 ${value.cycle}/${value.max_cycles} 轮`;
}
function updateNode(id,status){
  const node=[...el("graph").children].find(n=>n.dataset.node===id);
  if(node){node.dataset.state=status;node.querySelector(".node-state").textContent=statuses[status]||status;}
}
function appendEvent(event){
  if(event.seq<=lastSeq)return;lastSeq=event.seq;
  el("stage").textContent=event.message;
  if(event.kind==="model_progress")el("model-progress").textContent=`请求 ${event.data.attempt||1} · ${event.data.transport==="buffered"?"普通响应":"流式响应"} · ${event.data.signals||0} 次响应信号 · ${event.data.public_chars||0} 个公开交付字符`;
  if(event.kind==="node_start")updateNode(event.agent,"running");
  if(event.kind==="node_done")updateNode(event.agent,"completed");
  if(event.kind==="workflow_error")updateNode(event.agent,"failed");
  if(event.kind==="workflow_cancelled")updateNode(event.agent,"pending");
  el("cycle").textContent=`修复 ${event.cycle}/${state?.max_cycles||3} 轮`;
  if(["activity","model_progress"].includes(event.kind))return;
  const trace=el("trace"),follow=trace.scrollHeight-trace.scrollTop-trace.clientHeight<40;
  const item=document.createElement("li"),badge=document.createElement("span"),body=document.createElement("div"),title=document.createElement("strong"),detail=document.createElement("span");
  badge.className="trace-index";badge.textContent=String(++visibleStep).padStart(2,"0");
  title.textContent=roleLabels[event.agent]||event.agent||"工作流";detail.textContent=event.message;
  item.dataset.eventSeq=event.seq;if(event.kind==="workflow_error")item.classList.add("failed");
  body.append(title,detail);item.append(badge,body);trace.append(item);
  while(trace.children.length>200)trace.firstElementChild.remove();
  if(follow)trace.scrollTop=trace.scrollHeight;
}
function showState(value,text,evidence=null){
  state=value;runId=value.id;report=text||report;
  graph(value);el("trace").replaceChildren();lastSeq=0;visibleStep=0;
  value.events.forEach(appendEvent);
  showDrafts({drafts:value.drafts,summary:value.progress?.summary,progress:value.progress,status:value.status});
  el("resume").hidden=busy||!(["failed","cancelled","running"].includes(value.status));
  el("continue-run").hidden=busy||!value.artifacts["solution.py"]||!["completed","exhausted"].includes(value.status);
  if(text){
    renderEvidence(evidence);
    el("results").hidden=false;renderMarkdown(el("report"),text);
    el("code").textContent=value.artifacts["solution.py"]||"尚未交付代码。";
    el("tests").textContent=value.baseline_tests||"尚未交付测试。";
    el("messages").textContent=JSON.stringify(value.messages,null,2);
    const tests=value.reviews.at(-1)?.tests;
    el("metrics").textContent=`${value.status} · ${value.mode==="demo"?"离线规则 Agent":"真实模型"} · ${value.messages.length} 条交接消息 · 测试 ${tests?.status||"未运行"}`;
  }
}
async function post(path,data){
  const response=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-Agent-Token":token},body:JSON.stringify(data)});
  const value=await response.json();
  if(!response.ok){const error=new Error(value.error||"请求失败");error.needsSettings=value.needs_settings;throw error;}
  return value;
}
function openSettings(){
  el("close-settings").hidden=!config?.configured;el("api-key").value="";
  el("api-key").required=!config?.api_key_configured;
  el("api-key").placeholder=config?.api_key_configured?"密钥已保存，留空沿用":"填写 API Key";
  if(!el("model-dialog").open)el("model-dialog").showModal();
}
function applyConfig(value){
  const previousMode=config?.mode;
  config={...config,...value};
  if(previousMode==="demo"&&config.mode==="llm")el("static-only").checked=true;
  el("mode").textContent=config.mode==="demo"?"离线教学演示 · 三个规则 Agent（非 LLM）":`真实模型 · ${config.model}`;
  el("model").value=config.model;el("base-url").value=config.base_url;el("api-style").value=config.api_style||"auto";
  el("stream-mode").value=config.stream_mode||"auto";el("stream-timeout").value=config.stream_idle_timeout||120;
  el("remember").checked=config.remembered!==false||!config.configured;
  el("demo-options").hidden=config.mode!=="demo"||Boolean(iterationBase);el("source-section").hidden=config.mode==="demo"||Boolean(iterationBase);
  if(config.mode==="demo"&&!iterationBase){el("task").value=config.demo_cases?.find(item=>item.id===el("example").value)?.task||config.demo_task;el("task").readOnly=true;el("source").value="";if(config.configured)el("allow-exec").checked=true;}
  else{el("task").readOnly=false;if(!el("task").value)el("task").value=config.demo_task;}
  setBusy(false);
}
async function saveSettings(demo){
  for(const id of ["verify","use-demo","close-settings"])el(id).disabled=true;
  el("settings-status").textContent=demo?"正在进入演示…":"正在验证模型和工具调用…";el("settings-status").classList.remove("error");
  try{
    const payload={demo,remember:el("remember").checked};
    if(!demo)Object.assign(payload,{api_key:el("api-key").value.trim(),model:el("model").value.trim(),base_url:el("base-url").value.trim(),api_style:el("api-style").value,stream_mode:el("stream-mode").value,stream_idle_timeout:el("stream-timeout").value});
    applyConfig(await post("/api/settings",payload));el("api-key").value="";el("model-dialog").close();
    message(demo?"已进入固定教学演示，点击开始协作。":"模型验证成功，填写需求后开始协作。");
  }catch(error){el("settings-status").textContent=error.message;el("settings-status").classList.add("error");if(error.needsSettings)el("api-key").required=true;}
  finally{for(const id of ["verify","use-demo","close-settings"])el(id).disabled=false;}
}
async function frames(path,payload){
  const response=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-Agent-Token":token},body:JSON.stringify(payload),signal:activeController.signal});
  if(!response.ok){const data=await response.json();throw new Error(data.error||"运行失败");}
  const reader=response.body.getReader(),decoder=new TextDecoder();let buffer="",result=null;
  const dispatch=line=>{
    if(!line.trim())return;const frame=JSON.parse(line);
    if(frame.type==="run")showState(frame.data);
    else if(frame.type==="event")appendEvent(frame.data);
    else if(frame.type==="preview")showDrafts(frame.data);
    else if(frame.type==="result")result=frame.data;
  };
  try{
    while(true){const {done,value}=await reader.read();buffer+=decoder.decode(value||new Uint8Array(),{stream:!done});let index;
      while((index=buffer.indexOf("\n"))>=0){dispatch(buffer.slice(0,index));buffer=buffer.slice(index+1);}if(done)break;}
    if(buffer.trim())dispatch(buffer);
  }finally{await reader.cancel().catch(()=>{});reader.releaseLock();}
  if(!result)throw new Error("连接提前结束，可在最近运行中打开检查点并继续。");
  return result;
}
async function run(resume=false){
  if(!config?.configured){openSettings();return;}
  if(!resume&&iterationBase&&config.mode!=="llm"){message("继续迭代新增需求需要真实模型，请先切换模型设置。",true);openSettings();return;}
  cancelRequested=false;activeController=new AbortController();if(!resume){runId=null;state=null;showDrafts(null);el("model-progress").textContent="";}
  setBusy(true);message(resume?"正在恢复失败节点…":"正在开始多 Agent 协作…");el("resume").hidden=true;
  if(!resume){el("results").hidden=true;report="";el("trace").replaceChildren();lastSeq=0;visibleStep=0;}
  const started=Date.now();el("elapsed").textContent="0 秒";
  const clock=setInterval(()=>el("elapsed").textContent=Math.floor((Date.now()-started)/1000)+" 秒",1000);
  try{
    const payload={task:iterationBase?el("iteration-request").value:el("task").value,max_cycles:Number(el("max-cycles").value),allow_exec:el("allow-exec").checked,graph:workflowGraph,workspace_id:currentWorkspace?.id};
    if(iterationBase)payload.parent_run_id=iterationBase.id;
    else Object.assign(payload,{initial_source:config.mode==="demo"?"":el("source").value,scenario:el("scenario").value,example:el("example").value});
    const result=await frames(resume?"/api/resume":iterationBase?"/api/continue":"/api/run",resume?{id:runId}:payload);
    showState(result.state,result.report,result.evidence);
    const last=result.state.reviews.at(-1)?.tests;
    message(result.state.status==="completed"?`工作流完成，最终测试状态：${last?.status||"未运行"}。`:result.state.error||"达到修复上限，仍有未解决问题。",!["completed","cancelled"].includes(result.state.status));
    el("results").scrollIntoView({behavior:"smooth",block:"start"});
  }catch(error){message(error.name==="AbortError"?"已停止接收，已完成交付和检查点会保留。":error.message,error.name!=="AbortError");if(runId){try{const response=await fetch("/api/runs/"+runId),loaded=await response.json();if(response.ok)showState(loaded.state,loaded.report,loaded.evidence);}catch{}}}
  finally{clearInterval(clock);el("elapsed").textContent=((Date.now()-started)/1000).toFixed(1)+" 秒";clearTimeout(cancelTimer);cancelTimer=null;activeController=null;cancelRequested=false;setBusy(false);await history();}
}
async function source(native){
  try{
    const value=await post(native?"/api/pick":"/api/source",native?{}:{path:el("source-path").value});
    if(value.cancelled){message("已取消选择。");return;}
    clearIteration();el("source-path").value=value.path;el("source").value=value.content;updateRunSummary();message(value.kind==="tests"?`识别到回归测试，将为 ${value.module} 生成实现，原测试冻结不修改。`:"已读取本地模块，原文件不会被修改。");
  }catch(error){message(error.message,true);}
}
async function history(){
  try{
    const response=await fetch("/api/history"),value=await response.json();renderHistory(value.runs);
  }catch(error){message(error.message,true);}
}
el("settings").addEventListener("click",openSettings);el("close-settings").addEventListener("click",()=>el("model-dialog").close());
el("model-dialog").addEventListener("cancel",event=>{if(!config?.configured)event.preventDefault();});
el("model-form").addEventListener("submit",event=>{event.preventDefault();saveSettings(false);});el("use-demo").addEventListener("click",()=>saveSettings(true));
el("run-form").addEventListener("submit",event=>{event.preventDefault();run();});el("resume").addEventListener("click",()=>run(true));
el("cancel").addEventListener("click",async()=>{if(!busy)return;cancelRequested=true;el("cancel").disabled=true;el("cancel").textContent="正在停止…";message("正在停止模型请求与测试，保存已完成交付。");
  cancelTimer=setTimeout(()=>activeController?.abort(),2000);
  if(!runId){activeController?.abort();return;}
  try{await post("/api/cancel",{id:runId});}catch{activeController?.abort();}});
function resetNewRun(){runId=null;state=null;report="";el("results").hidden=true;el("resume").hidden=true;el("trace").replaceChildren();lastSeq=0;
  visibleStep=0;clearIteration(true);el("continue-run").hidden=true;el("demo-options").hidden=config?.mode!=="demo";
  showDrafts(null);
  stopReplay();previewWorkflow();
  el("stage").textContent="等待开始新的协作。";el("elapsed").textContent="0 秒";message("已新建运行，保留模型配置。");}
el("new-run").addEventListener("click",resetNewRun);
el("pick-source").addEventListener("click",()=>source(true));el("read-source").addEventListener("click",()=>source(false));el("refresh-history").addEventListener("click",history);
el("continue-run").addEventListener("click",()=>prepareIteration(runId));
for(const id of ["source","max-cycles","allow-exec","static-only"])el(id).addEventListener("input",updateRunSummary);
document.querySelectorAll("[data-tab]").forEach(button=>button.addEventListener("click",()=>{stopReplay();for(const id of ["report","code","tests","messages","evidence"])el(id).hidden=id!==button.dataset.tab;document.querySelectorAll("[data-tab]").forEach(b=>b.classList.toggle("active",b===button));}));
el("download").addEventListener("click",()=>{const url=URL.createObjectURL(new Blob([report],{type:"text/markdown;charset=utf-8"})),a=document.createElement("a");a.href=url;a.download="agentcoder-report-"+(runId?.slice(0,8)||"run")+".md";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
(async()=>{try{const response=await fetch("/api/config"),value=await response.json();if(!response.ok)throw new Error(value.error);token=value.csrf_token;applyConfig(value);
  await setupWorkspace();await setupStudio();previewWorkflow();
  if(!value.configured){el("settings-status").textContent=value.bootstrap_error||"验证成功后开始协作，也可以使用离线演示。";openSettings();}else message("已恢复本机模型配置，可直接开始协作。");await history();
}catch(error){message(error.message,true);}})();
