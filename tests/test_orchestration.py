import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
from adapters import parse_codex
from dispatch import ConfigurationError, DispatchUncertain, publish_workbuddy, adapter_command, resolve_program
from orchestrate import prepare, launch, observe, bind, snapshot, export_report, read, save, provider_environment, link_evaluation
from compare import apply_reviews, add_artifacts


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def setup_exp(self, providers=None, inputs=None):
        task = {"title":"test", "prompt":"只回复 OK", "criteria":[{"id":"outcome","description":"回答 OK"}],
                "providers":providers or {"codex":{"driver":"codex_cli"},"workbuddy":{"driver":"workbuddy_openapi"},"jahead":{"driver":"manual"}},
                "input_files":inputs or [], "log_roots":{"codex":[str(self.root/'logs/codex')],"workbuddy":[str(self.root/'logs/workbuddy')]}}
        p=self.root/'task.json';save(p,task)
        return prepare(p,self.root/'experiment')

    def test_same_prompt_and_isolated_input_snapshots(self):
        f=self.root/'数据.txt';f.write_text('42')
        root,exp=self.setup_exp(inputs=[{'source':'数据.txt','name':'data.txt'}])
        prompts=[(root/'runs'/p/'prompt.txt').read_bytes() for p in exp['providers']]
        self.assertEqual(len(set(prompts)),1)
        for p in exp['providers']:
            self.assertEqual((root/'runs'/p/'workspace/input/data.txt').read_text(),'42')
        with zipfile.ZipFile(root/'runs/jahead/submission.zip') as z:
            self.assertEqual(z.read('prompt.txt'),prompts[0])
            self.assertEqual(z.read('input/data.txt'),b'42')
        (root/'runs/codex/workspace/input/data.txt').write_text('changed')
        self.assertEqual((root/'runs/jahead/workspace/input/data.txt').read_text(),'42')

    def test_input_traversal_rejected_before_creating_experiment(self):
        f=self.root/'file';f.write_text('x')
        with self.assertRaises(ValueError):
            self.setup_exp(inputs=[{'source':'file','name':'../escape'}])
        self.assertFalse((self.root/'experiment').exists())

    def test_manual_launch_has_no_false_submission(self):
        root,exp=self.setup_exp()
        first=launch(root,exp,'jahead')
        self.assertEqual(first['status'],'awaiting_manual_submission')
        second=launch(root,exp,'jahead')
        self.assertIn('未重复提交',second['message'])
        self.assertEqual(observe(root,exp,'jahead')['tracking'],'manual_import_required')

    def test_workbuddy_online_check_and_exact_text(self):
        calls=[]
        def api(method,suffix,token,data=None):
            calls.append((method,suffix,data))
            return {'online':True} if method=='GET' else {'message_id':'m1'}
        with patch.dict(os.environ,{'TEST_WB_TOKEN':'dummy'}):
            receipt=publish_workbuddy('same task',{'token_env':'TEST_WB_TOKEN'},api)
        self.assertEqual(receipt['message_id'],'m1')
        self.assertEqual(calls,[('GET','',None),('POST','/message',{'content':'same task','msg_type':'text'})])
        self.assertNotIn('dummy',json.dumps(receipt))

    def test_workbuddy_offline_does_not_post(self):
        calls=[]
        def api(method,*args):
            calls.append(method);return {'online':False}
        with patch.dict(os.environ,{'TEST_WB_TOKEN':'dummy'}),self.assertRaises(ConfigurationError):
            publish_workbuddy('task',{'token_env':'TEST_WB_TOKEN'},api)
        self.assertEqual(calls,['GET'])

    def test_uncertain_write_is_not_retried(self):
        calls=[]
        def api(method,*args):
            calls.append(method)
            if method=='POST':raise DispatchUncertain('timeout')
            return {'online':True}
        with patch.dict(os.environ,{'TEST_WB_TOKEN':'dummy'}),self.assertRaises(DispatchUncertain):
            publish_workbuddy('task',{'token_env':'TEST_WB_TOKEN'},api)
        self.assertEqual(calls,['GET','POST'])

    def test_command_uses_argv_not_shell(self):
        command=adapter_command({'argv':[sys.executable,'driver.py','{workspace}']},{'workspace':'C:/任务 & 文件/目录'})
        self.assertEqual(command[-1],'C:/任务 & 文件/目录')
        p=self.root/'bad.cmd';p.write_text('not executed')
        with self.assertRaises(ConfigurationError):resolve_program(str(p))

    def test_tail_partial_line_and_bad_interior(self):
        p=self.root/'live.jsonl';p.write_bytes(b'{"x":1}\n{"x":')
        info=snapshot(p,self.root/'snapshot.jsonl')
        self.assertTrue(info['pending_last_line']);self.assertEqual(info['complete_lines'],1)
        p.write_bytes(b'{"x":1}\n{"x":2}\n')
        self.assertEqual(snapshot(p,self.root/'snapshot.jsonl')['complete_lines'],2)
        p.write_bytes(b'bad\n{"x":2}\n')
        with self.assertRaises(ValueError):snapshot(p,self.root/'snapshot.jsonl')

    def test_fake_codex_worker_real_process_and_no_duplicate_launch(self):
        root,exp=self.setup_exp({'codex':{'driver':'codex_cli','timeout_seconds':10}})
        stub=self.root/'fake_codex.py'
        stub.write_text('''import json,sys,pathlib
prompt=sys.stdin.read()
p=pathlib.Path('invocations.txt');p.write_text(p.read_text()+'1' if p.exists() else '1')
for x in [dict(type='thread.started',thread_id='fake-session'),dict(type='turn.started'),dict(type='item.completed',item={'type':'agent_message','id':'a','text':'OK'}),dict(type='turn.completed',usage={'input_tokens':100,'output_tokens':2})]: print(json.dumps(x),flush=True)
''')
        with patch('orchestrate.preflight',return_value=[sys.executable,str(stub)]):
            state=launch(root,exp,'codex',background=False)
            again=launch(root,exp,'codex',background=False)
        self.assertEqual(state['status'],'completed')
        self.assertEqual((root/'runs/codex/workspace/invocations.txt').read_text(),'1')
        result=observe(root,exp,'codex')
        self.assertEqual(result['final_answer'],'OK')
        self.assertEqual(result['metrics']['total_tokens']['value'],102)
        self.assertEqual(result['metrics']['total_tokens']['method'],'reported_turn_usage')
        self.assertIsNone(result['metrics']['llm_latency']['mean_ms'])
        report=export_report(root,exp,self.root/'frozen')
        self.assertTrue(Path(report['report']).exists())
        data=read(self.root/'frozen/report/results.json');run=data['tasks'][0]['runs'][0]
        artifact=self.root/'artifact.md';artifact.write_text('OK')
        add_artifacts(run,[{'path':str(artifact)}],self.root)
        review={'run_id':run['id'],'fingerprint':run['fingerprint'],'rubric_hash':run['rubric_hash'],'kind':'llm','reviewer':'fixture',
                'created_at':'2026-10-19T00:00:00Z','evaluations':[{'criterion_id':'outcome','verdict':'pass','reason':'exact OK','evidence':[run['final_ref']]}]}
        apply_reviews(data,[review]);save(self.root/'reviewed.json',data)
        link_evaluation(root,exp,self.root/'reviewed.json')
        self.assertEqual(observe(root,exp,'codex')['evaluation_status'],'pass')
        artifact.write_text('changed')
        self.assertEqual(observe(root,exp,'codex')['evaluation_status'],'stale')
        artifact.write_text('OK')
        with (root/'runs/codex/events.jsonl').open('a') as f:f.write('{"type":"later_event"}\n')
        self.assertEqual(observe(root,exp,'codex')['evaluation_status'],'stale')

    def test_jahead_explicit_import_and_cache(self):
        root,exp=self.setup_exp({'jahead':{'driver':'manual'}})
        p=self.root/'uploaded.jsonl'
        p.write_text(json.dumps({'id':'j','metadata':{},'stats':{},'messages':[{'role':'user','content':'task'},{'role':'assistant','content':'OK'}]})+'\n')
        result=bind(root,exp,'jahead',p,{'record_id':'j'})
        self.assertEqual(result['runs'][0]['status'],'imported')
        # One selector normalization pass, then unchanged source avoids re-parsing large uploads.
        observe(root,exp,'jahead')
        with patch('orchestrate.parse',side_effect=AssertionError('should reuse snapshot')):
            cached=observe(root,exp,'jahead')
        self.assertEqual(cached['final_answer'],'OK')

    def test_mutated_prompt_prevents_publication(self):
        root,exp=self.setup_exp({'jahead':{'driver':'manual'}})
        (root/'runs/jahead/prompt.txt').write_text('different task')
        with self.assertRaises(ConfigurationError):launch(root,exp,'jahead')
        self.assertFalse((root/'runs/jahead/dispatch.lock').exists())

    def test_workbuddy_credential_not_in_codex_process_environment(self):
        root,exp=self.setup_exp()
        with patch.dict(os.environ,{'WORKBUDDY_ACCESS_TOKEN':'secret-test'}):
            self.assertNotIn('WORKBUDDY_ACCESS_TOKEN',provider_environment(exp,'codex'))
            self.assertEqual(provider_environment(exp,'workbuddy')['WORKBUDDY_ACCESS_TOKEN'],'secret-test')

    def test_desktop_unknown_submission_preserves_identity_without_replay(self):
        root,exp=self.setup_exp({'workbuddy':{'driver':'workbuddy_desktop','timeout_seconds':10}})
        stub=self.root/'desktop.py'
        stub.write_text("import json,sys\nsys.stdin.read()\nprint(json.dumps({'type':'cli.error','error':'ack timeout','receipt':{'session_id':'native-session','request_id':'native-request','phase':'dispatch_unknown','submitted':False}}))\nsys.exit(2)\n")
        with patch('orchestrate.preflight',return_value=[sys.executable,str(stub)]):
            state=launch(root,exp,'workbuddy',background=False)
            again=launch(root,exp,'workbuddy',background=False)
        self.assertEqual(state['status'],'dispatch_unknown')
        self.assertEqual(state['session_id'],'native-session')
        self.assertIn('未重复提交',again['message'])


if __name__=='__main__': unittest.main()
