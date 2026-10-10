const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('dashboard-research.js','utf8');
const fundPage=fs.readFileSync('dashboard.js','utf8').match(/^function fundsTable\(\).*$/m)[0];
const page=vm.runInNewContext(source+'\n'+fundPage+';fundsTable()',{
    analysis:{target_date:'2026-09-30',decision:{funds:{board_expected:1,
        board_flows:[{name:'冲突分类',net_cny:100000000,date:'2026-09-30'}]}},
        research_context:{funds:{coverage:{observed:0,expected:1,original_observed:1,conflicted:1},
            boards:[{name:'冲突分类',net_cny:null,windows:{'1':{status:'CONFLICT'}},source:'来源'}]}}},
    fundQuery:'',sorted:rows=>rows,esc:String,money:v=>Number.isFinite(v)?String(v/1e8):'—',
    tone:()=>'',sourceLinks:()=>'',sortHead:()=>'',Number
});
assert.ok(page.includes('数据冲突'));
assert.ok(page.includes('当前金额可用 0/1'));
const fieldSource=fs.readFileSync('dashboard.js','utf8').match(/function evidenceFields\(id,f\)[\s\S]*?\n}/)[0];
const international=vm.runInNewContext(fieldSource+';evidenceFields("research:international",[{code:"NASDAQ",date:"2026-10-02",value:100}])',{esc:String});
assert.ok(international.includes('NASDAQ'));
assert.ok(!international.includes('undefined undefined'));
const api=vm.runInNewContext(source+';({fundRanking,windowText,currentResearchAI,runFailureVisible})',{
    analysis:{ai_review:{business_status:'PASSED',evidence_status:'STALE',interpretation:{market:{}}}},
    Number,esc:x=>String(x),money:x=>Number.isFinite(x)?String(x):'—'
});
const rows=[{code:'x',net_cny:null},{code:'a',net_cny:2},{code:'b',net_cny:-3},{code:'z',net_cny:0}];
const before=JSON.stringify(rows);
assert.deepEqual(Array.from(api.fundRanking(rows,'in'),r=>r.code),['a']);
assert.deepEqual(Array.from(api.fundRanking(rows,'out'),r=>r.code),['b']);
assert.equal(JSON.stringify(rows),before);
assert.equal(api.windowText({status:'MISSING_DATA',net_cny:null}),'数据不足');
assert.equal(api.windowText({status:'CONFLICT'}),'数据冲突');
assert.equal(api.windowText({status:'READY',net_cny:0}),'0');
assert.equal(api.currentResearchAI(),null);
assert.equal(api.runFailureVisible({target_date:'2026-09-28',required:[{ok:false}]},'2026-09-30'),false);
assert.equal(api.runFailureVisible({target_date:'2026-09-30',required:[{ok:false}]},'2026-09-30'),true);
const noKey={target_date:'2026-09-30',decision:{ai:{status:'NOT_CONFIGURED',generated_at:'attempt-time'}}};
const timing=vm.runInNewContext(source+';researchTiming()',{
    analysis:noKey,esc:String,timeText:String
});
assert.ok(timing.includes('AI 未生成'));
assert.ok(!timing.includes('attempt-time'));
const dashboardSource=fs.readFileSync('dashboard.js','utf8');
const overviewSource=dashboardSource.slice(dashboardSource.indexOf('function renderOverview()'),dashboardSource.indexOf('function marketDetails()'));
const screen={innerHTML:''};
const emptyParts=Object.fromEntries(['head','sourceLinks','entrySummary','researchGlance','marketPicture',
    'marketResearchPanel','fundsOverview','priorityResearch','internationalOverview','eventsOverview',
    'evidenceTasks','opportunityList','compareSummary','boardPicture','marketDetails','nextDayResearch'].map(k=>[k,()=>'' ]));
vm.runInNewContext(overviewSource+';renderOverview()',{
    ...emptyParts,analysis:noKey,$:()=>screen,esc:String,num:String,pct:String,researchTiming:()=>'',
    sectors:()=>[],researchStocks:()=>[],diff:()=>({available:false}),activeAI:()=>null,
    opportunityData:()=>({counts:{waiting:1},selected_codes:['801780']})
});
assert.ok(!screen.innerHTML.includes('已存AI审阅'));
assert.ok(!screen.innerHTML.includes('机会线索'));
assert.ok(screen.innerHTML.includes('检查我的自选'));
const attemptState={ai_review:{business_status:'PASSED',generated_at:'2026-10-05T15:46:54Z'},
    ai_review_attempt:{status:'FAILED',generated_at:'2026-10-05T15:46:54Z'}};
const latestAttempt=vm.runInNewContext(source+';latestAiAttempt',{analysis:attemptState});
assert.equal(latestAttempt(),null);
attemptState.ai_review_attempt.generated_at='2026-10-05T15:47:00Z';
assert.equal(latestAttempt().status,'FAILED');
attemptState.ai_review_attempt.generated_at='invalid';
assert.equal(latestAttempt().status,'FAILED');
console.log('Research UI: rankings, zero, missing/conflicts, stale AI and older run isolation passed.');

const visualSource=fs.readFileSync('dashboard-visuals.js','utf8');
const scoreState={target_date:'2026-09-30',market:{regime:'分化'},decision:{market:{score:null,
    dimensions:[{id:'policy',name:'政策',score:null}]}},
    ai_review:{business_status:'PASSED',interpretation:{},ai_policy_assessment:{score:38,
        dimensions:[{id:'policy',name:'政策',score:65}]}}};
const scoreApi=vm.runInNewContext(source+'\n'+visualSource+';({entrySummary})',{
    analysis:scoreState,esc:String,num:String,timeText:String,pct:String,money:String,Number,sourceLinks:()=>''
});
assert.ok(scoreApi.entrySummary().includes('最终有效总分'));
assert.ok(!scoreApi.entrySummary().includes('38 / 100'));
assert.ok(scoreApi.entrySummary().includes('评分缺项'));
assert.equal(scoreState.decision.market.score,null);
scoreState.ai_review.evidence_status='STALE';
scoreState.ai_review.policy_evidence_status='CURRENT';
assert.ok(!scoreApi.entrySummary().includes('38 / 100'));
assert.equal(scoreState.decision.market.score,null);
scoreState.decision.market={score:38,action:'等待确认',score_basis:'final-effective-v1',dimensions:[{id:'policy',score:65}]};
assert.ok(scoreApi.entrySummary().includes('38 / 100'));
