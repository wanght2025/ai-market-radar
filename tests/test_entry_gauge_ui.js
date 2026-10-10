const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const analysis={target_date:'2026-09-30',market:{breadth_percent:46},decision:{
    market:{action:'等待确认',score:null,index_trends:Object.fromEntries(['sh000001','sz399001','sh000300'].map(k=>[k,{close:99,ma20:100}])),
        dimensions:['technical','funds','policy','international','sentiment'].map((id,i)=>({id,weight:[30,25,15,10,20][i],score:i===2?null:40,reason:'保存的评分依据'}))},
    stocks:[{code:'603259'}]},research_evidence:{stocks:[{code:'603259',buy_allowed:false}]}};
const esc=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
const api=vm.runInNewContext(fs.readFileSync('dashboard-research.js','utf8')+fs.readFileSync('dashboard-visuals.js','utf8')+';({entrySignal,entryGaugeLevel,entryGauge,entrySummary,scoreDimensions,scoreExplanation,marketForecast,marketCoverage})',{
    analysis,esc,num:String,timeText:String,sourceLinks:()=>'',Number
});
const before=JSON.stringify(analysis);
assert.equal(api.entryGaugeLevel(api.entrySignal()),null);
const html=api.entrySummary();
assert.equal((html.match(/class="entry-score-row"/g)||[]).length,5);
assert.equal((html.match(/class="gauge-band gauge-band-/g)||[]).length,5);
assert.ok(html.includes('尚未提供市场预测'));
assert.ok(api.scoreExplanation('policy').includes('待补证据'));
assert.equal(JSON.stringify(analysis),before);
analysis.decision.market.action='回避';assert.equal(api.entryGaugeLevel(api.entrySignal()),1);
analysis.decision.market.action='等待确认';analysis.decision.market.score=50;analysis.decision.market.index_trends.sh000001.close=101;
assert.equal(api.entryGaugeLevel(api.entrySignal()),3);
analysis.decision.market.action=undefined;assert.equal(api.entryGaugeLevel(api.entrySignal()),null);
analysis.decision.market.action='可考虑参与';analysis.decision.market.score=79.99;
analysis.research_evidence.stocks=[{code:'000001',buy_allowed:true}];
assert.notEqual(api.entrySignal().state,'enter'); // Foreign candidates cannot unlock entry.
analysis.research_evidence.stocks=[{code:'603259',buy_allowed:true}];
assert.equal(api.entryGaugeLevel(api.entrySignal()),4);
analysis.decision.market.score=80;
assert.equal(api.entryGaugeLevel(api.entrySignal()),4); // Missing dimension cannot show the highest band.
analysis.decision.market.dimensions[2].score=80;
assert.equal(api.entryGaugeLevel(api.entrySignal()),5);
analysis.ai_review={business_status:'PASSED',interpretation:{market:{stance:'回避'}}};
assert.equal(api.entryGaugeLevel(api.entrySignal()),1); // AI risk takes precedence over a high score.
analysis.ai_review.interpretation.market.stance='证据不足';assert.equal(api.entryGaugeLevel(api.entrySignal()),null);
const forecast={basis_date:'2026-09-30',confidence:'低',confidence_reason:'样本有限',next_day:'下一日预测',near_term:'3—5日预测',
    scenarios:Object.fromEntries(['基准','偏强','偏弱'].map(k=>[k,{outlook:'走势',trigger:'触发',invalidation:'失效'}]))};
analysis.ai_review.mode='SNAPSHOT_REVIEW';
const prediction=api.marketForecast({market:{forecast}});
assert.ok(prediction.includes('不计当时前瞻预测'));
assert.ok(prediction.includes('下一日预测')&&prediction.includes('3—5日预测'));
assert.equal((prediction.match(/<dt>失效条件<\/dt>/g)||[]).length,3);
assert.ok(prediction.includes('<details class="analysis-detail forecast-full">'));
const defaultText=html=>html.replace(/<details\b[^>]*>([\s\S]*?)<\/details>/g,(_,body)=>body.match(/<summary>[\s\S]*?<\/summary>/)?.[0]||'');
assert.ok(!defaultText(prediction).includes('下一日预测'));
assert.ok(defaultText(prediction).includes('走势'));
analysis.ai_review.interpretation.market={stance:'等待确认',headline:'一句话结论',opportunity_bias:'当前重点',judgment:'完整专业研判',
    fund_view:'资金全文',event_view:'政策全文',international_view:'海外全文',forecast};
const concise=api.entrySummary(),visible=defaultText(concise);
assert.ok(visible.includes('一句话结论')&&visible.includes('当前重点'));
for(const text of ['完整专业研判','资金全文','政策全文','海外全文','下一日预测','3—5日预测']){
    assert.ok(concise.includes(text));assert.ok(!visible.includes(text));
}
assert.equal((concise.match(/完整专业研判/g)||[]).length,1); // No repeated full judgment.
assert.equal((visible.match(/class="entry-score-row"/g)||[]).length,5);
analysis.decision.market.dimensions[2].score=null;
analysis.ai_review.ai_policy_assessment={score:37,dimensions:[{id:'policy',score:65,reason:'同期正文支持',source_ids:['event:p']}]};
assert.ok(!api.entrySummary().includes('37 / 100'));
assert.ok(api.scoreExplanation('policy').includes('待补证据'));
assert.equal(analysis.decision.market.dimensions[2].score,null); // Supplemental display never changes frozen scores or entry.
analysis.data_coverage={gaps:[{id:'mapping',title:'板块资金归因',status:'分类不同',cause:'两套成员未核对',impact:'不能跨分类归因',action:'核对成员'}],funds_observed:128,funds_expected:128,etf_comparable:10,etf_total:10};
const coverage=api.marketCoverage();
assert.ok(defaultText(coverage).includes('ETF日间比较 10/10'));
assert.ok(coverage.includes('不能跨分类归因')&&!defaultText(coverage).includes('不能跨分类归因'));
analysis.ai_review.evidence_status='STALE';analysis.ai_review.policy_evidence_status='NEEDS_REVIEW';
assert.ok(!api.entrySummary().includes('同期官方正文 · AI补评'));
assert.ok(!api.entrySummary().includes('完整专业研判'));
const code=fs.readFileSync('dashboard.js','utf8');
const functions=code.slice(code.indexOf('let scoreTrigger=null;'),code.indexOf('let evidenceTrigger=null;'));
const nodes={};for(const id of ['score-popover','score-content','score-close'])nodes[id]={hidden:true,style:{},offsetWidth:380,offsetHeight:400,focus(){this.focused=true}};
const trigger={isConnected:true,attributes:{},setAttribute(k,v){this.attributes[k]=v},getBoundingClientRect:()=>({left:900,bottom:680}),focus(){this.focused=true}};
const popup=vm.runInNewContext(functions+';({openScorePopover,closeScorePopover})',{window:{innerWidth:1024,innerHeight:768},$:id=>nodes[id],scoreExplanation:api.scoreExplanation,closeEvidence(){}});
popup.openScorePopover('technical',trigger);
assert.equal(nodes['score-popover'].hidden,false);assert.equal(nodes['score-popover'].style.left,'632px');
assert.equal(nodes['score-close'].focused,true);assert.equal(trigger.attributes['aria-expanded'],'true');
popup.openScorePopover('technical',trigger);assert.equal(nodes['score-popover'].hidden,true);assert(trigger.focused);
console.log('Five-band gauge: all states, missing/foreign/stale evidence gates, visible scores, forecast timing and popover interactions passed.');
