from __future__ import annotations
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jalert.reliable_delivery import ReliableDelivery, quality_report
from jalert.enhanced_delivery import Delivery
from tools import history_backfill

TRANSLATION = '磁性界面的交换偏置与自旋输运通过实验方法进行测量，进一步比较了不同磁性层之间的耦合以及电学测量中的变化。'
ORIGINAL = 'Exchange bias in magnetic heterostructures was measured at 10 K and 20 K. The magnetic interfaces generated a response of 15 mT.'


def makeitem(i, abstract=ORIGINAL):
    return (SimpleNamespace(uid=f'doi:10.2000/test{i}',title=f'Exchange bias paper {i}',journal='Physical Review B',date='2026-10-21',doi=f'10.2000/test{i}',url='',abstract=abstract), SimpleNamespace(labels=['交换偏置'],score=18))


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.delivery = ReliableDelivery(self.root)
        self.patcher = patch.dict(os.environ, {'JALERT_SERVERCHAN_SENDKEY':'test_key', 'JALERT_BARK_ENABLED':'true', 'JALERT_BARK_DEVICE_KEY':'device_test', 'JALERT_BARK_BODY_BYTES':'1500', 'JALERT_WECHAT_MAX_MESSAGES_DAY':'4', 'JALERT_BARK_MAX_MESSAGES_DAY':'12', 'JALERT_WECHAT_MAX_CHARS':'3500'})
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_dual_identical_bodies_and_no_duplicate_ack(self):
        for i in range(2):
            self.delivery.enqueue([makeitem(i)])
        sent=[]
        with patch.object(self.delivery, '_get_translation', return_value=TRANSLATION):
            result=self.delivery.flush_both(send_func=lambda ch,title,body: sent.append((ch,title,body)) or True, max_pages=10)
        self.assertEqual(result['pending_papers'],0)
        self.assertEqual(len(sent),2)
        self.assertEqual(sent[0][2],sent[1][2])
        self.assertEqual([x[0] for x in sent],['wechat','bark'])
        self.assertIn('积压提醒',sent[0][2])
        again=self.delivery.flush_both(send_func=lambda *args: self.fail('duplicate notification'), max_pages=2)
        self.assertEqual(again['pending_papers'],0)

    def test_failed_bark_resumes_without_repeat_wechat(self):
        self.delivery.enqueue([makeitem(0)])
        sent=[]
        with patch.object(self.delivery, '_get_translation', return_value=TRANSLATION):
            one=self.delivery.flush_both(send_func=lambda ch,title,body: sent.append(ch) or ch == 'wechat',max_pages=1)
            self.assertEqual(one['pending_papers'],1)
            self.assertEqual(sent,['wechat','bark'])
            other=ReliableDelivery(self.root)
            with patch.object(other, '_get_translation', return_value=TRANSLATION):
                two=other.flush_both(send_func=lambda ch,title,body: sent.append(ch) or True, max_pages=2)
        self.assertEqual(two['pending_papers'],0)
        self.assertEqual(sent,['wechat','bark','bark'])

    def test_long_translation_reassembled_without_truncation(self):
        self.delivery.enqueue([makeitem(0)])
        translation=TRANSLATION*17
        sent=[]
        with patch.object(self.delivery, '_get_translation', return_value=translation):
            result=self.delivery.flush_both(send_func=lambda ch,title,body: sent.append((ch,body)) or True,max_pages=20)
        self.assertGreater(len(sent),2)
        self.assertTrue(all(len(body.encode('utf-8'))<=1500 for ch,body in sent))
        self.assertTrue(all(sent[i][1]==sent[i+1][1] for i in range(0,len(sent),2)))
        self.assertEqual(result['pending_papers'],0)
        self.assertEqual(result['completed_papers_this_run'],1)
        # Remove message frames; all page payload excerpts must concatenate exactly to the full block.
        pieces=[]
        for ch,b in sent:
            if ch=='wechat':
                pos=b.index('】仍有 ') if '】仍有 ' in b else -1
                body=b.split('\n',2)[2].rsplit('\n完整双语卡片墙：',1)[0]
                pieces.append(body)
        self.assertEqual(''.join(pieces), self.delivery._block(self.delivery.queue['items']['doi:10.2000/test0'], translation))

    def test_quality_digits_caught_and_acronyms_warn(self):
        bad=quality_report('At 10 K and 50 mT, SOT increased.', '在10 K条件下，自旋轨道力矩增强。')
        self.assertIn('50',bad['missing_numbers'])
        self.assertFalse(bad['ok'])
        good=quality_report('At 10 K and 50 mT, SOT increased.', '在10 K和50 mT的条件下，自旋轨道力矩SOT增强。')
        self.assertTrue(good['ok'])

    def test_bark_key_required_when_enabled(self):
        with patch.dict(os.environ, {'JALERT_BARK_DEVICE_KEY':''}):
            with self.assertRaisesRegex(ValueError,'BARK_DEVICE_KEY'):
                self.delivery._channels()

    def test_bark_api_uses_json_and_body_is_unchanged(self):
        self.delivery.enqueue([makeitem(0)])
        with patch('jalert.reliable_delivery._http_json',return_value={'code':200}) as sender:
            self.assertTrue(self.delivery._bark('test title','完整中文译文'))
        url, payload = sender.call_args.args
        self.assertEqual(url,'https://api.day.app/push')
        self.assertEqual(payload['body'],'完整中文译文')
        self.assertEqual(payload['title'],'test title')
        self.assertIn('device_key', payload)

    def test_wechat_quota_exhaustion_retains_queue(self):
        self.delivery.enqueue([makeitem(0)])
        day=__import__('jalert.enhanced_delivery',fromlist=['_now_date'])._now_date()
        self.delivery.queue['usage'][day]={'wechat_attempts':4,'ai_attempts':0}
        self.delivery._save_queue()
        with patch.object(self.delivery, '_get_translation', side_effect=AssertionError('must not translate at quota')):
            result=self.delivery.flush_both(send_func=lambda *args: self.fail('must not send'))
        self.assertEqual(result['pending_papers'],1)
        self.assertIsNone(result['active_message'])

    def test_quality_rejection_keeps_pending(self):
        self.delivery.enqueue([makeitem(0)])
        with patch.object(self.delivery, '_get_translation', side_effect=ValueError('critical omission')):
            result=self.delivery.flush_both(send_func=lambda *args: self.fail('must not send'))
        self.assertEqual(result['pending_papers'],1)
        self.assertIn('critical omission',self.delivery.queue['items']['doi:10.2000/test0']['last_ai_error'])

    def test_backfill_does_not_enqueue_or_push(self):
        records=[{'doi':'10.1000/historic','title':'Historic exchange bias', 'abstract':'TRUNCATED ENGLISH'}]
        with patch.object(history_backfill,'collect',return_value=(records,[],[])), \
             patch.object(history_backfill.ReliableDelivery,'_get_translation',return_value=TRANSLATION) as translate:
            result=history_backfill.backfill(self.root,max_papers=1,lookup=lambda doi:(ORIGINAL,'OpenAlex'))
        self.assertEqual(result['translated'],1)
        self.assertEqual(result['attempted'],1)
        self.assertEqual(ReliableDelivery(self.root).pending_count(),0)
        self.assertEqual(translate.call_count,1)

    def test_empty_history_source_does_not_fake_translation(self):
        records=[{'doi':'10.1000/historic','title':'Historic exchange bias', 'abstract':'TRUNCATED ENGLISH'}]
        with patch.object(history_backfill,'collect',return_value=(records,[],[])):
            result=history_backfill.backfill(self.root,max_papers=1,lookup=lambda doi:('', ''))
        self.assertEqual(result['missing_source_abstract'],1)
        self.assertEqual(result['translated'],0)

if __name__=='__main__':
    unittest.main()
