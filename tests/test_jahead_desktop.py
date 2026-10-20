import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from adapters import parse_jahead
from orchestrate import prepare, observe, save, export_report, preflight
from dispatch import ConfigurationError

class JaheadDesktopTests(unittest.TestCase):
    def test_cumulative_snapshots_not_double_counted_and_exported(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);task=base/'task.json'
            save(task,{'prompt':'same task','criteria':[{'id':'answer','description':'OK'}],'providers':{'jahead':{'driver':'jahead_desktop'}}})
            root,exp=prepare(task,base/'exp');dest=root/'runs/jahead'
            receipt={'type':'submission.receipt','transport':'jahead_desktop','session_id':'rs_x','created_at':'2026-10-19T00:00:00Z','submitted':True}
            snap={'type':'execution.completed','session_id':'rs_x','status':'completed','observed_at':'2026-10-19T00:00:03Z','messages':[{'role':'user','content':exp['published_prompt']}],'events':[{'type':'tool_exec_started','tool_name':'search'},{'type':'tool_exec_completed','tool_name':'search'}],'final_answer':'OK','billed_token_count':100}
            (dest/'events.jsonl').write_text('\n'.join(json.dumps(o) for o in [receipt,snap,{**snap,'observed_at':'2026-10-19T00:00:10Z'}])+'\n')
            state=json.loads((dest/'state.json').read_text());state.update(status='completed',session_id='rs_x');save(dest/'state.json',state)
            run=parse_jahead(dest/'events.jsonl')[0]
            self.assertEqual(run['tool_call_count'],1);self.assertEqual(run['tool_results_count'],1)
            self.assertIsNone(run['tokens']['total_tokens']['value']);self.assertEqual(run['billed_token_count']['value'],100)
            self.assertEqual(run['duration']['value'],3000)
            observed=observe(root,exp,'jahead');self.assertEqual(observed['tracking'],'live');self.assertEqual(observed['final_answer'],'OK')
            exported=export_report(root,exp,base/'frozen');self.assertEqual(exported['missing_platforms'],[])
    def test_local_inputs_block_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);(base/'input.txt').write_text('private');task=base/'task.json'
            save(task,{'prompt':'read input','criteria':[{'id':'a','description':'read'}],'input_files':[{'source':'input.txt','name':'input.txt'}],'providers':{'jahead':{'driver':'jahead_desktop'}}})
            root,exp=prepare(task,base/'exp')
            with self.assertRaises(ConfigurationError):preflight(root,exp,'jahead')

class JaheadDispatchTests(unittest.TestCase):
    def test_selected_module_and_receipt_identity_reach_native_commands(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from dispatch import desktop_command, cancel_desktop_task
        config={'driver':'jahead_desktop','module':'developer','stage':'coding','project':'project1'}
        with patch('dispatch.resolve_program',return_value=['node']), patch('dispatch.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='{"ok":true,"profile":"developer-1.1.6"}\n')) as run:
            argv=desktop_command(config,Path('/tmp/work'),Path('/tmp/task.txt'),Path('/tmp/receipt.json'))
            probe=run.call_args.args[0]
            self.assertEqual(probe[probe.index('--module')+1],'developer')
            self.assertEqual(argv[argv.index('--stage')+1],'coding')
            self.assertEqual(argv[argv.index('--project')+1],'project1')
        with patch('dispatch.resolve_program',return_value=['node']), patch('dispatch.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='{"type":"cancel.requested"}\n')) as run:
            self.assertTrue(cancel_desktop_task(config,'conversation1',Path('/tmp/receipt.json')))
            argv=run.call_args.args[0]
            self.assertIn('--receipt-file',argv)
            self.assertEqual(argv[argv.index('--module')+1],'developer')

class PromptIdentityTests(unittest.TestCase):
    def test_runtime_prefix_does_not_replace_frozen_benchmark_prompt(self):
        from common import new_run, finish
        run=new_run('codex',Path('/tmp/source.jsonl'),'hash',{},'session')
        task='[评测任务编号: arc-abcd1234]\n\nactual task'
        run['user_messages']=[{'ref':'L1','text':'<recommended_plugins>runtime context</recommended_plugins>'},{'ref':'L2','text':task}]
        finish(run)
        self.assertEqual(run['prompt'],task)
        self.assertEqual(len(run['user_messages']),2)
