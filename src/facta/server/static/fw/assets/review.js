import{d as r,u as e,R as b}from"./hooks.module.js";async function g(t){if(t.ok)return t.json();let n=`HTTP ${t.status}`;try{n=(await t.json()).detail??n}catch{}const s=new Error(n);throw s.status=t.status,s}async function T({text:t,repoRoot:n,timeout:s=900}){const l=s?`?timeout=${s}`:"";return g(await fetch(`/api/runs/sync${l}`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text:t,mode:"review",repo_root:n||null})}))}const y=`【任务目标】


【验收要求】


【待评审方案】


【相关历史决定（可选）】
`;function x(){const[t,n]=r(""),[s,l]=r(""),[a,d]=r(!1),[u,p]=r(null),[c,h]=r(null),[m,f]=r(null),[v,w]=r(!1);return e("div",{children:[e("p",{class:"muted",style:"font-size:12px;",children:"按分节填【任务目标】【验收要求】【待评审方案】【相关历史决定】。评审只读 ——不会改代码、执行命令或固化记忆；需实跑才能判断的会标「待验证」。"}),e("p",{children:e("input",{value:s,onInput:o=>l(o.target.value),placeholder:"被评审仓库根路径（留空 = 评审 Facta 本仓）",style:"width:100%;"})}),e("p",{children:e("textarea",{value:t,onInput:o=>n(o.target.value),placeholder:y,rows:14,style:"width:100%;font-family:inherit;"})}),e("div",{class:"memory-actions visible",children:[e("button",{onClick:async()=>{if(!t.trim())return;d(!0),p(null),h(null),w(!1);const o=Date.now();try{const i=await T({text:t,repoRoot:s.trim()});h(i.text??""),w(!!i.timed_out),f(((Date.now()-o)/1e3).toFixed(1))}catch(i){p(i)}finally{d(!1)}},disabled:a||!t.trim(),children:a?"评审中…":"开始评审"}),e("button",{onClick:()=>n(y),disabled:a,children:"填模板"})]}),u&&e("div",{class:"notes-banner notes-banner-err",children:e("span",{children:u.message})}),v&&e("div",{class:"notes-banner",children:e("span",{children:"等待预算用尽，评审仍在后台运行；稍后可在会话清单里查看结果。"})}),c!==null&&e("div",{style:"margin-top:16px;",children:[e("hr",{}),m&&e("p",{class:"muted",style:"font-size:12px;",children:["评审用时 ",m,"s"]}),window.marked?e("div",{dangerouslySetInnerHTML:{__html:window.marked.parse(c)}}):e("pre",{style:"white-space:pre-wrap;",children:c})]})]})}b(e(x,{}),document.getElementById("review-root"));
