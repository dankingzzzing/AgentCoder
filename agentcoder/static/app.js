"use strict";
const el=id=>document.getElementById(id);
const roleLabels={coding:"编码 Agent",review:"审查 Agent",repair:"修复 Agent"};
const roles={coding:"交付实现与原始测试",review:"核对源码证据与测试结果",repair:"修复代码，保留原始测试"};
const statuses={pending:"等待",running:"执行中",completed:"完成",failed:"失败",skipped:"跳过"};
let config=null,token="",runId=null,state=null,report="",busy=false,lastSeq=0;

function message(text,error=false){el("status").textContent=text;el("status").classList.toggle("error",error);}
function setBusy(value){
  busy=value;
  for(const id of ["run","new-run","task","source","source-path","pick-source","read-source","scenario","max-cycles","allow-exec","resume","settings"])el(id).disabled=value||(id==="run"&&!config?.configured);
  el("cancel").hidden=!value;el("cancel").disabled=false;
  el("resume").hidden=value||!["failed","cancelled","running"].includes(state?.status);
  if(!value)el("task").readOnly=config?.mode==="demo";
}
function graph(value){
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
  const item=document.createElement("li"),title=document.createElement("strong"),detail=document.createElement("span");
  title.textContent=`${String(event.seq).padStart(2,"0")} · ${roleLabels[event.agent]||event.agent||"工作流"}`;
  detail.textContent=event.message;
  if(event.kind==="workflow_error")item.classList.add("failed");
  item.append(title,detail);el("trace").append(item);el("trace").scrollTop=el("trace").scrollHeight;
  el("stage").textContent=event.message;
  if(event.kind==="node_start")updateNode(event.agent,"running");
  if(event.kind==="node_done")updateNode(event.agent,"completed");
  if(event.kind==="workflow_error")updateNode(event.agent,"failed");
  el("cycle").textContent=`修复 ${event.cycle}/${state?.max_cycles||3} 轮`;
}
function showState(value,text){
  state=value;runId=value.id;report=text||report;
  graph(value);el("trace").replaceChildren();lastSeq=0;
  value.events.forEach(appendEvent);
  el("resume").hidden=busy||!(["failed","cancelled","running"].includes(value.status));
  if(text){
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
  config={...config,...value};
  el("mode").textContent=config.mode==="demo"?"离线教学演示 · 三个规则 Agent（非 LLM）":`真实模型 · ${config.model}`;
  el("model").value=config.model;el("base-url").value=config.base_url;el("api-style").value=config.api_style||"auto";
  el("remember").checked=config.remembered!==false||!config.configured;
  el("demo-options").hidden=config.mode!=="demo";el("source-section").hidden=config.mode==="demo";
  if(config.mode==="demo"){el("task").value=config.demo_task;el("task").readOnly=true;el("source").value="";el("allow-exec").checked=true;}
  else{el("task").readOnly=false;if(!el("task").value)el("task").value=config.demo_task;}
  setBusy(false);
}
async function saveSettings(demo){
  for(const id of ["verify","use-demo","close-settings"])el(id).disabled=true;
  el("settings-status").textContent=demo?"正在进入演示…":"正在验证模型和工具调用…";el("settings-status").classList.remove("error");
  try{
    const payload={demo,remember:el("remember").checked};
    if(!demo)Object.assign(payload,{api_key:el("api-key").value.trim(),model:el("model").value.trim(),base_url:el("base-url").value.trim(),api_style:el("api-style").value});
    applyConfig(await post("/api/settings",payload));el("api-key").value="";el("model-dialog").close();
    message(demo?"已进入固定教学演示，点击开始协作。":"模型验证成功，填写需求后开始协作。");
  }catch(error){el("settings-status").textContent=error.message;el("settings-status").classList.add("error");if(error.needsSettings)el("api-key").required=true;}
  finally{for(const id of ["verify","use-demo","close-settings"])el(id).disabled=false;}
}
async function frames(path,payload){
  const response=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-Agent-Token":token},body:JSON.stringify(payload)});
  if(!response.ok){const data=await response.json();throw new Error(data.error||"运行失败");}
  const reader=response.body.getReader(),decoder=new TextDecoder();let buffer="",result=null;
  const dispatch=line=>{
    if(!line.trim())return;const frame=JSON.parse(line);
    if(frame.type==="run")showState(frame.data);
    else if(frame.type==="event")appendEvent(frame.data);
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
  setBusy(true);message(resume?"正在恢复失败节点…":"正在开始多 Agent 协作…");el("resume").hidden=true;
  if(!resume){el("results").hidden=true;report="";el("trace").replaceChildren();lastSeq=0;}
  const started=Date.now();el("elapsed").textContent="0 秒";
  const clock=setInterval(()=>el("elapsed").textContent=Math.floor((Date.now()-started)/1000)+" 秒",1000);
  try{
    const result=await frames(resume?"/api/resume":"/api/run",resume?{id:runId}:{task:el("task").value,initial_source:el("source").value,max_cycles:Number(el("max-cycles").value),allow_exec:el("allow-exec").checked,scenario:el("scenario").value});
    showState(result.state,result.report);
    const last=result.state.reviews.at(-1)?.tests;
    message(result.state.status==="completed"?`工作流完成，最终测试状态：${last?.status||"未运行"}。`:result.state.error||"达到修复上限，仍有未解决问题。",result.state.status!=="completed");
    el("results").scrollIntoView({behavior:"smooth",block:"start"});
  }catch(error){message(error.message,true);if(runId)el("resume").hidden=false;}
  finally{clearInterval(clock);setBusy(false);await history();}
}
async function source(native){
  try{
    const value=await post(native?"/api/pick":"/api/source",native?{}:{path:el("source-path").value});
    if(value.cancelled){message("已取消选择。");return;}
    el("source-path").value=value.path;el("source").value=value.content;message("已读取本地模块，原文件不会被修改。");
  }catch(error){message(error.message,true);}
}
async function history(){
  try{
    const response=await fetch("/api/history"),value=await response.json();el("history").replaceChildren();
    if(!value.runs.length){el("history").textContent="还没有运行记录。";return;}
    for(const row of value.runs){const button=document.createElement("button"),title=document.createElement("strong"),detail=document.createElement("span");
      title.textContent=row.task.slice(0,80);detail.textContent=`${row.status} · ${row.id.slice(0,8)} · 修复 ${row.cycle} 轮`;
      button.append(title,detail);button.disabled=busy||row.active;button.addEventListener("click",async()=>{
        try{const response=await fetch("/api/runs/"+row.id),loaded=await response.json();if(!response.ok)throw new Error(loaded.error);
          showState(loaded.state,loaded.report);message("已打开运行记录，可查看交付文件和交接消息。");el("results").scrollIntoView({behavior:"smooth"});
        }catch(error){message(error.message,true);}});el("history").append(button);}
  }catch(error){message(error.message,true);}
}
el("settings").addEventListener("click",openSettings);el("close-settings").addEventListener("click",()=>el("model-dialog").close());
el("model-dialog").addEventListener("cancel",event=>{if(!config?.configured)event.preventDefault();});
el("model-form").addEventListener("submit",event=>{event.preventDefault();saveSettings(false);});el("use-demo").addEventListener("click",()=>saveSettings(true));
el("run-form").addEventListener("submit",event=>{event.preventDefault();run();});el("resume").addEventListener("click",()=>run(true));
el("cancel").addEventListener("click",async()=>{try{if(runId){await post("/api/cancel",{id:runId});message("已请求停止，等待当前操作退出并保存检查点。");el("cancel").disabled=true;}}catch(error){message(error.message,true);}});
el("new-run").addEventListener("click",()=>{runId=null;state=null;report="";el("results").hidden=true;el("resume").hidden=true;el("trace").replaceChildren();lastSeq=0;
  graph({graph:{nodes:[{id:"coding",agent:"coding"},{id:"review",agent:"review"},{id:"repair",agent:"repair"}]},nodes:{coding:"pending",review:"pending",repair:"pending"},cycle:0,max_cycles:Number(el("max-cycles").value)});
  el("stage").textContent="等待开始新的协作。";el("elapsed").textContent="0 秒";message("已新建运行，保留模型配置。");});
el("pick-source").addEventListener("click",()=>source(true));el("read-source").addEventListener("click",()=>source(false));el("refresh-history").addEventListener("click",history);
document.querySelectorAll("[data-tab]").forEach(button=>button.addEventListener("click",()=>{for(const id of ["report","code","tests","messages"])el(id).hidden=id!==button.dataset.tab;document.querySelectorAll("[data-tab]").forEach(b=>b.classList.toggle("active",b===button));}));
el("download").addEventListener("click",()=>{const url=URL.createObjectURL(new Blob([report],{type:"text/markdown;charset=utf-8"})),a=document.createElement("a");a.href=url;a.download="agentcoder-report-"+(runId?.slice(0,8)||"run")+".md";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
(async()=>{try{const response=await fetch("/api/config"),value=await response.json();if(!response.ok)throw new Error(value.error);token=value.csrf_token;applyConfig(value);
  graph({graph:{nodes:[{id:"coding",agent:"coding"},{id:"review",agent:"review"},{id:"repair",agent:"repair"}]},nodes:{coding:"pending",review:"pending",repair:"pending"},cycle:0,max_cycles:3});
  if(!value.configured){el("settings-status").textContent=value.bootstrap_error||"验证成功后开始协作，也可以使用离线演示。";openSettings();}else message("已恢复本机模型配置，可直接开始协作。");await history();
}catch(error){message(error.message,true);}})();
