import unittest

from gate import decide

JIRA = 'https://jira.example.com/browse/PTR-1'
MR1 = 'https://gitlab.example.com/g/p/-/merge_requests/2'
MR2 = 'https://gitlab.example.com/g/p/-/merge_requests/3'

def prompt(text):
    return {'hook_event_name': 'UserPromptSubmit', 'prompt': text}

def post(tool, args, result):
    return {'hook_event_name': 'PostToolUse', 'tool_name': 'mcp__qa_orchestrator__'+tool, 'tool_input': args, 'tool_response': result}

def start(mr, run):
    context = {'input_manifest': {'sources': [{'kind': 'merge_request', 'locator': mr}, {'kind': 'task', 'locator': JIRA}]}}
    return post('start_qa_orchestration', {'task_type': 'ordinary_review', 'review_context': context}, {'isError': False, 'structuredContent': {'run_id': run}})

def finish(run, status='completed', error=False):
    return post('finish_qa_orchestration', {'run_id': run}, {'isError': error, 'structuredContent': {'status': status}})

class GateTests(unittest.TestCase):
    def test_requires_matching_session_for_each_mr(self):
        state = {}
        decide(state, prompt(f'{JIRA} {MR1} {MR2}'))
        decide(state, start(MR1, 'qar-1'))
        decide(state, finish('qar-1'))
        self.assertEqual(decide(state, {'hook_event_name': 'Stop'})['decision'], 'block')
        decide(state, start(MR2, 'qar-2'))
        decide(state, finish('qar-2'))
        self.assertEqual(decide(state, {'hook_event_name': 'Stop'}), {})

    def test_unrelated_session_and_failed_finish_do_not_pass(self):
        state = {}
        decide(state, prompt(f'{JIRA} {MR1}'))
        decide(state, start(MR2, 'qar-2'))
        decide(state, finish('qar-2'))
        decide(state, start(MR1, 'qar-1'))
        decide(state, finish('qar-1', error=True))
        self.assertEqual(decide(state, {'hook_event_name': 'Stop'})['decision'], 'block')

    def test_skips_normal_messages_and_resets_for_new_prompt(self):
        state = {}
        decide(state, prompt(f'{JIRA} {MR1}'))
        decide(state, prompt('Остановись'))
        self.assertEqual(decide(state, {'hook_event_name': 'Stop'}), {})

    def test_continuation_preserves_pending_review(self):
        state = {}
        decide(state, prompt(f'{JIRA} {MR1}'))
        result = decide(state, {'hook_event_name': 'Stop'})
        decide(state, prompt(result['reason']))
        self.assertEqual(decide(state, {'hook_event_name': 'Stop'})['decision'], 'block')

    def test_start_error_cannot_be_mistaken_for_success(self):
        state = {}
        decide(state, prompt(f'{JIRA} {MR1}'))
        decide(state, post('start_qa_orchestration', {}, {'isError': True, 'structuredContent': {'run_id': 'qar-1'}}))
        self.assertEqual(decide(state, {'hook_event_name': 'Stop'})['decision'], 'block')

    def test_new_message_resets_scope(self):
        state = {}
        decide(state, prompt(f'{JIRA} {MR1}'))
        decide(state, prompt('Продолжай, пожалуйста'))
        self.assertEqual(decide(state, {'hook_event_name': 'Stop'}), {})

    def test_task_and_mr_separate_messages_do_not_trigger(self):
        state = {}
        decide(state, prompt(JIRA))
        decide(state, prompt(MR1))
        self.assertEqual(decide(state, {'hook_event_name': 'Stop'}), {})

    def test_wrong_task_does_not_pass(self):
        state = {}
        decide(state, prompt(f'{JIRA} {MR1}'))
        event = start(MR1, 'qar-1')
        event['tool_input']['review_context']['input_manifest']['sources'][1]['locator'] = 'https://jira.example.com/browse/PTR-2'
        decide(state, event)
        decide(state, finish('qar-1'))
        self.assertEqual(decide(state, {'hook_event_name': 'Stop'})['decision'], 'block')

    def test_explicit_no_review_is_respected(self):
        state = {}
        decide(state, prompt(f'Не делай ревью {JIRA} {MR1}'))
        self.assertEqual(decide(state, {'hook_event_name': 'Stop'}), {})

    def test_reported_observed_failure_can_end_after_retry(self):
        state = {}
        decide(state, prompt(f'{JIRA} {MR1}'))
        decide(state, post('start_qa_orchestration', {}, {'isError': True}))
        first = decide(state, {'hook_event_name': 'Stop'})
        self.assertEqual(first['decision'], 'block')
        result = decide(state, {'hook_event_name': 'Stop', 'stop_hook_active': True, 'last_assistant_message': 'Не удалось запустить start_qa_orchestration: ошибка MCP.'})
        self.assertEqual(result, {})

if __name__ == '__main__':
    unittest.main()
