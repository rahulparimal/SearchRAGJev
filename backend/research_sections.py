"""Deterministic sections of a Deep Research report. These are computed from the run itself (queries, sources, citation statistics,
verification result), never written by a model, so they cannot be invented: research brief, evidence assessment, confidence, research
method, source register and appendix. Pure functions, no I/O. Heuristics are labelled as heuristics in the output (domain-based
classification is not a reading of the source)."""
import re
from datetime import date
from urllib.parse import urlparse

AUTHORITATIVE={'Government / public body','Academic','Research / journal','Encyclopedia','Standards body'}
_RULES=[
 (r'(\.gov(\.[a-z]{2})?|\.mil|europa\.eu|\.int)$','Government / public body','Official public body; usually primary.'),
 (r'(\.edu(\.[a-z]{2})?|\.ac\.[a-z]{2})$','Academic','University or research institution.'),
 (r'(arxiv\.org|nature\.com|sciencedirect\.com|springer\.com|ieee\.org|acm\.org|pubmed|ncbi\.nlm\.nih\.gov|jstor\.org|plos\.org|wiley\.com|doi\.org)','Research / journal','Research publication; check peer-review status.'),
 (r'(wikipedia\.org|britannica\.com)','Encyclopedia','Secondary summary; good for orientation, not a primary source.'),
 (r'(ietf\.org|w3\.org|iso\.org|nist\.gov|ecma-international\.org)','Standards body','Standards or specification source.'),
 (r'(reuters\.com|apnews\.com|bbc\.|nytimes\.com|theguardian\.com|bloomberg\.com|ft\.com|wsj\.com|cnn\.com|economist\.com)','News media','Established news outlet; secondary reporting.'),
 (r'(github\.com|gitlab\.com)','Code repository','Primary for the code itself; READMEs can be outdated.'),
 (r'(reddit\.com|quora\.com|stackoverflow\.com|stackexchange\.com|news\.ycombinator\.com)','Community Q&A','User-generated; treat as anecdotal.'),
 (r'(medium\.com|substack\.com|blogspot\.|wordpress\.com|dev\.to|hashnode\.|linkedin\.com)','Blog / personal','Opinion or personal blog; verify against a primary source.'),
 (r'(ibm\.com|microsoft\.com|google\.com|cloud\.google|amazon\.com|aws\.amazon|oracle\.com|redhat\.com|salesforce\.com|databricks\.com|elastic\.co|mongodb\.com|snowflake\.com|nvidia\.com)','Vendor','Vendor content; may be promotional about its own product.'),
]
def classify_source(domain,url=''):
    """-> (type label, reliability note) from the domain only. A heuristic: it says what KIND of site this is, not whether the page is right."""
    h=re.sub(r'^www\.','',(domain or urlparse(url or '').hostname or '').lower()) if (domain or url) else ''
    for pat,label,note in _RULES:
        if re.search(pat,h):return label,note
    if h.startswith(('docs.','developer.','developers.')) or '/docs' in (url or ''):return 'Documentation','Documentation site; authoritative for its own product, check version.'
    return ('Web page (unclassified)','Unclassified domain; weigh against the other sources.') if h else ('Internal document','From your own knowledge base.')

def published_year(text):
    m=re.search(r'\b(19|20)\d{2}\b',text or '');return int(m.group(0)) if m else None

def _run(text):return {'text':str(text),'cites':[]}
def _p(label,text):return {'t':'p','label':label,'runs':[_run(text)]}
def _strength(ok_strong,ok_mod):return 'Strong' if ok_strong else ('Moderate' if ok_mod else 'Weak')

def _cited_runs(blocks):
    out=[]
    for b in blocks:
        if b['t']=='p':out.extend(b['runs'])
        elif b['t'] in ('ul','ol'):
            for it in b['items']:out.extend(it)
        elif b['t']=='table':
            for row in b['rows']:out.extend(c for c in row)
    return [r for r in out if r.get('cites')]

