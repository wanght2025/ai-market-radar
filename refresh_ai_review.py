"""Explicit AI-only review of a frozen publication; no collection or republishing."""
import argparse
import copy
import hashlib
import json
from pathlib import Path

from decision import interpret
from research_brief import evidence_registry
from storage import read_json, write_json, workspace_lock, json_digest, json_digest as publication_digest


def review_signature(publication, settings):
    source=Path(__file__).resolve().parent
    value={'publication':publication_digest(publication),'settings':settings,
           'prompt':(source/'prompts/decision_research.txt').read_text(encoding='utf-8'),
           'builder':hashlib.sha256((source/'decision.py').read_bytes()).hexdigest(),
           'fact_validator':hashlib.sha256((source/'research_facts.py').read_bytes()).hexdigest(),
           'transport_builder':hashlib.sha256((source/'daily_ai.py').read_bytes()).hexdigest(),
           'opportunity_builder':hashlib.sha256((source/'opportunities.py').read_bytes()).hexdigest(),
           'participation_builder':hashlib.sha256((source/'participation.py').read_bytes()).hexdigest(),
           'research_evidence':publication.get('research_evidence'),
           'research_context':publication.get('research_context'),
           'opportunities':publication.get('opportunity_input')}
    return json_digest(value)


def refresh_review(root, target, *, force=False, historical=False, saved_response=None):
    publication = read_json(root/'data/publications'/f'{target}.json',{})
    payload = publication.get('analysis',{})
    if payload.get('target_date') != target or not payload.get('decision'):
        raise ValueError('需要已正式发布且包含规则判断的存档')
    payload = copy.deepcopy(payload)
    if payload.get('research_window'):
        from collector import now_cst
        at=target+'T23:59:59+08:00' if historical else now_cst().isoformat()
        payload['research_window']={**payload['research_window'],'run_started_at':at,'external_information_cutoff':at}
    refs = payload.get('decision_evidence') or evidence_registry(payload)
    from research_context import build_context, add_evidence as context_evidence
    context_payload={**payload,'research_window':{'external_information_cutoff':target+'T23:59:59+08:00'} if historical else {}}
    payload['research_context']=build_context(root,context_payload,include_updates=True,
        observation_directory='historical-observations' if historical else 'observations')
    context_evidence(refs,payload['research_context'])
    from opportunities import snapshot, add_evidence
    # Minimal test publications have no industry facts; preserve their legacy path.
    if payload.get('industries'):
        payload['opportunities'] = snapshot(root,payload)
        add_evidence(refs,payload['opportunities'])
        write_json(root/'data/daily'/target/'opportunity-snapshot.json',{
            'publication_digest':publication_digest(publication),'opportunities':payload['opportunities'],
            'evidence':{k:v for k,v in refs.items() if k=='opportunities' or k.startswith('opportunity:')}})
    settings = read_json(root/'config.json',{}).get('deepseek')
    from participation import snapshot as participation_snapshot
    evidence = participation_snapshot(root,payload)
    from research_context import context_digest
    from participation import evidence_digest
    signature=review_signature({**publication,'opportunity_input':payload.get('opportunities'),
                               'research_context':context_digest(payload['research_context']),
                               'research_evidence':evidence_digest(evidence),'historical':historical},settings)
    review_name='ai-review-historical.json' if historical else 'ai-review.json'
    current=read_json(root/'data/daily'/target/review_name,{})
    if not force and current.get('status')=='READY' and current.get('business_status')=='PASSED' and current.get('review_signature')==signature:
        return {**current,'cached':True}
    if saved_response is not None:
        saved_input=read_json(root/'data/ai/inputs'/f"{saved_response.get('request_id','')}.json",{})
        original_request=saved_input.get('user_payload',{})
        if isinstance(original_request,str):original_request=json.loads(original_request)
        if original_request.get('target_date')!=target:
            raise ValueError('保存响应与所选日期不一致')
        payload['research_window']=original_request.get('research_window',{})
    replay={'saved_response':saved_response} if saved_response is not None else {}
    result = interpret(root,payload,copy.deepcopy(payload['decision']),refs,settings,save=False,force=force,**replay)
    record = {**result,'publication_digest':publication_digest(publication),
              'mode':'HISTORICAL_REVIEW' if historical else 'SNAPSHOT_REVIEW','review_signature':signature,
              'review_note':'历史快照再解读；不改写原判断，不计当时前瞻预测'}
    from participation import evidence_digest
    record['evidence_digest'] = evidence_digest(result.get('research_evidence') or evidence)
    from research_context import context_digest
    record['context_digest']=context_digest(payload['research_context'])
    record['research_context']=payload['research_context']
    from predictions import archive_ai_research
    archive_ai_research(root,payload,record)
    if payload.get('opportunities'):
        record['opportunities'] = payload['opportunities']
        record['opportunity_evidence'] = {k:v for k,v in refs.items() if k=='opportunities' or k.startswith('opportunity:')}
        source = read_json(root/'data/predictions'/f"{target}-{payload.get('prediction_id','')}.json",{})
        if source and result['status']=='READY':
            from predictions import evaluate
            from history import bars
            from datetime import datetime
            benchmark = read_json(root/'data/daily'/target/'index-sh000300.json',{}).get('rows',[])
            supplemental = {**source,'input':payload,'mode':'REPLAY_OR_LATE','issued_at':result['generated_at']}
            record['opportunity_review'] = evaluate(root,supplemental,bars(benchmark,target)[0],datetime.now().astimezone())
    # A failed attempt must not replace the last valid AI review.
    path = root/'data/daily'/target/(review_name if result['status']=='READY' else 'ai-review-attempt.json')
    write_json(path,record)
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date',required=True)
    parser.add_argument('--retry',action='store_true')
    args = parser.parse_args()
    from datetime import date
    date.fromisoformat(args.date)
    root = Path(__file__).resolve().parent
    with workspace_lock(root):
        print('RESEARCH_STAGE:正在生成并核验研究结论',flush=True)
        record = refresh_review(root,args.date,force=args.retry)
        print('RESEARCH_STAGE:正在保存研究结果',flush=True)
    print(json.dumps({key:record.get(key) for key in ('status','model','cached','usage','generated_at','request_id')},ensure_ascii=True))
    return 0 if record.get('status') in ('READY','DISABLED','NOT_CONFIGURED','BUDGET_EXCEEDED') else 1


if __name__ == '__main__':
    raise SystemExit(main())
