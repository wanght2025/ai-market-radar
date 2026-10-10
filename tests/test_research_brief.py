import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_brief import SECTIONS, evidence_registry, generate_brief, input_digest, validate_brief


class BriefTest(unittest.TestCase):
    def setUp(self):
        self.payload = {'target_date': '2026-09-30', 'market': {}, 'coverage': {}, 'candidates': []}
        self.brief = {'target_date': '2026-09-30', 'headline': '结论', 'summary': '摘要', 'sections': [
            {'id': key, 'facts': '事实', 'interpretation': '推断', 'check': '验证', 'source_ids': ['market']}
            for key in SECTIONS]}

    def test_rejects_unknown_references_wrong_dates_and_missing_sections(self):
        evidence = evidence_registry(self.payload)
        self.assertEqual(validate_brief(json.dumps(self.brief), '2026-09-30', evidence), self.brief)
        for change in ({'target_date': '2026-09-28'}, {'sections': self.brief['sections'][:-1]},
                       {'sections': [{**s, 'source_ids': ['invented']} for s in self.brief['sections']]}):
            with self.assertRaises(ValueError):
                validate_brief(json.dumps({**self.brief, **change}), '2026-09-30', evidence)

    def test_digest_tracks_data_but_not_attached_outputs(self):
        digest = input_digest(self.payload)
        self.assertEqual(input_digest({**self.payload, 'evidence': {}, 'research_brief': {}}), digest)
        self.assertNotEqual(input_digest({**self.payload, 'market': {'regime': '偏弱'}}), digest)

    def test_only_valid_complete_output_is_published(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('research_brief.analyze', return_value={'status': 'READY', 'model': 'test',
                    'target_date': '2026-09-30', 'usage': {}, 'cached': False,
                    'content': json.dumps(self.brief)}) as call:
                result = generate_brief(root, self.payload, {})
                path = root/'data/daily/2026-09-30/research-brief.json'
                original = path.read_bytes()
                self.assertEqual(result['input_digest'], input_digest(self.payload))
                self.assertTrue(call.call_args.kwargs['json_output'])
                call.return_value['content'] = '{}'
                self.assertEqual(generate_brief(root, self.payload, {})['status'], 'FAILED')
                self.assertEqual(path.read_bytes(), original)
