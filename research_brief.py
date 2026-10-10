"""Evidence-linked interpretation of a published snapshot; never called by HTTP GET."""
import json
from datetime import datetime, timezone

from deepseek_analysis import SYSTEM_PROMPT, analyze, finalize_validation
from storage import write_json, json_digest

SECTIONS = ('market', 'industries', 'clues', 'watchlist', 'limitations', 'observations')


def input_digest(payload):
    data = {k: v for k, v in payload.items() if k not in ('research_brief', 'evidence', 'reviews','data_errors','analysis_version','publication')}
    return json_digest(data)


def evidence_registry(payload):
    def entry(title, facts):
        return {'title': title, 'date': payload['target_date'], 'facts': facts}
    result = {
        'market': entry('全市场行情统计', payload.get('market', {})),
        'coverage': entry('覆盖范围与快照日期', {'coverage': payload.get('coverage', {}), 'snapshots': payload.get('snapshots', {})}),
        'limitations': entry('采集缺口与比较边界', {'limitations': payload.get('limitations', []), 'optional': payload.get('optional', {}), 'comparison_date': payload.get('comparison_date')}),
        'rules': entry('研究线索筛选规则', {'规则': payload.get('candidate_rule','涨幅≥3%、成交额≥1亿元、名称不含ST，按成交额取前50'), '数量': len(payload.get('candidates', [])), '说明': '未经收益验证；不是行业全部股票，也不是买入建议'}),
        'events': entry('目标日有限事件与资金样本', payload.get('enrichment', {})),
    }
    if payload.get('event_update'):
        result['event_update'] = entry('盘前新增资讯 · 独立发布时间',payload['event_update'])
    for row in payload.get('industries', []):
        result['industry:' + row['code']] = entry(row['name'] + ' · 行业统计', row)
    for row in payload.get('candidates', []):
        result['stock:' + row['代码']] = entry(row.get('名称', row['代码']) + ' · 行情与行业', row)
    for row in payload.get('watchlist_quotes', []):
        result['watch:' + row['code']] = entry(row['name'] + ' · 自选快照', row)
    return result


def validate_brief(content, target, evidence):
    data = json.loads(content)
    if not isinstance(data, dict) or data.get('target_date') != target:
        raise ValueError('解读日期不匹配')
    if not all(isinstance(data.get(k), str) and data[k].strip() for k in ('headline', 'summary')):
        raise ValueError('缺少摘要')
    sections = data.get('sections')
    if not isinstance(sections, list) or len(sections) != len(SECTIONS):
        raise ValueError('解读章节不完整')
    for section, expected in zip(sections, SECTIONS):
        if not isinstance(section, dict) or section.get('id') != expected:
            raise ValueError('章节不匹配')
        if not all(isinstance(section.get(k), str) and section[k].strip() for k in ('facts', 'interpretation', 'check')):
            raise ValueError('章节内容不完整')
        refs = section.get('source_ids')
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in evidence for ref in refs):
            raise ValueError('引用未匹配已核验数据')
    return data


def generate_brief(root, payload, settings):
    evidence = evidence_registry(payload)
    prompt = SYSTEM_PROMPT.replace('控制在约800字。', '完整解读约1500至2500字。').replace(
        '没有个股行业映射、权重或成交分布数据，不推断权重驱动、资金驱动或成交集中于少数个股。',
        '个股行业映射如提供可引用，但没有指数权重或全市场成交分布，不推断权重驱动或成交集中。')
    prompt += '''\n只输出一个 JSON 对象，禁止Markdown代码围栏。示例结构：
{"target_date":"输入日期","headline":"一句话结论","summary":"120字左右摘要",
"sections":[{"id":"market","facts":"已知事实与数值","interpretation":"推断及成立条件，不重述事实","check":"反例、缺口或下一步观察","source_ids":["market"]}]}。
sections必须按market、industries、clues、watchlist、limitations、observations顺序包含六章。
每章事实、推断、待验证各自完整说明。至少覆盖领先/落后行业、比较日变化、若干具名候选、自选股、数据缺口及可执行研究步骤。
source_ids只取输入evidence中的键，引用要能支持该章具体事实；避免笼统引用。
候选按成交额筛选，不代表全行业集中度或预测能力；金额以亿元展示，百分比与百分点区分。
不写具体交易建议，不把不同日期存档称为环比或连续趋势。不输出思维链，只给最终解读。'''
    clean = {k: v for k, v in payload.items() if k not in ('research_brief', 'evidence')}
    references = {key: {'title': row['title'], 'date': row['date']} for key, row in evidence.items()}
    result = analyze(root, {**clean, 'evidence': references}, settings, system_prompt=prompt, json_output=True)
    if result['status'] != 'READY':
        return result
    try:
        brief = validate_brief(result['content'], payload['target_date'], evidence)
    except (ValueError, TypeError) as error:
        finalize_validation(root,result,False,str(error))
        return {'status': 'FAILED', 'message': '完整解读的日期、章节或引用未通过校验，未发布'}
    finalize_validation(root,result,True)
    published = {k: result[k] for k in ('status', 'model', 'target_date', 'usage', 'cached')}
    published['reasoning_effort'] = settings.get('reasoning_effort', 'max')
    published.update(input_digest=input_digest(payload), generated_at=datetime.now(timezone.utc).isoformat(), brief=brief)
    write_json(root/'data'/'daily'/payload['target_date']/'research-brief.json', published)
    return published
