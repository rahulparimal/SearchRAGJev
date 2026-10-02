import{Fragment} from 'react';
import{Globe,LoaderCircle,CheckCircle2,XCircle,Copy} from 'lucide-react';

export type Run={text:string;cites:number[];flag?:string|null};
export type Block={t:'h2'|'h3';text:string}|{t:'p';runs:Run[]}|{t:'ul';items:Run[][]}|{t:'table';head:string[];rows:Run[][]};
export type Citation={n:number;title:string;url:string|null;domain:string|null;source:string};
export type Gap={id:string;question:string;reason:string};
export type Verification={status:'verified'|'partial'|'unavailable'|'skipped';checked:number;supported:number;flagged:number;unchecked:number};
export type Report={cached?:boolean;title:string|null;blocks:Block[];citations:Citation[];gaps:Gap[];verification:Verification;markdown:string;model:string;elapsed_ms:number;stats:any};
export type SubqStatus={id:string;question:string;state:'pending'|'searching'|'done'|'failed';sources:{title:string;domain:string;url:string}[];reason?:string};
export type PartialSource={n:number;title:string;url:string|null;domain:string|null;source:string};
export type ResearchState={
 status:'idle'|'planning'|'searching'|'reading'|'writing'|'verifying'|'done'|'error';
 title?:string|null;subqs:SubqStatus[];report?:Report;error?:string;elapsedMs?:number;
 partialSources?:PartialSource[];partialGaps?:Gap[];  // set on error when SOME sources were gathered before the failure; never fed to ReportView (that expects a full validated report)
};
export function PartialSources({sources,gaps}:{sources:PartialSource[];gaps:Gap[]}){
 return <div className="research-partial"><p className="muted">No report was written, but {sources.length} source{sources.length===1?'':'s'} were found before the error:</p>
  <ul>{sources.map(s=><li key={s.n}>{s.url?<a href={s.url} target="_blank" rel="noreferrer noopener">{s.title}</a>:<span>{s.title}</span>}{s.domain?<span className="cite-domain"> · {s.domain}</span>:''}</li>)}</ul>
  {gaps.length>0&&<p className="muted">No evidence was found for: {gaps.map(g=>g.question).join('; ')}</p>}
 </div>;
}

const ERR_COPY:{[k:string]:string}={
 not_configured:'Deep Research needs an LLM configured in Admin › Providers.',
 busy:'Deep Research is busy right now (too many runs in progress); try again in a moment.',
 rate_limited:'You have hit the hourly limit for Deep Research runs.',
 no_sources:'No usable web sources were found for this question.',
 provider_unavailable:'The research model did not respond. Try again.',
 unsupported_claim:'The draft report did not cite its sources well enough to show; try again or rephrase the question.',
 timeout:'Deep Research ran out of time before finishing.',
};
export function researchErrorText(reason?:string){return (reason&&ERR_COPY[reason])||'Deep Research could not complete.'}

export async function streamResearch(apiBase:string,token:string,query:string,includeKb:boolean,onEvent:(name:string,data:any)=>void,signal:AbortSignal,refresh=false){
 let r=await fetch(apiBase+'/research',{method:'POST',headers:{Authorization:`Bearer ${token}`,'Content-Type':'application/json'},body:JSON.stringify({query,web_consent:true,include_kb:includeKb,refresh}),signal});
 if(!r.ok||!r.body){let j:any={};try{j=await r.json()}catch{}  ;onEvent('error',{reason:j.detail||`http_${r.status}`});return}
 let rd=r.body.getReader(),dec=new TextDecoder(),buf='';
 for(;;){
  let{done,value}=await rd.read();if(done)break;
  buf+=dec.decode(value,{stream:true});let parts=buf.split('\n\n');buf=parts.pop()||'';
  for(let p of parts){
   if(!p.trim()||p.trim().startsWith(':'))continue;  // SSE heartbeat comments
   let ev=/^event: (.+)$/m.exec(p)?.[1],dt=/^data: (.+)$/m.exec(p)?.[1];
   if(!ev||!dt)continue;
   try{onEvent(ev,JSON.parse(dt))}catch{}
  }
 }
}

