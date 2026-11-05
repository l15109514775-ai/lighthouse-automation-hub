import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import asr_api


class NormalizeTest(unittest.TestCase):
    def test_strips_punctuation_and_width(self):
        self.assertEqual(asr_api.normalize('こんにちは、世界！'), 'こんにちは世界')
        self.assertEqual(asr_api.normalize('ＡＢＣ　１２３'), 'abc123')

    def test_cer(self):
        self.assertEqual(asr_api.cer('あいうえお', 'あいうえお'), 0.0)
        self.assertAlmostEqual(asr_api.cer('あいうえお', 'あいうえ'), 0.2)
        self.assertEqual(asr_api.cer('', ''), 0.0)
        self.assertEqual(asr_api.cer('あ', ''), 1.0)


class WindowTest(unittest.TestCase):
    def segments(self):
        return [
            {'start': 0.0, 'end': 5.0, 'text': 'あああ'},
            {'start': 5.0, 'end': 12.0, 'text': 'いいい'},
            {'start': 62.0, 'end': 70.0, 'text': 'ううう'},
        ]

    def test_window_text_uses_midpoint(self):
        rows = self.segments()
        self.assertEqual(asr_api.window_text(rows, 0, 60), 'あああいいい')
        self.assertEqual(asr_api.window_text(rows, 60, 120), 'ううう')

    def test_decision_paths(self):
        agree = asr_api.decide_window('あいうえお', 'あいうえお')
        self.assertEqual(agree['status'], 'agree')
        self.assertFalse(agree['arbiter_used'])

        minor = asr_api.decide_window('あいうえおかきくけこ', 'あいうえおかきくけ', agree=0.05, conflict=0.5)
        self.assertEqual(minor['status'], 'minor')

        conflict = asr_api.decide_window('あいうえお', 'かきくけこ', agree=0.2, conflict=0.4)
        self.assertEqual(conflict['status'], 'conflict')

        resolved = asr_api.decide_window('あいうえお', 'かきくけこ', 'あいうえお', agree=0.2, conflict=0.4)
        self.assertEqual(resolved['status'], 'resolved_primary')
        self.assertTrue(resolved['arbiter_used'])

        resolved_secondary = asr_api.decide_window('あいうえお', 'かきくけこ', 'かきくけこ', agree=0.2, conflict=0.4)
        self.assertEqual(resolved_secondary['status'], 'resolved_secondary')

        unresolved = asr_api.decide_window('あいうえお', 'かきくけこ', 'さしすせそ', agree=0.2, conflict=0.4)
        self.assertEqual(unresolved['status'], 'conflict')


class CrossCheckTest(unittest.TestCase):
    def test_arbiter_only_on_disagreement_and_capped(self):
        segments = [{'start': 0.0, 'end': 30.0, 'text': 'あいうえお'},
                    {'start': 60.0, 'end': 90.0, 'text': 'かきくけこ'}]
        calls = {'qwen': 0, 'arbiter': 0}

        def qwen(start, end):
            calls['qwen'] += 1
            return 'あいうえお' if start == 0 else 'さしすせそ'

        def arbiter(start, end):
            calls['arbiter'] += 1
            return 'さしすせそ'

        cfg = {'asr_window_seconds': 60, 'asr_agree_cer': 0.2, 'asr_conflict_cer': 0.4,
               'asr_max_arbiter_requests': 1}
        quality = asr_api.cross_check(segments, 120, cfg, qwen, arbiter)
        self.assertEqual(calls['qwen'], 2)
        self.assertEqual(calls['arbiter'], 1)
        self.assertEqual(quality['summary']['windows'], 2)
        self.assertEqual(quality['summary']['arbiter_requests'], 1)
        self.assertEqual(quality['summary']['conflicts'], 0)
        self.assertEqual(len(quality['flagged_segments']), 1)

    def test_no_arbiter_marks_conflict(self):
        segments = [{'start': 0.0, 'end': 30.0, 'text': 'あいうえお'}]
        quality = asr_api.cross_check(segments, 60, {'asr_window_seconds': 60}, lambda s, e: 'かきくけこ')
        self.assertEqual(quality['summary']['conflicts'], 1)
        self.assertEqual(quality['windows'][0]['status'], 'conflict')


class ParseTest(unittest.TestCase):
    def test_whisper(self):
        rows = asr_api.parse_whisper({'segments': [
            {'start': 0.0, 'end': 1.5, 'text': ' あ '},
            {'start': 1.5, 'end': 3.0, 'text': 'い'}]}, 10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['text'], 'あ')
        with self.assertRaises(asr_api.AsrError):
            asr_api.parse_whisper({'segments': []}, 10)

    def test_qwen(self):
        payload = {'choices': [{'finish_reason': 'stop', 'message': {'content': ' あい '}}]}
        self.assertEqual(asr_api.parse_qwen(payload), 'あい')
        with self.assertRaises(asr_api.AsrError):
            asr_api.parse_qwen({'choices': [{'finish_reason': 'length', 'message': {'content': 'あ'}}]})

    def test_arbiter(self):
        self.assertEqual(asr_api.parse_openai_text({'text': ' あい '}), 'あい')
        with self.assertRaises(asr_api.AsrError):
            asr_api.parse_openai_text({})


class HttpHelpersTest(unittest.TestCase):
    def test_multipart(self):
        body, content_type = asr_api._multipart({'model': 'whisper-1'}, {'file': ('a.mp3', b'abc', 'audio/mpeg')})
        self.assertIn(b'name="model"', body)
        self.assertIn(b'name="file"; filename="a.mp3"', body)
        self.assertIn(b'abc', body)
        self.assertTrue(content_type.startswith('multipart/form-data; boundary='))

    def test_https_only(self):
        with self.assertRaises(asr_api.AsrError):
            asr_api._require_https('http://api.openai.com/v1')

    def test_resolve_key_env_and_file(self):
        os.environ['ASR_TEST_KEY'] = 'sk-test'
        self.assertEqual(asr_api.resolve_key({}, 'ASR_TEST_KEY'), 'sk-test')
        del os.environ['ASR_TEST_KEY']
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'key.txt'
            path.write_text('sk-file\n', 'utf-8')
            os.environ['ASR_TEST_KEY_FILE'] = str(path)
            self.assertEqual(asr_api.resolve_key({}, 'ASR_TEST_KEY'), 'sk-file')
            del os.environ['ASR_TEST_KEY_FILE']
        with self.assertRaises(asr_api.AsrError):
            asr_api.resolve_key({}, 'ASR_MISSING_KEY')


if __name__ == '__main__':
    unittest.main()
