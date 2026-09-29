import io
import json
import threading
import unittest
from broker import MODEL, NoTerminalEvent, normalized, public, read_wire


class Stream(io.BytesIO):
    def __init__(self, events):
        super().__init__(b''.join(b'data: ' + json.dumps(e).encode() + b'\n\n' for e in events))
        self.headers = {'Content-Type':'text/event-stream'}


class TransportTests(unittest.TestCase):
    def test_dropped_stream_reports_whether_generation_was_observed(self):
        created = {'type':'response.created','response':{'id':'fixture','status':'in_progress'}}
        with self.assertRaises(NoTerminalEvent) as ctx: read_wire(Stream([created]), threading.Event())
        self.assertFalse(ctx.exception.generated)
        self.assertIsInstance(ctx.exception, ValueError)
        delta = {'type':'response.output_text.delta','delta':'partial'}
        with self.assertRaises(NoTerminalEvent) as ctx: read_wire(Stream([created, delta]), threading.Event())
        self.assertTrue(ctx.exception.generated)
        stop = threading.Event(); stop.set()
        with self.assertRaises(TimeoutError): read_wire(Stream([created]), stop)
    def test_exact_model_and_budgets(self):
        x = normalized({'model': MODEL, 'input': [], 'tools': [], 'reasoning': {'effort':'high'}}, 1000)
        self.assertEqual(x['max_output_tokens'], 1000)
        self.assertEqual(x['reasoning'], {'effort':'high'})
        self.assertFalse(x['store'])
        with self.assertRaises(ValueError): normalized({'model':'gpt-6-astra'},1000)
    def test_no_server_network_tools_or_cross_task_state(self):
        for tool in [{'type':'web_search'}, {'type':'mcp'}, {'type':'function','server_url':'https://x'},
                     {'type':'namespace','tools':[{'type':'sse'}]}]:
            with self.assertRaises(ValueError): normalized({'model':MODEL,'tools':[tool]},1000)
        with self.assertRaises(ValueError): normalized({'model':MODEL,'previous_response_id':'other-task'},1000)
        with self.assertRaises(ValueError): normalized({'model':MODEL,'input':[{'type':'input_file','file_url':'https://x'}]},1000)
        with self.assertRaises(ValueError): normalized({'model':MODEL,'input':[{'type':'input_image','image_url':'https://x'}]},1000)
        normalized({'model':MODEL,'input':[{'type':'input_image','image_url':'data:image/png;base64,iVBORw0KGgo='}]},1000)
    def test_private_reasoning_not_logged(self):
        self.assertEqual(public({'input':[{'type':'reasoning','summary':['private']},{'type':'message','content':'public'}],
                                 'reasoning_content':'private'}), {'input':[{'type':'message','content':'public'}]})

if __name__ == '__main__': unittest.main()
