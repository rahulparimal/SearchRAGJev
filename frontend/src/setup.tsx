import{useState}from'react';
import{CheckCircle2,XCircle,LoaderCircle,Server}from'lucide-react';

export const PRESETS=[
 {id:'openai',label:'OpenAI',base:'https://api.openai.com/v1',models:['gpt-4o-mini','gpt-4.1-mini'],key:true,hint:'Paste an OpenAI API key.'},
 {id:'openrouter',label:'OpenRouter',base:'https://openrouter.ai/api/v1',models:['meta-llama/llama-3.3-70b-instruct','openai/gpt-4o-mini'],key:true,hint:'Paste an OpenRouter API key; the model id includes the vendor, e.g. openai/gpt-4o-mini.'},
 {id:'ollama',label:'Ollama (local model)',base:'http://localhost:11434/v1',models:['llama3.2','qwen2.5'],key:false,hint:'Runs on your machine, no key and nothing leaves it. Install from ollama.com, then run: ollama pull llama3.2. If this app runs in Docker, use http://host.docker.internal:11434/v1 as the URL.'},
 {id:'custom',label:'Other (OpenAI-compatible)',base:'',models:[],key:true,hint:'Any server that supports /chat/completions (vLLM, LM Studio, Azure OpenAI, ...).'},
];
const presetOf=(base:string)=>PRESETS.find(p=>p.base&&base.replace(/\/$/,'')===p.base)||(base?PRESETS[3]:null);

type Props={api:string;headers:()=>{[k:string]:string};secrets:any;setSecrets:(f:any)=>void;config:any;onSave:()=>Promise<boolean>;onSkip?:()=>void;compact?:boolean};
export function QuickSetup({api,headers,secrets,setSecrets,config,onSave,onSkip,compact}:Props){
 const[detected,setDetected]=useState<string[]>([]);const[test,setTest]=useState<{state:'idle'|'busy'|'ok'|'fail';msg?:string}>({state:'idle'});
 const[busy,setBusy]=useState(false);const[note,setNote]=useState('');
 const preset=presetOf(secrets.llm_base_url||'');const needsKey=preset?preset.key:true;
 const probe=async(path:string)=>{
  const r=await fetch(api+path,{method:'POST',headers:{...headers(),'Content-Type':'application/json'},body:JSON.stringify({base_url:secrets.llm_base_url,model:secrets.llm_model,api_key:secrets.llm_api_key||''})});
  const j=await r.json().catch(()=>({}));if(!r.ok)return{ok:false,error:typeof j.detail==='string'?j.detail:`http_${r.status}`,models:[] as string[]};return j;
 };
 async function detect(){setNote('');const j=await probe('/admin/llm/models');if(j.ok){setDetected(j.models);setNote(j.models.length?`${j.models.length} model${j.models.length===1?'':'s'} found`:'Connected, but no models are installed yet');const chat=j.models.filter((m:string)=>!/embed/i.test(m));if(!secrets.llm_model&&chat.length)setSecrets({...secrets,llm_model:chat[0]})}else setNote(`Could not list models (${j.error}). Is the server running and the URL right?`)}
 function choose(p:typeof PRESETS[number]){setSecrets({...secrets,llm_base_url:p.base,llm_model:'',llm_api_key:p.key?secrets.llm_api_key:''});setDetected([]);setTest({state:'idle'});setNote('')}
 async function runTest(){setTest({state:'busy'});const j=await probe('/admin/llm/test');setTest(j.ok?{state:'ok',msg:`Working (${j.latency_ms} ms)`}:{state:'fail',msg:`Failed: ${j.error}${j.error==='timeout'&&preset?.id==='ollama'?' - the first request loads the model; try again':''}`})}
 async function save(){setBusy(true);const ok=await onSave();setBusy(false);setNote(ok?'Saved. Quick answer, AI Overview and Deep Research now use this model.':'')}
 const suggestions=Array.from(new Set([...detected,...(preset?.models||[])]));
 return <div className={`quick-setup${compact?' compact':''}`}>
  {!compact&&<><div className="qs-head"><Server size={16}/><b>Connect your AI</b></div>
  <p className="section-intro">Pick where your language model runs. This one model powers Quick answer, AI Overview and Deep Research (each can still be overridden in the tabs below).</p></>}
  <div className="qs-presets" role="radiogroup" aria-label="LLM provider">{PRESETS.map(p=><button type="button" key={p.id} role="radio" aria-checked={preset?.id===p.id} className={preset?.id===p.id?'selected':''} onClick={()=>choose(p)}>{p.label}</button>)}</div>
  {preset&&<p className="qs-hint">{preset.hint}</p>}
  <label>API base URL<input value={secrets.llm_base_url||''} onChange={e=>{setSecrets({...secrets,llm_base_url:e.target.value});setTest({state:'idle'})}} placeholder="https://.../v1"/></label>
  <label>Model <span className="field-tag">{preset?.id==='ollama'?'Installed on this machine':'Exact model id'}</span><input list="qs-models" value={secrets.llm_model||''} onChange={e=>{setSecrets({...secrets,llm_model:e.target.value});setTest({state:'idle'})}} placeholder={preset?.models[0]||'model id'}/><datalist id="qs-models">{suggestions.map(m=><option key={m} value={m}/>)}</datalist></label>
  <div className="qs-row"><button type="button" className="refresh-btn" disabled={!secrets.llm_base_url} onClick={detect}>{preset?.id==='ollama'?'Detect installed models':'List available models'}</button>{note&&<span className="qs-note">{note}</span>}</div>
  {needsKey&&<label>API key <span className="field-tag">{preset?.id==='custom'?'Optional for local servers':'Required'}</span><input type="password" value={secrets.llm_api_key||''} onChange={e=>setSecrets({...secrets,llm_api_key:e.target.value})} placeholder={config.llm_key_configured?'Configured - enter to replace':'Paste key'} autoComplete="new-password"/></label>}
  <label>TypeSafe Jev API key <span className="field-tag">Optional - search works without it</span><input type="password" value={secrets.typesafe_api_key||''} onChange={e=>setSecrets({...secrets,typesafe_api_key:e.target.value})} placeholder={config.typesafe_configured?'Configured - enter to replace':'Adds smarter routing, reranking and citation checks'} autoComplete="new-password"/></label>
  <div className="qs-row">
   <button type="button" className="refresh-btn" disabled={!secrets.llm_base_url||!secrets.llm_model||test.state==='busy'} onClick={runTest}>{test.state==='busy'?<><LoaderCircle size={13} className="spin"/> Testing...</>:'Test connection'}</button>
   {test.state==='ok'&&<span className="qs-ok"><CheckCircle2 size={14}/> {test.msg}</span>}{test.state==='fail'&&<span className="qs-fail"><XCircle size={14}/> {test.msg}</span>}
  </div>
  <div className="qs-actions"><button type="button" className="primary" disabled={busy||(!secrets.llm_base_url&&!secrets.typesafe_api_key)} onClick={save}><CheckCircle2 size={15}/> Save and apply</button>{onSkip&&<button type="button" className="refresh-btn" onClick={onSkip}>Skip for now</button>}</div>
 </div>;
}

