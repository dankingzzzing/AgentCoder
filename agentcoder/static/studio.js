"use strict";
let workflowGraph=null,workflowPresets={},draftGraph=null,revisionEvidence=null,replayTimer=null;
const cloneGraph=value=>JSON.parse(JSON.stringify(value));

function drawRoutes(value){
  el("routes").replaceChildren();el("workflow-label").textContent=value.name;
  for(const node of value.nodes){
    const line=document.createElement("span");
    line.textContent=`${node.id} ${node.kind==="review"?"通过":"交付"} → ${node.next}`;
    if(node.kind==="review")line.textContent+=`；未通过 → ${node.revise}`;
    el("routes").append(line);
  }
}
function previewWorkflow(){if(workflowGraph)graph({graph:workflowGraph,nodes:{},cycle:0,max_cycles:Number(el("max-cycles").value)});}
function syncDraft(){draftGraph.name=el("workflow-name").value;el("graph-json").value=JSON.stringify(draftGraph,null,2);el("workflow-status").textContent="有修改，请校验或保存。";el("workflow-status").classList.remove("error");}
function renderEditor(){
  el("workflow-name").value=draftGraph.name;el("graph-json").value=JSON.stringify(draftGraph,null,2);el("workflow-nodes").replaceChildren();
  for(const node of draftGraph.nodes){
    const row=document.createElement("div");row.className="workflow-row";
    const title=document.createElement("strong");title.textContent=roleLabels[node.agent]||node.agent;
    const hint=document.createElement("small");hint.textContent=node.id+(node.id===draftGraph.entry?" · 入口":"");title.append(hint);row.append(title);
    for(const key of node.kind==="review"?["next","revise"]:["next"]){
      const label=document.createElement("label");label.textContent=key==="revise"?"未通过交给":"交付 / 通过交给";
      const select=document.createElement("select");select.setAttribute("aria-label",`${node.id} ${key}`);
      for(const id of [...draftGraph.nodes.map(n=>n.id),"done"]){const option=document.createElement("option");option.value=id;option.textContent=id==="done"?"完成交付":id;select.append(option);}
      select.value=node[key];select.addEventListener("change",()=>{node[key]=select.value;syncDraft();});label.append(select);row.append(label);
    }
    if(node.kind==="observer"){
      const remove=document.createElement("button");remove.type="button";remove.className="quiet";remove.textContent="移除";
      remove.addEventListener("click",()=>{for(const other of draftGraph.nodes)for(const key of ["next","revise"])if(other[key]===node.id)other[key]=node.next;draftGraph.nodes=draftGraph.nodes.filter(n=>n!==node);renderEditor();syncDraft();});row.append(remove);
    }
    el("workflow-nodes").append(row);
  }
}
async function saveGraph(save){
  for(const id of ["validate-workflow","save-workflow","close-workflow"])el(id).disabled=true;
  try{
    const candidate=JSON.parse(el("graph-json").value);
    const value=await post(save?"/api/graph":"/api/graph/validate",{graph:candidate});
    draftGraph=cloneGraph(value.graph);renderEditor();el("workflow-status").textContent=value.summary;el("workflow-status").classList.remove("error");
    if(save){workflowGraph=cloneGraph(value.graph);el("workflow-dialog").close();previewWorkflow();message("流程已在本机保存，下一次运行使用新编排；历史记录保留原流程。");}
  }catch(error){el("workflow-status").textContent=error instanceof SyntaxError?"JSON 格式错误，请检查括号和逗号。":error.message;el("workflow-status").classList.add("error");}
  finally{for(const id of ["validate-workflow","save-workflow","close-workflow"])el(id).disabled=false;}
}
function stopReplay(){if(replayTimer)clearInterval(replayTimer);replayTimer=null;el("replay").textContent="回放交接 ▶";}
function showRevision(){
  const revision=revisionEvidence?.revisions[Number(el("revision-select").value)];
  if(!revision){el("revision-summary").textContent="该历史记录没有逐轮源码快照，无法重建先前版本。";el("revision-integrity").textContent="";el("revision-source").textContent="";el("revision-diff").textContent="";return;}
  el("revision-summary").textContent=`版本 ${revision.index} · ${revision.node} · 消息 #${revision.message_seq}：${revision.summary}`;
  el("revision-integrity").textContent=`代码 ${revision.code_sha256.slice(0,16)}… · 冻结测试 ${revision.tests_sha256.slice(0,16)}… · 新增 ${revision.added} / 删除 ${revision.removed} 行`;
  el("revision-source").textContent=revision.source;el("revision-diff").replaceChildren();
  const patch=revision.index===0?"初始交付版本；后续版本在此显示相对它的修改。":revision.diff||"代码未发生变化。";
  for(const line of patch.split("\n")){const span=document.createElement("span");span.textContent=line+"\n";span.className=line.startsWith("+")?"diff-added":line.startsWith("-")?"diff-removed":"";el("revision-diff").append(span);}
}
function renderEvidence(value){
  stopReplay();revisionEvidence=value;
  el("acceptance").dataset.acceptance=value?.acceptance||"unfinished";
  el("acceptance").textContent=({verified:"实际验收通过 · 原始测试保持不变",unverified:"流程完成 · 尚待实际测试验证",unfinished:"尚未验收通过 · 查看审查记录或恢复检查点"})[value?.acceptance||"unfinished"];
  el("review-comparison").replaceChildren();
  for(const row of value?.review_history||[]){const item=document.createElement("div");item.className="review-round";item.dataset.status=row.tests;
    const title=document.createElement("strong"),detail=document.createElement("span");title.textContent=`第 ${row.cycle} 轮 · ${row.tests}`;detail.textContent=`${row.issues} 个问题 · ${row.tests_run??"未运行"} 个测试`;item.append(title,detail);el("review-comparison").append(item);}
  el("revision-select").replaceChildren();
  for(const revision of value?.revisions||[]){const option=document.createElement("option");option.value=revision.index;option.textContent=`版本 ${revision.index} · ${revision.node} · ${revision.reviews.at(-1)?.test_status||"等待复审"}`;el("revision-select").append(option);}
  if(value?.revisions.length)el("revision-select").value=value.revisions.at(-1).index;
  el("replay").disabled=!(value?.revisions.length>1);showRevision();
}
async function setupStudio(){
  const response=await fetch("/api/graph"),value=await response.json();if(!response.ok)throw new Error(value.error);
  workflowGraph=cloneGraph(value.graph);workflowPresets=value.presets;
  el("workflow-status").textContent=value.error||"选择模板、调整交接方向，然后校验并保存。";
  el("example").replaceChildren();for(const item of config.demo_cases){const option=document.createElement("option");option.value=item.id;option.textContent=item.title;el("example").append(option);}
  el("example").addEventListener("change",()=>{if(config.mode==="demo")el("task").value=config.demo_cases.find(c=>c.id===el("example").value).task;});
  el("edit-workflow").addEventListener("click",()=>{draftGraph=cloneGraph(workflowGraph);renderEditor();el("workflow-dialog").showModal();});
  el("close-workflow").addEventListener("click",()=>el("workflow-dialog").close());
  el("workflow-preset").addEventListener("change",()=>{draftGraph=cloneGraph(workflowPresets[el("workflow-preset").value]);renderEditor();syncDraft();});
  el("workflow-name").addEventListener("input",syncDraft);
  el("add-observer").addEventListener("click",()=>{if(draftGraph.nodes.length>=10){el("workflow-status").textContent="最多 10 个节点。";return;}
    const review=draftGraph.nodes.find(n=>n.kind==="review");if(!review){el("workflow-status").textContent="请先添加审查节点。";return;}
    let number=1;while(draftGraph.nodes.some(n=>n.id===`audit_${number}`))number++;
    const id=`audit_${number}`;draftGraph.nodes.push({id,agent:"audit",kind:"observer",next:review.next});review.next=id;renderEditor();syncDraft();});
  el("validate-workflow").addEventListener("click",()=>saveGraph(false));el("save-workflow").addEventListener("click",()=>saveGraph(true));
  el("revision-select").addEventListener("change",()=>{stopReplay();showRevision();});
  el("replay").addEventListener("click",()=>{if(replayTimer){stopReplay();return;}el("revision-select").value="0";showRevision();el("replay").textContent="停止回放 ■";
    replayTimer=setInterval(()=>{const next=Number(el("revision-select").value)+1;if(next>=revisionEvidence.revisions.length){stopReplay();return;}el("revision-select").value=String(next);showRevision();},1600);});
  el("download-bundle").addEventListener("click",async()=>{try{const response=await fetch(`/api/runs/${runId}/bundle`);if(!response.ok)throw new Error((await response.json()).error);
    const url=URL.createObjectURL(await response.blob()),anchor=document.createElement("a");anchor.href=url;anchor.download=`agentcoder-${runId.slice(0,8)}.zip`;anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }catch(error){message(error.message,true);}});
}
