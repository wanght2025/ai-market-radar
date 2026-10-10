const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const visuals=fs.readFileSync('dashboard-visuals.js','utf8');
const entry=vm.runInNewContext(visuals.slice(visuals.indexOf('function entryDecision('),visuals.indexOf('function entrySummary('))+'; ({entryDecision,emotionState,indexChecks})',{
    analysis:{decision:{market:{index_trends:{sh000001:{close:9,ma20:10},sz399001:{close:11,ma20:10}}}}}
});
assert.equal(entry.entryDecision({action:'等待确认',score:33}),'暂不入场');
assert.equal(entry.entryDecision({action:'可考虑参与',score:75}),'值得入场');
assert.equal(entry.entryDecision({action:'回避',score:null}),'回避');
assert.equal(entry.entryDecision({action:'可考虑参与',score:null}),'暂停判断');
assert.equal(entry.emotionState(null),'未取得');
assert.equal(entry.emotionState(47),'分化 · 略偏弱');
assert.equal(entry.emotionState(20),'偏冷');
assert.equal(entry.emotionState(80),'偏热');
assert.deepEqual(Array.from(entry.indexChecks()),[false,true,null]);
const source=fs.readFileSync('dashboard.js','utf8');
// Test the actual presentation comparison without a DOM or any collection.
const comparison=source.slice(source.indexOf('function snapshotDiff('),source.indexOf('function diff()'));
const compare=vm.runInNewContext(comparison+'; snapshotDiff');
const savedWatch={target_date:'2026-09-30',watchlist_quotes:[{code:'000001',name:'平安银行','收盘':11.57}]};
const watchBefore=JSON.stringify(savedWatch);
const evidenceSource=source.slice(source.indexOf('function evidenceFor('),source.indexOf('function sourceLinks('));
const evidence=vm.runInNewContext(evidenceSource+'; evidenceFor',{analysis:savedWatch});
assert.equal(evidence('watch:000001').facts['收盘'],11.57);
assert.equal(evidence('watch:600000'),null);
assert.equal(JSON.stringify(savedWatch),watchBefore);
const current={target_date:'2026-09-30',decision:{version:'v2.1',stocks:[{code:'000001',state:'观察线索'},{code:'600000',state:'暂不参与'}]}};
const before={target_date:'2026-09-29',decision:{version:'v2.1',stocks:[{code:'000001',state:'等待确认'},{code:'600036',state:'观察线索'}]}};
assert.equal(compare(current,null).available,false);
assert.equal(compare(current,{...before,target_date:current.target_date}).available,false);
assert.equal(compare(current,{...before,decision:{...before.decision,version:'v2.2'}}).available,false);
assert.equal(compare({...current,decision:{...current.decision,candidate_version:'opportunity-v1'}},before).available,false);
assert.equal(compare({...current,decision:{...current.decision,candidate_version:'opportunity-v1'}},
    {...before,decision:{...before.decision,candidate_version:'opportunity-v1'}}).available,true);
const original=JSON.stringify([current,before]);
const result=compare(current,before);
assert.equal(result.available,true);
assert.equal(result.date,'2026-09-29');
assert.deepEqual(Array.from(result.added,r=>r.code),['600000']);
assert.deepEqual(Array.from(result.removed,r=>r.code),['600036']);
assert.deepEqual(Array.from(result.retained,r=>r.code),['000001']);
assert.equal(JSON.stringify([current,before]),original);
const gap=compare(current,{...before,target_date:'2026-09-28'});
assert.equal(gap.available,true);
assert.equal(gap.date,'2026-09-28'); // Labeled as a saved comparison, never silently called yesterday.
assert.equal(compare({target_date:'2026-09-30',candidate_rule:'new',candidates:[]},{target_date:'2026-09-29',candidate_rule:'old',candidates:[]}).available,false);
console.log('UI comparison: missing baseline, invalid date, changed rules, additions, exits, retained names and non-mutation passed.');
const routeSource=source.slice(source.indexOf('function readRoute('),source.indexOf('function saveRoute('));
const route=vm.runInNewContext(routeSource+'; readRoute',{URLSearchParams,views:['overview','sectors','stocks','watch','history']});
assert.equal(route('?view=stocks&date=2026-09-30&stock=600000').view,'stocks');
assert.equal(route('?view=stocks&date=2026-09-30&stock=600000').stock,'600000');
assert.equal(route('?view=unknown&stock=../../.env').view,'overview');
assert.equal(route('?view=unknown&stock=../../.env').stock,'');
let renders=0;
const imeSource=source.slice(source.indexOf('function handleSearchInput('),source.indexOf("document.querySelector('main').addEventListener('compositionstart'"));
const input=vm.runInNewContext(imeSource+'; handleSearchInput',{
    composing:false,sectorQuery:'',stockQuery:'',renderSectors:()=>renders++,renderStocks:()=>renders++,
    $:()=>({focus(){},setSelectionRange(){}})
});
input({isComposing:true,target:{id:'sector-search',value:'yi'}});
assert.equal(renders,0);
input({isComposing:false,target:{id:'sector-search',value:'医药',selectionStart:2,selectionEnd:2}});
assert.equal(renders,1);
