const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const dashboard=fs.readFileSync('dashboard.js','utf8');
const research=fs.readFileSync('dashboard-research.js','utf8');
const visuals=fs.readFileSync('dashboard-visuals.js','utf8');
const slice=(text,from,to)=>text.slice(text.indexOf('function '+from+'('),text.indexOf('function '+to+'('));
const analysis={target_date:'2026-09-30',decision:{},ai_review:{business_status:'FAILED',
    evidence_status:'FACT_MISMATCH',fact_validation:{status:'FAILED',reason:'医药均线相反'},
    generated_at:'2026-10-06T21:57:32Z',interpretation:{sectors:[{code:'801150',judgment:'错误历史原文'}]}}};
const helpers={analysis,URL,esc:String,num:String,pct:String,money:String,timeText:String,sourceLinks:(ids,historical)=>historical?'历史证据':'当前证据'};
const api=vm.runInNewContext(research+visuals+';({activeAI,aiDetail,currentResearchAI,groupEventSamples,eventSourceLabel,latestAiAttempt,entrySummary})',helpers);
assert.equal(api.activeAI(),null);
const detail=api.aiDetail('sectors','801150');
assert.ok(detail.includes('关键事实校验不通过'));
assert.ok(detail.includes('查看上次完整研究'));
assert.ok(detail.includes('历史证据'));
assert.ok(!detail.includes('<details class="inline-detail" open'));
assert.ok(detail.indexOf('暂停采用')<detail.indexOf('错误历史原文'));
assert.ok(detail.includes('历史AI 板块推论'));
analysis.ai_review_attempt={status:'FAILED',generated_at:'2026-10-06T20:00:00Z'};
assert.equal(api.latestAiAttempt(),null);
const summary=api.entrySummary();
assert.ok(summary.includes('主要风险'));
assert.ok(summary.includes('上次AI均线事实有误，已停用'));
assert.ok(summary.includes('资料不足，先暂停判断'));
const groups=api.groupEventSamples([{id:'a',title:'澳洲联储：AI板块回调影响消费'},
    {id:'b',title:'澳大利亚央行警示人工智能资产风险'},{id:'c',title:'另一个事件'}]);
assert.equal(groups.length,2);
assert.equal(groups[0].length,2);
assert.equal(api.eventSourceLabel('https://finance.sina.com.cn/7x24'),'来源频道（滚动更新）');
assert.equal(api.eventSourceLabel('https://www.pbc.gov.cn/article.html'),'原始条目来源');
const elements=Object.fromEntries(['update-date-row','update-date','update-scope','update-start','update-budget','update-task','update-options'].map(id=>[id,{value:'2026-09-30',dataset:{}}]));
let mode='current';
const status={analysis_dates:['2026-09-30'],ai_settings:{enabled:true,key_configured:true},
    ai_budget:{new_request_possible:false,remaining_requests:0,remaining_tokens:2000,reset_at:'2026-10-07T00:00:00Z'}};
const controls=vm.runInNewContext(slice(dashboard,'updateOptions','scheduleText')+';updateOptions',{
    analysis,status,$:id=>elements[id],document:{querySelector:()=>({value:mode})},timeText:String
});
controls();
assert.ok(elements['update-start'].textContent.includes('更新当前研究'));
assert.ok(elements['update-start'].textContent.includes('仅资料与缓存'));
assert.ok(elements['update-budget'].textContent.includes('不能新增AI请求'));
mode='replay';controls();
assert.ok(elements['update-start'].textContent.includes('生成 2026-09-30 复盘'));
assert.ok(elements['update-scope'].textContent.includes('23:59'));
status.ai_budget.new_request_possible=true;controls();
assert.equal(elements['update-start'].textContent,'生成 2026-09-30 复盘');
let routed='';
vm.runInNewContext(dashboard.match(/^function saveRoute\(\).*$/m)[0]+';saveRoute()',{
    view:'history',analysis,stockCode:'',sectorCode:'',historyMode:'reviews',URLSearchParams,
    views:['history'],window:{location:{search:''},history:{replaceState:(_,__,url)=>{routed=url;}}}
});
assert.ok(routed.includes('history=reviews'));
const evidenceApi=vm.runInNewContext(slice(dashboard,'evidenceFor','sourceLinks')+';evidenceFor',{
    analysis:{evidence:{'research:603259':{date:'new'}},historical_evidence:{'research:603259':{date:'old'}}}
});
assert.equal(evidenceApi('research:603259').date,'new');
assert.equal(evidenceApi('historical:research:603259').date,'old');
console.log('Experience UI: disqualified history, evidence versions, budget/mode labels, event grouping and review route passed.');
