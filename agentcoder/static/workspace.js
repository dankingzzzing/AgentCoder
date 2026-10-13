"use strict";
let currentWorkspace=null;
function applyWorkspace(value){
  currentWorkspace=value;el("workspace-path").value=value.path;el("known-workspaces").replaceChildren();
  for(const row of value.known){const option=document.createElement("option");option.value=row.path;option.textContent=row.path;el("known-workspaces").append(option);}
  el("known-workspaces").value=value.path;
  el("workspace-status").textContent=value.error||`协作输出保存到 ${value.output_hint}，已有源码不会自动覆盖。`;
  el("workspace-files").replaceChildren();
  for(const file of value.files){const button=document.createElement("button");button.type="button";button.className="secondary";button.textContent=file.name;button.disabled=busy;
    button.addEventListener("click",async()=>{try{const result=await post("/api/source",{path:file.path});el("source-path").value=result.path;el("source").value=result.content;el("source-section").open=true;message(result.kind==="tests"?`识别到回归测试，将为 ${result.module} 生成实现，原测试冻结不修改。`:"已载入本地模块，原文件保持只读。");}catch(error){message(error.message,true);}});el("workspace-files").append(button);}
}
async function loadWorkspace(){const response=await fetch("/api/workspaces"),value=await response.json();if(!response.ok)throw new Error(value.error);applyWorkspace(value);}
async function chooseWorkspace(native,path=null){
  for(const id of ["pick-workspace","use-workspace"])el(id).disabled=true;
  try{const value=await post(native?"/api/workspaces/pick":"/api/workspaces/select",native?{}:{path:path||el("workspace-path").value});if(!value.cancelled){applyWorkspace(value);await history();message("已选择本地工作目录，下一次协作输出将保存在该目录。");}}
  catch(error){message(error.message,true);}finally{for(const id of ["pick-workspace","use-workspace"])el(id).disabled=busy;}
}
function showDrafts(value){
  if(!value)el("model-progress").textContent="";
  if(value?.progress){const progress=value.progress;el("model-progress").textContent=`请求 ${progress.attempt||1} · ${progress.transport==="buffered"?"普通响应":"流式响应"} · ${progress.signals||0} 次响应信号 · ${progress.public_chars||0} 个公开交付字符`;
    if(value.agent&&busy&&progress.stage!=="connecting")el("stage").textContent=`模型正在处理请求；已收到 ${progress.signals||0} 次响应信号、${progress.public_chars||0} 个公开交付字符。当前输出尚未提交，可随时停止。`;
  }
  el("draft-code").textContent=value?.drafts?.["solution.py"]||"还未接收到模块草稿。";
  el("draft-tests").textContent=value?.drafts?.["test_solution.py"]||"还未接收到测试草稿。";
  el("draft-summary").textContent=value?.summary||"这些是公开交付中的内容，尚未校验；完成后再查看正式报告与代码。";
  if(value?.drafts&&Object.keys(value.drafts).length)el("live-drafts").open=true;
}
async function setupWorkspace(){
  await loadWorkspace();
  el("use-workspace").addEventListener("click",()=>chooseWorkspace(false));el("pick-workspace").addEventListener("click",()=>chooseWorkspace(true));
  el("known-workspaces").addEventListener("change",()=>chooseWorkspace(false,el("known-workspaces").value));
  el("refresh-workspace").addEventListener("click",()=>loadWorkspace().catch(error=>message(error.message,true)));
  el("create-file").addEventListener("click",async()=>{el("create-file").disabled=true;
    try{const value=await post("/api/workspaces/create-file",{workspace_id:currentWorkspace.id,name:el("new-filename").value,content:el("new-file-content").value});applyWorkspace(value.workspace);
      el("source-path").value=value.path;el("source").value=value.content;el("source-section").open=true;message("本地代码文件已创建并载入。填写需求后可以开始协作。");
    }catch(error){message(error.message,true);}finally{el("create-file").disabled=busy;}});
  showDrafts(null);
}
