"""Provider-specific balance refusal must not be mistaken for invalid credentials."""
import unittest

import httpx

from audit_core.model_settings import api_selection
from audit_core.qwen_model import QwenRequestError, complete_json


class ProviderQuotaErrorsTests(unittest.TestCase):
    def reject(self, status, body, *, provider='ppio'):
        key='sk_fake-private-value_ppio' if provider=='ppio' else 'sk-fake-private-value'
        selection=api_selection({'provider':provider,'api_key':key})
        observations=[]
        calls=[]
        def handler(request):
            calls.append(request)
            return httpx.Response(status, json=body)
        with self.assertRaises(QwenRequestError) as raised:
            complete_json(selection, [], system='Isolated diagnostic fixture',
                          transport=httpx.MockTransport(handler),progress=observations.append)
        error=raised.exception
        self.assertEqual(len(calls),1)
        self.assertNotIn(key,str(error))
        return error,observations

    def test_exact_balance_refusal_is_quota_for_top_level_and_nested_errors(self):
        for body in (
            {'code':'403','message':'not enough balance','private_debug':'must not leak'},
            {'error':{'code':'403','message':'not enough balance','api_key':'must not leak'}},
            {'error':{'message':' Not Enough Balance. '}},
        ):
            with self.subTest(body=body):
                error,metrics=self.reject(403,body)
                self.assertEqual(error.error_code,'quota')
                self.assertFalse(error.retryable)
                self.assertIn('账户余额不足',str(error))
                self.assertNotIn('权限',str(error))
                self.assertNotIn('must not leak',str(error))
                self.assertEqual(metrics[-1]['http_status'],403)
                self.assertNotIn('total_tokens',metrics[-1])

    def test_other_403_is_still_auth_including_echoed_balance_words(self):
        for body in (
            {'error':{'message':'IP denied','debug':'not enough balance'}},
            {'message':'permission denied'},
            {'message':{'quoted':'not enough balance'}},
            ['not enough balance'],
        ):
            with self.subTest(body=body):
                error,_=self.reject(403,body)
                self.assertEqual(error.error_code,'auth')
                self.assertFalse(error.retryable)

    def test_invalid_credentials_do_not_become_quota_from_body_words(self):
        error,_=self.reject(401,{'message':'not enough balance'})
        self.assertEqual(error.error_code,'auth')

    def test_other_providers_keep_their_existing_auth_and_quota_statuses(self):
        error,_=self.reject(403,{'message':'not enough balance'},provider='deepseek')
        self.assertEqual(error.error_code,'auth')
        for provider in ('ppio','deepseek'):
            with self.subTest(provider=provider):
                error,_=self.reject(402,{'message':'private provider text'},provider=provider)
                self.assertEqual(error.error_code,'quota')
                self.assertNotIn('private provider text',str(error))

    def test_rate_limit_remains_retryable_and_is_not_a_balance_error(self):
        error,_=self.reject(429,{'message':'not enough balance'})
        self.assertEqual(error.error_code,'rate_limit')
        self.assertTrue(error.retryable)


if __name__=='__main__':
    unittest.main()
