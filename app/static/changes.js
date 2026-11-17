(function(){
const editor=document.querySelector('#change-json');
const message=document.querySelector('#change-msg');
if(!editor)return;
const sample=name=>{const node=document.querySelector('#sample-'+name);if(node)editor.value=node.textContent.trim();};
document.querySelector('#load-fix').addEventListener('click',()=>sample('fix'));
document.querySelector('#load-break').addEventListener('click',()=>sample('break'));
document.querySelector('#submit-change').addEventListener('click',async event=>{
  event.target.disabled=true;message.textContent='改单核验中…';
  try{
    let payload;
    try{payload=JSON.parse(editor.value);}catch(e){throw Error('变更单不是有效JSON：'+e.message);}
    const response=await fetch('/api/changes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const result=await response.json();
    if(!response.ok)throw Error(result.detail||response.status);
    const note=result.reused?'同一变更已存在，未新增第二套当前结果；':'';
    message.textContent=note+'变更'+result.status+'，整批裁决：'+result.decision.batch;
    if(!result.reused)setTimeout(()=>{location.href='/changes?change_id='+result.change_id;},600);
  }catch(e){message.textContent=e.message;}
  finally{event.target.disabled=false;}
});
})();
