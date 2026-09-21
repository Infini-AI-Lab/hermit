"""Offline scheduler and transport regression tests; no real API or credentials."""
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

import broker
import client
import rate_control as R


class Response(io.BytesIO):
    def __init__(self, body, *, content_type='application/json', headers=None):
        super().__init__(body)
        self.headers = {'Content-Type':content_type, **(headers or {})}
        self.status = 200


class Opener:
    def __init__(self, entries):
        self.entries = list(entries)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request.full_url, request.data, timeout))
        entry = self.entries.pop(0)
        if isinstance(entry, Exception): raise entry
        return entry


def http_error(code=429, body=None, retry_after='0'):
    raw = json.dumps(body or {'error':{'message':'rate limit exceeded'}}).encode()
    return urllib.error.HTTPError(client.URL, code, 'fixture error', {'Retry-After':retry_after}, io.BytesIO(raw))


def completed():
    return {'id':'fixture-response','model':broker.MODEL,'status':'completed',
            'output':[{'type':'message','content':[{'type':'output_text','text':'fixture answer'}]}],
            'usage':{'input_tokens':3,'output_tokens':2,'total_tokens':5}}


def refused(**updates):
    result = {'model':broker.MODEL,'status':'failed','error':{'message':'rate limit exceeded'},
              'output':[], 'usage':{'input_tokens':0,'output_tokens':0,'total_tokens':0}}
    result.update(updates)
    return result


def responses_refusal_with_configuration(**updates):
    """Synthetic envelope matching the observed refusal/configuration shape.

    In particular: output=[], usage=null, text.format.type=text. Other values
    are inert fixture metadata, never copied benchmark input or actor output.
    """
    body = refused(usage=None, object='response', background=False,
        instructions='Synthetic input instructions.', max_output_tokens=32768,
        parallel_tool_calls=True, previous_response_id=None,
        reasoning={'effort':'high','summary':None}, store=False, temperature=0,
        text={'format':{'type':'text'}}, tool_choice='auto',
        tools=[{'type':'function','name':'fixture_tool','parameters':{'type':'object'}}],
        top_p=1, truncation='disabled', metadata={'fixture':True}, incomplete_details=None)
    body.update(updates)
    return body


def sse(body, *, prefix=()):
    events = list(prefix) + [{'type':'response.' + body['status'], 'response':body}]
    return b''.join(b'data: ' + json.dumps(e).encode() + b'\n\n' for e in events) + b'data: [DONE]\n\n'


class DummyServer:
    def __init__(self, _address, handler):
        self.handler = handler
        self.server_port = 12345
    def serve_forever(self): pass
    def shutdown(self): pass
    def server_close(self): pass


def relay_request(relay):
    handler = relay.server.handler.__new__(relay.server.handler)
    data = json.dumps({'model':broker.MODEL,'input':[],'tools':[],'stream':True}).encode()
    handler.path = '/v1/responses'
    handler.headers = {'Content-Length':str(len(data))}
    handler.rfile = io.BytesIO(data)
    handler.wfile = io.BytesIO()
    handler.status = None
    handler.send_response = lambda code: setattr(handler,'status',code)
    handler.send_header = lambda *_: None
    handler.end_headers = lambda: None
    handler.send_error = lambda code, *_: setattr(handler,'status',code)
    handler.do_POST()
    return handler


