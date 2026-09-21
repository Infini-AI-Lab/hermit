import unittest
from broker import MODEL, normalized, public

class TransportTests(unittest.TestCase):
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
