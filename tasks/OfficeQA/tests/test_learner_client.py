"""Synthetic-only completion budget checks for the GLM learner transport."""
import io
import json
import unittest
from unittest.mock import patch

import client
import rate_control


class Response(io.BytesIO):
    def __init__(self, body):
        super().__init__(json.dumps(body).encode())
        self.status=200


class Opener:
    def __init__(self, body):
        self.body=body
        self.requests=[]
    def open(self, request, timeout):
        self.requests.append(request)
        return Response(self.body)


def body(usage):
    return {"model":client.MODEL,"id":"synthetic-response",
            "choices":[{"message":{"role":"assistant","content":"Synthetic response only."},"finish_reason":"stop"}],
            "usage":usage}


class LearnerClientBudgetTests(unittest.TestCase):
    def complete(self,response,**options):
        opener=Opener(response)
        with patch.object(client.urllib.request,"build_opener",return_value=opener), \
                patch.object(rate_control,"GLOBAL",rate_control.RateScheduler(min_interval=0,backoffs=(0,))):
            result,meta=client.complete("synthetic-key",[{"role":"user","content":"Synthetic test."}],timeout=2,**options)
        return result,meta,opener

    def test_default_allows65536_and_smaller_remaining_is_preserved(self):
        response=body({"prompt_tokens":3,"completion_tokens":2,"total_tokens":5})
        result,meta,opener=self.complete(response)
        self.assertEqual(result,response)
        self.assertEqual(json.loads(opener.requests[0].data)["max_tokens"],65536)
        self.assertTrue(meta["completion_usage_valid"])
        self.assertTrue(meta["usage_complete"])
        result,_meta,opener=self.complete(response,max_tokens=7)
        self.assertEqual(json.loads(opener.requests[0].data)["max_tokens"],7)

    def test_success_without_integer_completion_usage_stops_without_retry(self):
        cases=[None,{}, {"total_tokens":5}, {"completion_tokens":None},
               {"completion_tokens":True},{"completion_tokens":-1},
               {"completion_tokens":"2"},{"completion_tokens":2.0}]
        for usage in cases:
            with self.subTest(usage=usage):
                result,meta,opener=self.complete(body(usage))
                self.assertIsNone(result)
                self.assertEqual(len(opener.requests),1)
                self.assertFalse(meta["completion_usage_valid"])
                self.assertFalse(meta["usage_complete"])
                self.assertTrue(meta["usage_is_lower_bound"])

    def test_over_budget_reported_tokens_are_retained_but_not_forwarded(self):
        response=body({"prompt_tokens":3,"completion_tokens":8,"total_tokens":11})
        result,meta,opener=self.complete(response,max_tokens=7)
        self.assertIsNone(result)
        self.assertEqual(len(opener.requests),1)
        self.assertEqual(meta["usage"]["completion_tokens"],8)
        self.assertTrue(meta["usage_complete"])
        self.assertIn("exceeded",meta["error"])

    def test_invalid_requested_budget_rejected_before_network(self):
        for requested in (0,-1,True,65537,"100"):
            with self.subTest(requested=requested), patch.object(client.urllib.request,"build_opener") as build:
                with self.assertRaises(ValueError):client.complete("synthetic-key",[],max_tokens=requested)
                build.assert_not_called()


if __name__=="__main__":unittest.main(verbosity=2)
