"use strict";
let iterationBase=null;

function updateRunSummary(){
  const source=el("source").value,execute=el("allow-exec").checked;
  const basis=iterationBase?`继承运行 ${iterationBase.id.slice(0,8)} 的最新源码及 ${(iterationBase.inherited_tests?.length||0)+1} 组历史测试`:
    source.trim()?`使用已载入的 Python 代码或测试（${source.length} 字符）`:"新建实现，尚未选择历史版本";
  el("run-summary").textContent=`代码来源：${basis}。测试：${execute?"执行 unittest":"仅静态审查，不会产生实测通过结论"}。最多修复 ${el("max-cycles").value} 轮。`;
  el("run-summary").dataset.execution=execute?"unittest":"static";
}
function clearIteration(clearSource=false){
  iterationBase=null;el("iteration-context").hidden=true;el("iteration-requirement").hidden=true;
  el("iteration-request").required=false;el("iteration-request").value="";el("iteration-source").textContent="";
  el("task").readOnly=busy||config?.mode==="demo";el("run-title").textContent="开始一次协作";
  el("source-section").hidden=config?.mode==="demo";
  if(clearSource){el("source").value="";el("source-path").value="";}
  if(clearSource&&config?.mode==="demo")el("task").value=config.demo_cases?.find(item=>item.id===el("example").value)?.task||config.demo_task;
  updateRunSummary();
}
async function prepareIteration(id){
  if(busy)return;
  setBusy(true);el("cancel").hidden=true;message("正在载入所选源码版本与历史测试…");
  try{
    const response=await fetch("/api/runs/"+id),loaded=await response.json();if(!response.ok)throw new Error(loaded.error);
    const parent=loaded.state;
    if(loaded.active||!["completed","exhausted"].includes(parent.status)||!parent.artifacts["solution.py"])throw new Error("请选择已经结束并交付源码的运行；中断记录请先恢复。");
    resetNewRun();iterationBase=parent;
    el("task").value=parent.task;el("task").readOnly=true;el("source").value=parent.artifacts["solution.py"];
    el("iteration-context").hidden=false;el("iteration-requirement").hidden=false;el("iteration-request").required=true;
    el("iteration-request").maxLength=Math.max(1,4000-parent.task.length-12);
    const version=parent.revisions?.at(-1)?.index;
    el("iteration-origin").textContent=`基于 ${parent.id.slice(0,8)} · ${version===undefined?"当前版本":"版本 "+version} 继续迭代`;
    el("iteration-source").textContent=parent.artifacts["solution.py"];el("source-section").hidden=true;el("demo-options").hidden=true;
    el("run-title").textContent="继续迭代已有版本";el("max-cycles").value=String(parent.max_cycles);
    workflowGraph=cloneGraph(parent.graph);previewWorkflow();
    if(parent.workdir&&currentWorkspace?.path!==parent.workdir)await chooseWorkspace(false,parent.workdir);
    updateRunSummary();message("已继承上一轮代码与测试，请填写本轮新增要求；父运行不会改写。");
    el("run-form").scrollIntoView({behavior:"smooth",block:"start"});el("iteration-request").focus({preventScroll:true});
  }catch(error){message(error.message,true);}finally{setBusy(false);}
}
function renderHistory(rows){
  el("history").replaceChildren();
  if(!rows.length){el("history").textContent="还没有运行记录。";return;}
  for(const row of rows){
    const card=document.createElement("article");card.className="history-card";
    const view=document.createElement("button"),title=document.createElement("strong"),detail=document.createElement("span");
    view.type="button";view.className="history-view";title.textContent=row.task.slice(0,80);
    detail.textContent=`${row.status} · ${row.id.slice(0,8)} · 修复 ${row.cycle} 轮`+(row.parent_run_id?` · 继承 ${row.parent_run_id.slice(0,8)}`:"");
    view.append(title,detail);view.dataset.locked=String(Boolean(row.active));view.disabled=busy||row.active;
    view.addEventListener("click",async()=>{try{const response=await fetch("/api/runs/"+row.id),loaded=await response.json();if(!response.ok)throw new Error(loaded.error);
      showState(loaded.state,loaded.report,loaded.evidence);message("已打开交付记录；可基于此版本继续迭代。");el("results").scrollIntoView({behavior:"smooth"});}catch(error){message(error.message,true);}});
    const actions=document.createElement("div");actions.className="history-actions";
    const next=document.createElement("button");next.type="button";next.className="secondary";next.textContent="继续迭代";
    next.dataset.locked=String(Boolean(row.active)||!row.has_source||!["completed","exhausted"].includes(row.status));next.disabled=busy||next.dataset.locked==="true";
    next.addEventListener("click",()=>prepareIteration(row.id));
    const download=document.createElement("a");download.href="/api/runs/"+row.id+"/bundle";download.textContent="下载交付包";download.className="quiet";
    download.dataset.locked=String(Boolean(row.active));download.setAttribute("aria-disabled",String(busy||Boolean(row.active)));
    download.addEventListener("click",event=>{if(busy||row.active)event.preventDefault();});
    actions.append(next,download);card.append(view,actions);el("history").append(card);
  }
}