def build_sections(question,subqs,numbered,llm_blocks,verification,gaps,providers,mode,stats_in,today=None):
    """-> {'brief','assessment','method','register','appendix': [blocks], 'confidence': {'t':'confidence',...}}.
    stats_in: {'queries':[...],'pages_fetched':int,'dropped_uncited':int,'analysis_dropped':int,'models':{...},'failover':bool,'fallback_reason':str|None}"""
    today=today or date.today();now_y=today.year
    web=[s for s in numbered if s.get('source')=='web'];n=len(numbered)
    runs=_cited_runs(llm_blocks);n_cited=len(runs);multi=sum(1 for r in runs if len(set(r['cites']))>=2)
    cited_by={};[cited_by.__setitem__(c,cited_by.get(c,0)+1) for r in runs for c in set(r['cites'])]
    types=[classify_source(s.get('domain'),s.get('url'))[0] for s in numbered];auth=sum(1 for t in types if t in AUTHORITATIVE)
    domains={s.get('domain') for s in numbered if s.get('domain')}
    years=[y for y in (published_year(s.get('published')) for s in numbered) if y]
    ver=verification or {};checked=ver.get('checked',0);supported=ver.get('supported',0)
    # --- confidence (rule-based, shown with its reasons)
    reasons=[];level='Medium'
    agree=(multi/n_cited) if n_cited else 0.0;vrate=(supported/checked) if checked else None
    if mode=='extractive':level='Low';reasons.append('this is an evidence-only page, not a synthesised analysis')
    else:
        if n<3 or len(domains)<2:level='Low';reasons.append(f'only {n} source{"s" if n!=1 else ""} from {len(domains)} site{"s" if len(domains)!=1 else ""}')
        if vrate is not None and vrate<0.5:level='Low';reasons.append(f'only {supported} of {checked} checked statements were confirmed against their cited passage')
        elif level!='Low' and (vrate is not None and vrate>=0.8) and agree>=0.4 and len(domains)>=3:level='High'
        if level=='Medium':reasons.append('mixed signals: '+(f'{supported} of {checked} checked statements confirmed' if checked else 'citation checking was not available')+f'; {multi} of {n_cited} statements rest on 2+ sources')
        if level=='High':reasons.append(f'{supported} of {checked} checked statements confirmed; {multi} of {n_cited} statements rest on 2+ sources; {len(domains)} different sites')
    conf={'t':'confidence','level':level,'reason':'; '.join(reasons)+'. Computed from the run, not written by the model.'}
    # --- brief
    brief=[{'t':'h2','text':'Research brief'},_p('Research question',question),_p('Date',today.isoformat()),
           _p('Evidence base',f'{len(web)} web source{"s" if len(web)!=1 else ""}'+(f' and {n-len(web)} of your documents' if n>len(web) else '')+f' found by {len(stats_in.get("queries",[]))} searches across {len(subqs)} sub-question{"s" if len(subqs)!=1 else ""}.')]
    if years:brief.append(_p('Source dates',f'Publication years reported by the providers: {min(years)}-{max(years)} ({len(years)} of {n} sources had a date).'))
    # --- assessment
    pc=lambda x:f'{round(100*x)}%'
    rec=sum(1 for y in years if y>=now_y-2)/len(years) if years else None
    rows=[
     ('Agreement across sources',f'{multi} of {n_cited} cited statements are supported by two or more sources.' if n_cited else 'No cited statements to compare.',_strength(agree>=0.5,agree>=0.25) if n_cited else 'Not assessed','Several sources can repeat the same original; agreement is not proof of independence.'),
     ('Source quality',f'{auth} of {n} sources are authoritative types ({", ".join(sorted({t for t in types if t in AUTHORITATIVE})) or "none"}). Types seen: '+', '.join(f'{t} ({types.count(t)})' for t in dict.fromkeys(types))+'.',_strength(n and auth/n>=0.4,n and auth/n>=0.15),'Classified from the website address only (see the register); a good site can still host a weak page.'),
     ('Recency',(f'{len(years)} of {n} sources report a date; {pc(rec)} are from the last two years (newest {max(years)}, oldest {min(years)}).' if years else 'The search providers returned no publication dates.'),(_strength(rec>=0.5,rec>=0.25) if years else 'Not assessed'),'Dates come from the provider and can be missing or wrong; undated pages may be old.'),
     ('Data quality (claim checking)',(f'{supported} of {checked} checked statements were confirmed against the cited passage' + (f' ({ver.get("flagged",0)} flagged)' if ver.get('flagged') else '')+'.' if checked else 'Automated claim checking was not available for this run.'),(_strength(checked and supported/checked>=0.8,checked and supported/checked>=0.5) if checked else 'Not assessed'),'Checked by an automated model, not a person; short table cells are judged with their row context.'),
     ('Applicability','Fit to your specific situation cannot be assessed automatically.','Not assessed','Judge this yourself; see the Limitations below.'),
    ]
    assessment=[{'t':'h2','text':'Evidence assessment'},{'t':'table','head':['Evidence area','What was found','Strength','Caveats'],'rows':[[_run(c) for c in r] for r in rows]}]
    # --- method
    queries=list(dict.fromkeys(stats_in.get('queries',[])))
    lims=[]
    if not stats_in.get('pages_fetched'):lims.append('Evidence is mostly search-result snippets, not whole pages, so detail is limited.')
    elif stats_in['pages_fetched']<len(web):lims.append(f'Whole pages were read for {stats_in["pages_fetched"]} of {len(web)} web sources; the rest are snippets.')
    if gaps:lims.append(f'No usable evidence was found for {len(gaps)} sub-question{"s" if len(gaps)!=1 else ""} (see the appendix).')
    if n<5:lims.append('Few sources were found; conclusions are tentative.')
    if not checked:lims.append('Statements were not independently checked against their sources.')
    if stats_in.get('failover'):lims.append('The primary writing model failed, so a backup model wrote this report.')
    lims.append('Source classification and the confidence rating are rule-based heuristics.')
    method=[{'t':'h2','text':'Research method'},_p('Sources searched',', '.join(providers) if providers else 'Web search providers'),
            _p('Search terms','; '.join(queries[:20])+(f' (+{len(queries)-20} more)' if len(queries)>20 else '')),
            _p('Inclusion criteria','Top-ranked results per query (keyword relevance plus a source-quality prior), de-duplicated by address, at most a few per website.'),
            _p('Exclusion criteria','Duplicate addresses, results beyond the per-question limit, and pages that could not be fetched or that returned non-text content.'),
            _p('Analysis method',f'A model wrote the report from the numbered sources only. Every factual statement must cite a source; {stats_in.get("dropped_uncited",0)} statement(s) without a valid citation were removed'+(f' and {stats_in["analysis_dropped"]} analysis sentence(s) were removed for adding facts not found in the sources' if stats_in.get('analysis_dropped') else '')+'. Remaining statements were checked against the cited passage by an automated verifier.'),
            {'t':'label','text':'Limitations'},{'t':'ul','items':[[_run(x)] for x in lims]}]
    # --- register
    reg_rows=[]
    for s in numbered:
        t,note=classify_source(s.get('domain'),s.get('url'));k=cited_by.get(s['n'],0)
        reg_rows.append([_run(f"S{s['n']}"),_run((s.get('title') or '')+(f" - {s['url']}" if s.get('url') else '')),_run(s.get('domain') or ('Your documents' if s.get('source')=='local' else '')),_run(str(published_year(s.get('published')) or '-')),_run(t),_run(f'Cited in {k} statement{"s" if k!=1 else ""}' if k else 'Not cited'),_run(note)])
    register=[{'t':'h2','text':'Source register'},{'t':'table','head':['ID','Source','Organisation','Year','Type','How it supports the research','Reliability notes'],'rows':reg_rows}] if reg_rows else []
    # --- appendix
    app=[{'t':'h2','text':'Appendix'}]
    if gaps:app+=[{'t':'h3','text':'Coverage gaps'},{'t':'ul','items':[[_run(f"{g['question']} ({g['reason'].replace('_',' ')})")] for g in gaps]}]
    m=stats_in.get('models') or {}
    details=[f'Planning model: {m.get("plan") or "-"}; writing model: {m.get("report") or "none (evidence-only report)"}.',f'{n} sources, {len(queries)} searches, {stats_in.get("pages_fetched",0)} whole pages read.']
    if stats_in.get('fallback_reason'):details.append('Evidence-only fallback reason: '+str(stats_in['fallback_reason']).replace('_',' ')+'.')
    app+=[{'t':'h3','text':'Run details'},{'t':'ul','items':[[_run(x)] for x in details]}]
    return {'brief':brief,'assessment':assessment,'method':method,'register':register,'appendix':app,'confidence':conf}

def assemble(llm_blocks,sec):
    """brief + model-written blocks (with the computed confidence placed right after the executive summary) + assessment, method, register, appendix."""
    out=list(sec['brief']);placed=False
    for b in llm_blocks:
        if not placed and b['t']=='h2' and re.match(r'(key )?findings',b['text'],re.I):out.append(sec['confidence']);placed=True
        out.append(b)
    if not placed:out.insert(len(sec['brief']),sec['confidence'])
    return out+sec['assessment']+sec['method']+sec['register']+sec['appendix']