class SchedulerTests(unittest.TestCase):
    def test_responses_text_configuration_is_not_generated_output(self):
        body = responses_refusal_with_configuration()
        self.assertTrue(R.retryable_refusal(200,body,sse=True,observed_generation=False))
        # The format exception is scoped to an identified Responses/SSE envelope.
        self.assertFalse(R.retryable_refusal(429,body,sse=False))
        self.assertFalse(R.no_generation(body,responses_envelope=False))
        self.assertFalse(R.usage_is_reported(body['usage']))
        for update in ({'text':'Actual generated text'},
                       {'text':{'format':{'type':'text'},'content':'Unexpected generated content'}},
                       {'output':[{'type':'message','content':[]}]},
                       {'usage':{'input_tokens':1,'output_tokens':0}},
                       {'reasoning_content':'actual generated reasoning'},
                       {'tool_calls':[{'function':{'name':'fixture'}}]}):
            with self.subTest(update=update):
                self.assertFalse(R.retryable_refusal(200,responses_refusal_with_configuration(**update),sse=True))
        self.assertFalse(R.retryable_refusal(200,body,sse=True,observed_generation=True))

    def test_classification_never_retries_partial_or_nonzero_usage(self):
        self.assertTrue(R.retryable_refusal(429,None))
        self.assertTrue(R.retryable_refusal(200,refused(),sse=True))
        for body, generated in [(refused(output=[{'type':'message'}]),False),
                                (refused(usage={'input_tokens':1,'output_tokens':0}),False),
                                (refused(),True), (refused(error={'message':'internal error'}),False)]:
            self.assertFalse(R.retryable_refusal(200,body,sse=True,observed_generation=generated))
        self.assertFalse(R.retryable_refusal(200,refused(),sse=False))
        self.assertFalse(R.retryable_refusal(429,{'choices':[{'message':{'content':'partial'}}]}))

    def test_retry_after_seconds_and_dates(self):
        self.assertEqual(R.retry_after_seconds('30'),30)
        self.assertEqual(R.retry_after_seconds('Thu, 01 Jan 1970 00:01:00 GMT',now=30),30)
        self.assertEqual(R.retry_after_seconds('-3'),0)
        self.assertIsNone(R.retry_after_seconds('nan'))
        self.assertIsNone(R.retry_after_seconds('invalid'))
        scheduler = R.RateScheduler(backoffs=(15,30,60,120))
        self.assertEqual(scheduler.refused(1,45)['shared_cooldown_seconds'],45)
        self.assertEqual(scheduler.refused(2)['backoff_seconds'],30)
        self.assertEqual(scheduler.refused(3)['backoff_seconds'],60)
        self.assertEqual(scheduler.refused(4)['backoff_seconds'],120)

    def test_global_concurrency_and_dispatch_spacing(self):
        scheduler = R.RateScheduler(max_in_flight=2,min_interval=.02,backoffs=(0,))
        lock, starts, active, maximum = threading.Lock(), [], [0], [0]
        def op(_timeout,_number):
            with lock:
                starts.append(time.monotonic());active[0]+=1;maximum[0]=max(maximum[0],active[0])
            time.sleep(.055)
            with lock: active[0]-=1
            return R.Outcome('ok',{'usage_known':True})
        threads = [threading.Thread(target=lambda:R.run(op,timeout=2,request_sha256='same',scheduler=scheduler)) for _ in range(6)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(maximum[0],2)
        self.assertEqual(len(starts),6)
        self.assertTrue(all(b-a >= .017 for a,b in zip(starts,starts[1:])))

    def test_exactly_five_attempts_and_no_retry_on_timeout(self):
        calls=[]
        def refusal(_timeout,number):
            calls.append(number)
            raise R.AttemptFailure('rate',retryable=True,details={'usage_known':True})
        with self.assertRaises(R.TransportFailure) as ctx:
            R.run(refusal,timeout=2,request_sha256='immutable',scheduler=R.RateScheduler(min_interval=0,backoffs=(0,)))
        self.assertEqual(calls,[1,2,3,4,5])
        self.assertTrue(all(a['request_sha256']=='immutable' for a in ctx.exception.attempts))
        calls.clear()
        def failure(_timeout,number):
            calls.append(number);raise TimeoutError('Unknown server progress')
        with self.assertRaises(R.TransportFailure) as ctx:
            R.run(failure,timeout=2,request_sha256='immutable',scheduler=R.RateScheduler(min_interval=0))
        self.assertEqual(calls,[1])
        self.assertTrue(ctx.exception.attempts[0]['usage_unknown'])

    def test_stop_during_shared_cooldown_prevents_new_wire(self):
        stop=threading.Event();scheduler=R.RateScheduler(min_interval=0,backoffs=(10,))
        count=[]
        def refuse(_timeout,number):
            count.append(number)
            threading.Timer(.025,stop.set).start()
            raise R.AttemptFailure('rate',retryable=True,details={'usage_known':True})
        started=time.monotonic()
        with self.assertRaises(R.TransportFailure) as ctx:
            R.run(refuse,timeout=20,request_sha256='same',scheduler=scheduler,stop=stop)
        self.assertLess(time.monotonic()-started,.5)
        self.assertEqual(count,[1])
        self.assertFalse(ctx.exception.attempts[-1]['dispatched'])

    def test_wire_wall_clock_guard_stops_drip_and_late_response(self):
        response=Response(b'fixture')
        with self.assertRaises(TimeoutError):
            with R.wire_deadline(response,time.monotonic()+.02):time.sleep(.04)
        self.assertTrue(response.closed)
        def late(_timeout,_number):
            time.sleep(.03);return R.Outcome('late',{'usage_known':True,'usage':{'total_tokens':5}})
        with self.assertRaises(R.TransportFailure) as ctx:
            R.run(late,timeout=.01,request_sha256='same',scheduler=R.RateScheduler(min_interval=0))
        self.assertTrue(ctx.exception.attempts[0]['completed_after_deadline'])
        self.assertEqual(ctx.exception.attempts[0]['usage']['total_tokens'],5)


class ClientTests(unittest.TestCase):
    def test_chat_429_retry_same_bytes_missing_usage_stays_unknown(self):
        body={'id':'fixture','model':client.MODEL,'choices':[{'message':{'role':'assistant','content':'ok'}}],
              'usage':{'prompt_tokens':2,'completion_tokens':3,'total_tokens':5}}
        opener=Opener([http_error(),Response(json.dumps(body).encode())])
        with patch.object(client.urllib.request,'build_opener',return_value=opener), patch.object(R,'GLOBAL',R.RateScheduler(min_interval=0,backoffs=(0,))):
            result,meta=client.complete('fixture-secret',[{'role':'user','content':'fixture'}],timeout=2)
        self.assertEqual(result,body)
        self.assertEqual(len(opener.requests),2)
        self.assertEqual(opener.requests[0][1],opener.requests[1][1])
        self.assertTrue(meta['usage_is_lower_bound'])
        self.assertEqual(meta['usage']['total_tokens'],5)
        self.assertNotIn('fixture-secret',json.dumps(meta))

    def test_chat_partial_429_and_wrong_model_are_not_retried(self):
        entries=[http_error(body={'error':{'message':'rate limit'},'usage':{'prompt_tokens':1,'completion_tokens':0}}),
                 Response(json.dumps({'model':'another-model','choices':[{'message':{'content':'x'}}]}).encode())]
        for entry in entries:
            opener=Opener([entry])
            with patch.object(client.urllib.request,'build_opener',return_value=opener), patch.object(R,'GLOBAL',R.RateScheduler(min_interval=0,backoffs=(0,))):
                result,meta=client.complete('fixture-secret',[],timeout=2)
            self.assertIsNone(result)
            self.assertEqual(len(opener.requests),1)


class BrokerTests(unittest.TestCase):
    def run_relay(self, entries):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        output=Path(temp.name)
        relay=broker.Broker('fixture-secret',output,scheduler=R.RateScheduler(min_interval=0,backoffs=(0,)),request_timeout=2)
        relay.opener=Opener(entries)
        with patch.object(broker,'ThreadingHTTPServer',DummyServer):
            with relay:handler=relay_request(relay)
        return relay,handler,output

    def test_sse_rate_then_success_unchanged_wire_and_exact_payload(self):
        good=sse(completed())
        relay,handler,out=self.run_relay([Response(sse(refused()),content_type='text/event-stream'),
                                         Response(good,content_type='text/event-stream')])
        self.assertEqual(handler.status,200)
        self.assertEqual(handler.wfile.getvalue(),good)
        self.assertEqual(relay.opener.requests[0][1],relay.opener.requests[1][1])
        self.assertEqual(json.loads(relay.opener.requests[0][1])['max_output_tokens'],65536)
        summary=json.loads((out/'summary.json').read_text())
        self.assertEqual(summary['wire_attempts'],2)
        self.assertEqual(summary['rate_refusals'],1)
        self.assertTrue(summary['usage_complete'])
        self.assertFalse(summary['failed'])
        first=json.loads((out/'001/attempts/01/transport.json').read_text())
        self.assertEqual(first['status'],'rate_refused')
        self.assertTrue((out/'001/attempts/01/response.json').is_file())

    def test_actor_can_make_forty_logical_requests_without_count_cap(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            relay = broker.Broker('fixture-secret', output,
                scheduler=R.RateScheduler(min_interval=0, backoffs=(0,)), request_timeout=2)
            relay.opener = Opener([Response(sse(completed()), content_type='text/event-stream')
                                   for _ in range(40)])
            with patch.object(broker, 'ThreadingHTTPServer', DummyServer):
                with relay:
                    statuses = [relay_request(relay).status for _ in range(40)]
            self.assertEqual(statuses, [200] * 40)
            self.assertEqual(relay.calls, 40)
            self.assertEqual(len(relay.opener.requests), 40)
            self.assertEqual([json.loads(data)['max_output_tokens']
                              for _url, data, _timeout in relay.opener.requests],
                             [65536 - 2 * number for number in range(40)])
            summary = json.loads((output/'summary.json').read_text())
            self.assertIsNone(summary['limits']['requests'])
            self.assertEqual(summary['limits']['per_response'], 65536)
            self.assertEqual(summary['usage']['output_tokens'], 80)
            self.assertFalse(summary['failed'])
            self.assertTrue((output/'040/response.json').is_file())

    def test_last_output_token_is_usable_then_budget_stops_before_wire(self):
        first = completed()
        first['usage'] = {'input_tokens':3, 'output_tokens':65535, 'total_tokens':65538,
                          'output_tokens_details':{'reasoning_tokens':65534}}
        last = completed()
        last['usage'] = {'input_tokens':3, 'output_tokens':1, 'total_tokens':4}
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            relay = broker.Broker('fixture-secret', output,
                scheduler=R.RateScheduler(min_interval=0, backoffs=(0,)), request_timeout=2)
            relay.opener = Opener([Response(sse(body), content_type='text/event-stream')
                                   for body in (first, last)])
            with patch.object(broker, 'ThreadingHTTPServer', DummyServer):
                with relay:
                    statuses = [relay_request(relay).status for _ in range(3)]
            self.assertEqual(statuses, [200, 200, 400])
            self.assertEqual(relay.calls, 2)
            self.assertEqual(len(relay.opener.requests), 2)
            self.assertEqual([json.loads(data)['max_output_tokens']
                              for _url, data, _timeout in relay.opener.requests], [65536, 1])
            summary = json.loads((output/'summary.json').read_text())
            # output_tokens already includes reasoning; do not add it a second time.
            self.assertEqual(summary['usage']['output_tokens'], 65536)
            self.assertEqual(summary['failure_kind'], 'budget_exhausted')
            self.assertTrue(summary['usage_complete'])

    def test_old_request_cap_cannot_be_silently_reenabled(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, 'unbounded'):
                broker.Broker('fixture-secret', Path(temp), max_calls=24)

    def test_generated_response_requires_reliable_output_usage_and_is_not_retried(self):
        for invalid in (None, '2', 'unknown', 2.5, True, -1):
            with self.subTest(output_tokens=invalid):
                body = completed()
                body['usage'] = {'input_tokens':3, 'total_tokens':5}
                if invalid is not None: body['usage']['output_tokens'] = invalid
                relay, handler, output = self.run_relay([
                    Response(sse(body), content_type='text/event-stream')])
                self.assertEqual(handler.status, 400)
                self.assertEqual(handler.wfile.getvalue(), b'')
                self.assertEqual(len(relay.opener.requests), 1)
                summary = json.loads((output/'summary.json').read_text())
                self.assertEqual(summary['failure_kind'], 'output_usage_missing_or_invalid')
                self.assertFalse(summary['usage_complete'])
                self.assertTrue(summary['usage_is_lower_bound'])
                self.assertEqual(summary['usage_unknown_calls'], [1])
                self.assertEqual(summary['usage']['input_tokens'], 3)
                self.assertEqual(summary['usage']['total_tokens'], 5)
                self.assertFalse((output/'001/response.json').exists())
                self.assertTrue((output/'001/attempts/01/response.json').is_file())

    def test_provider_output_overshoot_is_recorded_but_never_forwarded(self):
        body = completed()
        body['usage'] = {'input_tokens':3, 'output_tokens':65537, 'total_tokens':65540}
        relay, handler, output = self.run_relay([
            Response(sse(body), content_type='text/event-stream')])
        self.assertEqual(handler.status, 400)
        self.assertEqual(handler.wfile.getvalue(), b'')
        self.assertEqual(len(relay.opener.requests), 1)
        summary = json.loads((output/'summary.json').read_text())
        self.assertEqual(summary['failure_kind'], 'provider_output_budget_exceeded')
        self.assertEqual(summary['usage']['output_tokens'], 65537)
        self.assertEqual(summary['usage']['total_tokens'], 65540)
        self.assertTrue(summary['usage_complete'])
        self.assertFalse((output/'001/response.json').exists())

    def test_provider_cannot_exceed_depleted_remaining_budget(self):
        first = completed()
        first['usage'] = {'input_tokens':3, 'output_tokens':65535, 'total_tokens':65538}
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            relay = broker.Broker('fixture-secret', output,
                scheduler=R.RateScheduler(min_interval=0, backoffs=(0,)), request_timeout=2)
            relay.opener = Opener([Response(sse(body), content_type='text/event-stream')
                                   for body in (first, completed())])
            with patch.object(broker, 'ThreadingHTTPServer', DummyServer):
                with relay:
                    self.assertEqual(relay_request(relay).status, 200)
                    rejected = relay_request(relay)
                    self.assertEqual(rejected.status, 400)
                    self.assertEqual(rejected.wfile.getvalue(), b'')
                    self.assertEqual(relay_request(relay).status, 400)
            self.assertEqual(len(relay.opener.requests), 2)
            self.assertEqual(json.loads(relay.opener.requests[1][1])['max_output_tokens'], 1)
            summary = json.loads((output/'summary.json').read_text())
            self.assertEqual(summary['failure_kind'], 'provider_output_budget_exceeded')
            self.assertEqual(summary['usage']['output_tokens'], 65537)

    def test_missing_or_malformed_usage_object_remains_unknown(self):
        for usage in (None, [], ['invalid'], 'invalid'):
            with self.subTest(usage=usage):
                body = completed() | {'usage':usage}
                _relay, handler, output = self.run_relay([
                    Response(sse(body), content_type='text/event-stream')])
                self.assertEqual(handler.status, 400)
                self.assertEqual(handler.wfile.getvalue(), b'')
                summary = json.loads((output/'summary.json').read_text())
                self.assertEqual(summary['failure_kind'], 'output_usage_missing_or_invalid')
                self.assertEqual(summary['usage_unknown_calls'], [1])
                self.assertTrue(summary['usage_is_lower_bound'])

    def test_observed_responses_configuration_refusal_retries_without_claiming_zero_cost(self):
        failed = responses_refusal_with_configuration()
        prefix = [{'type':'response.created','response':failed|{'status':'in_progress','error':None}},
                  {'type':'response.in_progress','response':failed|{'status':'in_progress','error':None}}]
        good = sse(completed())
        relay,handler,out = self.run_relay([
            Response(sse(failed,prefix=prefix),content_type='text/event-stream'),
            Response(good,content_type='text/event-stream')])
        self.assertEqual(handler.status,200)
        self.assertEqual(handler.wfile.getvalue(),good)
        self.assertEqual(len(relay.opener.requests),2)
        self.assertEqual(relay.opener.requests[0][1],relay.opener.requests[1][1])
        first = json.loads((out/'001/attempts/01/transport.json').read_text())
        self.assertEqual(first['status'],'rate_refused')
        self.assertFalse(first['observed_generation'])
        self.assertTrue(first['usage_unknown'])
        summary = json.loads((out/'summary.json').read_text())
        self.assertEqual(summary['rate_refusals'],1)
        self.assertEqual(summary['usage']['total_tokens'],5)
        self.assertTrue(summary['usage_is_lower_bound'])
        self.assertEqual(summary['usage_unknown_calls'],[1])
        self.assertFalse(summary['failed'])

    def test_sse_partial_generation_or_wrong_model_is_never_forwarded_or_retried(self):
        cases=[sse(refused(),prefix=[{'type':'response.output_text.delta','delta':'partial'}]),
               sse(refused(usage={'input_tokens':2,'output_tokens':0})),
               sse(completed()|{'model':'wrong-model'})]
        for raw in cases:
            relay,handler,_out=self.run_relay([Response(raw,content_type='text/event-stream')])
            self.assertEqual(handler.status,400)
            self.assertEqual(handler.wfile.getvalue(),b'')
            self.assertEqual(len(relay.opener.requests),1)
            self.assertTrue(relay.failed)


if __name__=='__main__':unittest.main(verbosity=2)