export function ProviderSelect({base,onPick,label}:{base:string;onPick:(p:typeof PRESETS[number])=>void;label?:string}){
 const cur=presetOf(base||'');
 return <label>{label||'Service provider'} <span className="field-tag">Fills the URL; edit it below for anything else</span>
  <select value={cur?.id||''} onChange={e=>{const p=PRESETS.find(x=>x.id===e.target.value);if(p)onPick(p)}}>
   <option value="" disabled>Choose a provider...</option>{PRESETS.map(p=><option key={p.id} value={p.id}>{p.label}</option>)}
  </select></label>;
}

export function EndpointTest({api,headers,base,model,apiKey}:{api:string;headers:()=>{[k:string]:string};base:string;model:string;apiKey:string}){
 const[t,setT]=useState<{state:'idle'|'busy'|'ok'|'fail';msg?:string}>({state:'idle'});
 async function run(){setT({state:'busy'});const r=await fetch(api+'/admin/llm/test',{method:'POST',headers:{...headers(),'Content-Type':'application/json'},body:JSON.stringify({base_url:base,model,api_key:apiKey})});const j=await r.json().catch(()=>({}));
  setT(r.ok&&j.ok?{state:'ok',msg:`Working (${j.latency_ms} ms)`}:{state:'fail',msg:`Failed: ${r.ok?j.error:(typeof j.detail==='string'?j.detail:`http_${r.status}`)}`})}
 return <div className="qs-row"><button type="button" className="refresh-btn" disabled={!base||!model||t.state==='busy'} onClick={run}>{t.state==='busy'?'Testing...':'Test connection'}</button>
  {t.state==='ok'&&<span className="qs-ok"><CheckCircle2 size={14}/> {t.msg}</span>}{t.state==='fail'&&<span className="qs-fail"><XCircle size={14}/> {t.msg}</span>}</div>;
}
