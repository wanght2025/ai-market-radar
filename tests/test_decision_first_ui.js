const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const research=fs.readFileSync('dashboard-research.js','utf8');
const visuals=fs.readFileSync('dashboard-visuals.js','utf8');
const dashboard=fs.readFileSync('dashboard.js','utf8');
const analysis={target_date:'2026-09-30',market:{breadth_percent:46.1},decision:{
    market:{action:'等待确认',index_trends:Object.fromEntries(['sh000001','sz399001','sh000300'].map(k=>[k,{close:99,ma20:100}]))},
    stocks:[{code:'603259',name:'药明康德',state:'观察线索',industry:{code:'801150'}}],
    ai:{status:'BUDGET_EXCEEDED'}},research_evidence:{stocks:[{code:'603259',buy_allowed:false}]},
    price_facts:{sectors:{'801150':{above_ma20:true}}},
    ai_review:{business_status:'FAILED',fact_validation:{status:'FAILED',reason:'均线相反'}}};
const opportunity={code:'801150',name:'医药生物',type:'反转观察',five_percent:-.13,breadth:84,
    confirmation_checks:[{label:'行业指数当日上涨',met:true}],
    invalidation_checks:[{id:'below_signal_reference',label:'指数跌破信号日20日均线',met:false}],signal_reference:7753.4605};
analysis.opportunities={sectors:[opportunity]};
const status={ai_budget:{new_request_possible:true}};
const before=JSON.stringify(analysis);
const api=vm.runInNewContext(research+visuals+';({marketDecisionSummary,aiResearchState,sectorDecisionView,entrySummary,entrySignal,plainResearchText})',{
    analysis,status,esc:String,num:(n)=>String(n),pct:n=>`${n}%`,timeText:String,sourceLinks:()=>'',
    sectors:()=>[{code:'801150',name:'医药生物',state:'等待确认',five:-.13,breadth:84}],
    opportunityOf:()=>opportunity
});
assert.equal(api.marketDecisionSummary().headline,'暂不新增参与，先观察');
assert.ok(api.marketDecisionSummary().reason.includes('0/3'));
assert.equal(api.marketDecisionSummary().ready,0);
assert.ok(api.entrySummary().includes('什么时候改变入场状态'));
assert.ok(api.entrySummary().includes('0 / 1 只通过'));
assert.equal(api.entrySignal().state,'unknown');
assert.ok(api.entrySummary().includes('role="img" aria-label="当前入场判断：资料不足，先暂停判断'));
assert.ok(api.aiResearchState().includes('现在可更新研究'));
assert.ok(api.aiResearchState().includes('上次AI均线事实有误，已停用'));
const sector=api.sectorDecisionView('801150');
assert.equal(sector.positioning,'观察反弹持续性');
assert.ok(sector.reason.includes('收盘高于MA20'));
assert.ok(sector.invalidation.includes('7753.4605'));
assert.ok(sector.next.includes('下一收盘复核'));
assert.equal(JSON.stringify(analysis),before);
analysis.decision.market.dimensions=[{weight:30,score:0},{weight:25,score:41.40625},
    {weight:15,score:null},{weight:10,score:75},{weight:20,score:46.9}];
assert.ok(Math.abs(api.marketDecisionSummary().ceiling-42.2315625)<1e-8);
assert.ok(api.marketDecisionSummary().next.includes('补满缺项'));
assert.equal(analysis.decision.market.dimensions[2].score,null);
opportunity.confirmation_checks=[];
assert.ok(api.sectorDecisionView('801150').next.includes('尚无确认条件'));
analysis.decision.market.action='回避';
assert.equal(api.marketDecisionSummary().headline,'暂停新增参与');
assert.equal(api.entrySignal().state,'avoid');
analysis.decision.market.action='可考虑参与';
assert.equal(api.marketDecisionSummary().headline,'市场可考虑参与，个股仍待核验');
analysis.research_evidence.stocks[0].buy_allowed=true;
assert.equal(api.marketDecisionSummary().headline,'可评估已通过核验的候选');
assert.equal(api.entrySignal().state,'unknown'); // Missing total score cannot produce an entry signal.
analysis.decision.market.score=70;
assert.equal(api.entrySignal().state,'enter');
analysis.ai_review={business_status:'PASSED',generated_at:'2026-10-06T23:45:35Z',interpretation:{market:{
    stance:'等待确认',judgment:'三大核心指数均低于20日均线，上涨广度不足50%，市场未能确认转强；部分行业反弹但整体偏弱。'}}};
assert.equal(api.entrySignal().state,'wait'); // An independent cautious opinion remains visible.
const plain=api.entrySummary();
const originalJudgment=analysis.ai_review.interpretation.market.judgment;
assert.ok(plain.includes(originalJudgment));
assert.ok(plain.indexOf('entry-signal')<plain.indexOf('ai-market-paragraph'));
assert.ok(plain.indexOf('entry-score-list')<plain.indexOf('ai-market-paragraph'));
assert.ok(plain.indexOf('entry-signal')<plain.indexOf('entry-support'));
assert.ok(plain.indexOf('判断依据与数据说明')<plain.indexOf('原评分规则'));
assert.ok(api.plainResearchText('MA20').includes('20日均线'));
assert.equal(api.plainResearchText('MA200与MA50'),'MA200与MA50');
assert.equal(analysis.ai_review.interpretation.market.judgment,originalJudgment);
analysis.ai_review.interpretation.market.stance='证据不足';
assert.equal(api.entrySignal().state,'unknown');
analysis.ai_review.interpretation.market.stance='回避';
assert.equal(api.entrySignal().state,'avoid');
analysis.ai_review.evidence_status='STALE';
assert.ok(api.entrySummary().includes('当前没有可用的AI判断'));
assert.equal(api.entrySignal().state,'enter');
analysis.decision.market.action=undefined;
assert.equal(api.marketDecisionSummary().headline,'资料不足，暂不评估参与');
assert.equal(api.entrySignal().state,'unknown');
analysis.research_evidence.stocks=[];
assert.ok(api.entrySummary().includes('尚未查齐'));
const nodes=Object.fromEntries(['update-track','research-progress','progress-steps','progress-time','update-start'].map(k=>[k,{classList:{toggle(){}},setAttribute(){}}]));
const start=dashboard.indexOf('function renderUpdateProgress()'),end=dashboard.indexOf('async function loadAnalysis(',start);
const progress=vm.runInNewContext(dashboard.slice(start,end)+';renderUpdateProgress',{
    status:{running:false,exit_code:0,progress:{completed:null,total:4,steps:[]}},posting:false,$:id=>nodes[id]
});
progress();
assert.equal(nodes['update-track'].hidden,true);
console.log('Decision-first UI: explicit actions, candidate gates, concrete sector checks, unknown states, unlimited requests and finished progress passed.');