function Cites({cites}:{cites:number[]}){
 if(!cites.length)return null;
 return <>{cites.map(n=><sup key={n}><a href={`#rs-S${n}`} className="cite-link">[{n}]</a></sup>)}</>;
}
function RunText({runs}:{runs:Run[]}){
 return <>{runs.map((r,i)=><Fragment key={i}>{i>0?' ':''}<span className={r.flag?'claim-flagged':''} title={r.flag?'Not independently verified against its cited source':undefined}>{r.text}</span><Cites cites={r.cites}/></Fragment>)}</>;
}
export function ReportView({report,onCopy,onRefresh}:{report:Report;onCopy:()=>void;onRefresh?:()=>void}){
 if(!report||!report.blocks)return null;
 const webCount=report.citations.filter(c=>c.source!=='local').length,pagesRead=report.stats?.pages_fetched||0;  // defensive: only a genuine 'report' SSE event should ever reach this component
 return <div className="research-report">
  {report.title&&<h2 className="research-title">{report.title}</h2>}
  {report.blocks.map((b,i)=>{
   if(b.t==='h2')return <h3 key={i}>{b.text}</h3>;
   if(b.t==='h3')return <h4 key={i}>{b.text}</h4>;
   if(b.t==='p')return <p key={i}><RunText runs={b.runs}/></p>;
   if(b.t==='ul')return <ul key={i}>{b.items.map((item,j)=><li key={j}><RunText runs={item}/></li>)}</ul>;
   if(b.t==='table')return <div className="research-table-wrap" key={i}><table className="research-table"><thead><tr>{b.head.map((h,j)=><th key={j}>{h}</th>)}</tr></thead><tbody>{b.rows.map((row,j)=><tr key={j}>{row.map((c,k)=><td key={k}><RunText runs={[c]}/></td>)}</tr>)}</tbody></table></div>;
   return null;
  })}
  {report.gaps.length>0&&<div className="research-gaps"><b>Coverage gaps</b><ul>{report.gaps.map(g=><li key={g.id}>{g.question}</li>)}</ul></div>}
  <div className="research-sources"><b>Sources</b><ol>{report.citations.map(c=><li id={`rs-S${c.n}`} key={c.n}>{c.url?<a href={c.url} target="_blank" rel="noreferrer noopener">{c.title}</a>:<span>{c.title}{c.source==='local'?' (your documents)':''}</span>}{c.domain?<span className="cite-domain"> · {c.domain}</span>:''}</li>)}</ol></div>
  <div className="research-footer">
   Built from {webCount} web source{webCount===1?'':'s'} ({pagesRead>0?`${pagesRead} read as full pages, the rest as snippets`:'snippets, not full pages'})
   {report.verification.status==='verified'&&` · ${report.verification.supported} of ${report.verification.checked} statements verified against the cited source text`}
   {report.verification.status==='partial'&&` · ${report.verification.supported} of ${report.verification.checked} checked statements verified (some claims were not checked)`}
   {report.verification.status==='unavailable'&&' · citation verification was unavailable for this run'}
   {' · '}{(report.elapsed_ms/1000).toFixed(0)}s
   {report.cached&&' · served from cache'}
   {report.cached&&onRefresh&&<button type="button" className="research-copy" title="Runs the research again (counts toward your hourly limit)" onClick={onRefresh}>Refresh</button>}
   <button type="button" className="research-copy" onClick={()=>window.print()}>Print / save as PDF</button>
   <button type="button" className="research-copy" onClick={onCopy}><Copy size={12}/> Copy markdown</button>
   <button type="button" className="research-copy" onClick={()=>{const u=URL.createObjectURL(new Blob([report.markdown],{type:'text/markdown'}));const a=document.createElement('a');a.href=u;a.download=`${(report.title||'research-report').replace(/[^\w-]+/g,'-').slice(0,60)}.md`;a.click();URL.revokeObjectURL(u)}}>Download .md</button>
  </div>
 </div>;
}
export function ResearchProgress({state,onStop}:{state:ResearchState;onStop:()=>void}){
 const label:{[k:string]:string}={planning:'Planning research…',searching:'Searching the web…',reading:'Reading your documents…',writing:'Writing the report…',verifying:'Verifying citations…'};
 return <div className="research-progress">
  <div className="research-progress-head"><LoaderCircle className="spin" size={15}/> <span>{label[state.status]||'Working…'}</span>{state.elapsedMs!=null&&<span className="research-elapsed">{Math.round(state.elapsedMs/1000)}s</span>}<button type="button" className="research-stop" onClick={onStop}>Stop</button></div>
  {state.subqs.length>0&&<ul className="research-subqs">{state.subqs.map(sq=><li key={sq.id}>
   {sq.state==='done'?<CheckCircle2 size={13} className="subq-ok"/>:sq.state==='failed'?<XCircle size={13} className="subq-fail"/>:<LoaderCircle className="spin" size={13}/>}
   <span>{sq.question}</span>{sq.sources.length>0&&<span className="subq-count">{sq.sources.length} source{sq.sources.length===1?'':'s'}</span>}
  </li>)}</ul>}
 </div>;
}
export function ResearchDisclosure({providers,model}:{providers:string;model?:string|null}){
 return <div className="privacy-pill research-pill"><Globe size={12}/> Deep Research uses public web sources: your question and generated sub-queries go to {providers||'the configured search providers'}; gathered sources go to {model||'the configured model'}. Takes about 1-3 minutes.</div>;
}
