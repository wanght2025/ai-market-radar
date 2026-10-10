"""Explicit model edit over frozen research; no collection or republishing."""
import copy
import hashlib
import json
from pathlib import Path

from deepseek_analysis import analyze, finalize_validation
from participation import snapshot, add_evidence, validate_brief
from refresh_ai_review import publication_digest
from storage import read_json, write_json, workspace_lock


def refresh(root, target, *, force=False):
    publication = read_json(root/'data/publications'/f'{target}.json')
    payload = copy.deepcopy(publication['analysis'])
    path = root/'data/daily'/target/'ai-review.json'
    saved = read_json(path, {})
    if saved.get('status') != 'READY' or saved.get('publication_digest') != publication_digest(publication):
        raise ValueError('需要与正式报告匹配的有效AI研究')
    opportunities = saved['opportunities']
    status = snapshot(root, {**payload, 'opportunities':opportunities})
    refs = {}
    add_evidence(refs, status)
    prompt = (root/'prompts/entry_brief.txt').read_text(encoding='utf-8')
    sector_ai = {s['code']:s for s in saved['interpretation']['sectors']}
    request = {'target_date':target, 'participation':status,
        'market':payload['decision']['market'],
        'opportunities':[{**{k:v for k,v in s.items() if k in ('code','name','type','five_percent',
            'index_change_percent','relative','breadth','confirmation_checks','invalidation_checks')},
            'condition_text':'；'.join(c['label'] for c in s['confirmation_checks']),
            'prior_hypothesis':sector_ai.get(s['code'], {}).get('hypothesis')}
            for s in opportunities['sectors'] if s['code'] in opportunities['selected_codes']]}
    request_id = hashlib.sha256((prompt+json.dumps(request, ensure_ascii=False, sort_keys=True)).encode()).hexdigest()
    write_json(root/'data/ai/inputs'/f'{request_id}.json', {'system_prompt':prompt, 'user_payload':request})
    if not force and saved.get('entry_brief_review', {}).get('request_id') == request_id:
        validate_brief(saved['interpretation']['entry_brief'], status, opportunities)
        return {**saved['entry_brief_review'], 'cached':True}
    settings = read_json(root/'config.json')['deepseek']
    result = analyze(root, request, settings, system_prompt=prompt, json_output=True,force=force)
    result['request_id'] = request_id
    if result['status'] == 'READY':
        try:
            brief = json.loads(result['content'])
            validate_brief(brief, status, opportunities)
        except (ValueError, KeyError, TypeError) as error:
            result.update(status='FAILED', validation_error=str(error))
        finalize_validation(root,result,result['status']=='READY',result.get('validation_error',''))
    write_json(root/'artifacts/entry-clarity-ai-response.json', result)
    if result['status'] == 'READY':
        from participation import evidence_digest
        saved['research_evidence'] = status
        saved['evidence_digest'] = evidence_digest(status)
        saved.setdefault('opportunity_evidence', {}).update(refs)
        saved['interpretation']['entry_brief'] = brief
        saved['entry_brief_review'] = {k:result[k] for k in ('status','model','generated_at','request_id','usage')}
        write_json(path, saved)
        from predictions import archive_ai_research
        archive_ai_research(root,payload,{**result,'interpretation':saved['interpretation'],
            'research_evidence':status,'evidence_digest':saved['evidence_digest']})
    return result


def main():
    import argparse
    from datetime import date
    parser=argparse.ArgumentParser()
    parser.add_argument('--date',required=True)
    parser.add_argument('--retry',action='store_true')
    args=parser.parse_args()
    date.fromisoformat(args.date)
    root = Path(__file__).resolve().parent
    with workspace_lock(root):
        result = refresh(root, args.date,force=args.retry)
    print(json.dumps({k:result.get(k) for k in ('status','usage','cached','request_id','validation_error')}, ensure_ascii=True))
    return 0 if result.get('status') in ('READY','DISABLED','NOT_CONFIGURED','BUDGET_EXCEEDED') else 1


if __name__ == '__main__':
    raise SystemExit(main())
