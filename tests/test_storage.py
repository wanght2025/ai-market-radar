import tempfile
import os
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from storage import write_bytes, write_text, json_digest


class StorageTest(unittest.TestCase):
    def test_json_identity_preserves_existing_cache_serialization(self):
        from predictions import digest
        from refresh_ai_review import publication_digest
        value={'日期':'2026-09-30','score':93.7,'items':[False,None,65]}
        expected='d06d99f7b28824096c03bc0889138e9d07e6e0af33910ff9625f9d0210e06dfd'
        self.assertEqual(json_digest(value),expected)
        self.assertIs(digest,json_digest)
        self.assertIs(publication_digest,json_digest)
        self.assertEqual(json_digest(dict(reversed(list(value.items())))),expected)
        self.assertNotEqual(json_digest({**value,'items':list(reversed(value['items']))}),expected)

    def test_text_and_binary_keep_their_original_newline_behavior(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'nested/report.txt'
            write_text(path,'中文\n第二行')
            self.assertEqual(path.read_bytes(),('中文'+os.linesep+'第二行').encode('utf-8'))
            write_bytes(path,b'first\nsecond')
            self.assertEqual(path.read_bytes(),b'first\nsecond')

    def test_windows_reader_releases_before_atomic_write_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'report.json';path.write_text('old',encoding='utf-8')
            reader=path.open('rb')
            release=threading.Timer(.03,reader.close)
            release.start()
            try:
                write_text(path,'new')
                self.assertEqual(path.read_text(encoding='utf-8'),'new')
            finally:
                reader.close();release.join()

    def test_persistent_permission_failure_is_bounded_and_keeps_old_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'report.json';path.write_bytes(b'old')
            with patch('storage.os.replace',side_effect=PermissionError('locked')) as replace:
                with self.assertRaises(PermissionError):write_bytes(path,b'new')
            self.assertEqual(replace.call_count,3)
            self.assertEqual(path.read_bytes(),b'old')
            self.assertEqual(list(Path(folder).glob('*.tmp')),[])
