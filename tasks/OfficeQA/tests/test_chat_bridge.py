import json
import threading
import unittest

import broker
import chat_bridge


def sse(*events, done=True):
    lines = []
    for e in events:
        lines += [b'data: ' + json.dumps(e).encode() + b'\n', b'\n']
    if done:
        lines += [b'data: [DONE]\n', b'\n']
    return lines


class Upstream:
    def __init__(self, lines):
        self.lines, self.status, self.headers = lines, 200, {'Content-Type': 'text/event-stream'}

    def __iter__(self):
        return iter(self.lines)


REQ = {'model': 'm', 'instructions': 'sys', 'stream': True, 'temperature': 0, 'max_output_tokens': 100,
       'reasoning': {'effort': 'high'}, 'tool_choice': 'auto', 'parallel_tool_calls': True,
       'tools': [{'type': 'function', 'name': 'exec_command', 'description': 'd', 'strict': False, 'parameters': {'type': 'object'}}],
       'input': [{'type': 'message', 'role': 'developer', 'content': [{'type': 'input_text', 'text': 'dev'}]},
                 {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'q'}]},
                 {'type': 'function_call', 'call_id': 'c1', 'name': 'exec_command', 'arguments': '{"cmd":"ls"}'},
                 {'type': 'function_call', 'call_id': 'c2', 'name': 'exec_command', 'arguments': '{"cmd":"pwd"}'},
                 {'type': 'function_call_output', 'call_id': 'c1', 'output': 'a'},
                 {'type': 'function_call_output', 'call_id': 'c2', 'output': 'b'},
                 {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'ok'}]}]}


def chunk(delta=None, finish=None, usage=None):
    e = {'model': 'm', 'choices': [] if delta is None and finish is None else [{'index': 0, 'delta': delta or {}, 'finish_reason': finish}]}
    if usage:
        e['usage'] = usage
    return e


USAGE = {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15, 'prompt_tokens_details': {'cached_tokens': 4},
         'completion_tokens_details': {'reasoning_tokens': 2}}


class ChatBridgeTest(unittest.TestCase):
    def test_request(self):
        b = chat_bridge.request_to_chat(REQ)
        self.assertEqual([m['role'] for m in b['messages']], ['system', 'system', 'user', 'assistant', 'tool', 'tool', 'assistant'])
        self.assertEqual([c['id'] for c in b['messages'][3]['tool_calls']], ['c1', 'c2'])
        self.assertEqual(b['max_tokens'], 100); self.assertEqual(b['reasoning_effort'], 'high'); self.assertEqual(b['temperature'], 0)
        self.assertEqual(b['tools'][0]['function']['name'], 'exec_command')
        with self.assertRaises(ValueError):
            chat_bridge.request_to_chat({**REQ, 'input': [{'type': 'custom_tool_call'}]})

    def run_bridge(self, lines):
        return broker.read_wire(chat_bridge.ResponsesFromChat(Upstream(lines), REQ), threading.Event())

    def test_stream(self):
        lines = sse(chunk({'role': 'assistant', 'reasoning_content': 'think '}), chunk({'reasoning_content': 'more'}),
                    chunk({'content': 'hi'}),
                    chunk({'tool_calls': [{'index': 0, 'id': 'call_x', 'function': {'name': 'exec_command', 'arguments': ''}}]}),
                    chunk({'tool_calls': [{'index': 0, 'function': {'arguments': '{"cmd":'}}]}),
                    chunk({'tool_calls': [{'index': 0, 'function': {'arguments': '"ls"}'}}]}),
                    chunk(finish='tool_calls'), chunk(usage=USAGE))
        final, wire, ct, is_sse, gen, errors = self.run_bridge(lines)
        self.assertEqual(final['status'], 'completed'); self.assertTrue(gen); self.assertEqual(errors, [])
        self.assertEqual([o['type'] for o in final['output']], ['reasoning', 'message', 'function_call'])
        self.assertEqual(final['output'][0]['summary'][0]['text'], 'think more')
        self.assertEqual(final['output'][2]['arguments'], '{"cmd":"ls"}'); self.assertEqual(final['output'][2]['call_id'], 'call_x')
        self.assertEqual(final['usage']['output_tokens'], 5); self.assertEqual(final['usage']['input_tokens_details']['cached_tokens'], 4)

    def test_length(self):
        final, *_ = self.run_bridge(sse(chunk({'content': 'x'}), chunk(finish='length'), chunk(usage=USAGE)))
        self.assertEqual(final['status'], 'incomplete'); self.assertEqual(final['incomplete_details'], {'reason': 'max_output_tokens'})

    def test_cut_stream_has_no_terminal_event(self):
        with self.assertRaisesRegex(ValueError, 'No terminal Responses event'):
            self.run_bridge(sse(chunk({'reasoning_content': 'x'}), done=False))

    def test_error(self):
        final, wire, ct, is_sse, gen, errors = self.run_bridge(sse({'error': {'message': 'boom'}}))
        self.assertEqual(final['status'], 'failed'); self.assertTrue(errors)


if __name__ == '__main__':
    unittest.main()


class ChatBridgeSizeTest(unittest.TestCase):
    def test_long_stream_stays_small(self):   # one event per token once made a 20-minute stream exceed the 32 MB transport cap
        lines = sse(*[chunk({'reasoning_content': 'tok '}) for _ in range(150000)], chunk({'content': 'answer'}), chunk(finish='stop'), chunk(usage=USAGE))
        final, wire, *_ = broker.read_wire(chat_bridge.ResponsesFromChat(Upstream(lines), REQ), threading.Event())
        self.assertEqual(final['status'], 'completed')
        self.assertEqual(len(final['output'][0]['summary'][0]['text']), 600000)
        self.assertLess(len(wire), 4 * 600000 + 1_000_000)   # the text appears in the deltas, the done events and the completed response
