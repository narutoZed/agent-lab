import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from mesh_api import MeshClient, collect, submit, cancel, validate_base_url, recover_receipt
from dispatch import ConfigurationError
from mesh_protocol import mesh_usage, sanitize_event
from adapters import parse
from orchestrate import prepare, observe, save, read, preflight, roots_for
from task_console import validate_request
from compare import load_report, apply_reviews, render_report


def event(seq, kind, data, timestamp=None):
    return {'schema_version':1,'session_generation':0,'seq':seq,'time_ms':timestamp or 1000+seq*100,'type':kind,'data':data}


def raw_events():
    return [event(0,'mesh/run-started',{'run_id':'run-fixture'}),
            event(1,'request/header',{'header':{'config':{'model':'mesh-model','api_key':'CREDENTIAL'},'system':'PRIVATE_SYSTEM'}}),
            event(2,'user/message',{'message':{'content':[{'type':'text','text':'[评测任务编号: arc-aabb]\n计算17*19'}],'source':{'kind':'user'}}}),
            event(3,'assistant/message',{'turn':1,'step':1,'message':{'content':[{'type':'reasoning','text':'HIDDEN_THOUGHT'},{'type':'text','text':'Calculating'},{'type':'tool-call','name':'exec'}]},'usage':{'inputTokens':10,'cacheReadTokens':20,'cacheWriteTokens':2,'outputTokens':5,'reasoningTokens':2,'totalTokens':37}}),
            event(4,'tool/call',{'callId':'c1','name':'exec','arguments':{'secret':'TOOL_SECRET'}},1500),
            event(5,'tool/result',{'message':{'source':{'callId':'c1'},'content':[{'type':'text','text':'323'}]}},1750),
            event(6,'assistant/message',{'turn':1,'step':2,'message':{'content':[{'type':'text','text':'323 ARC_MESH_API_OK'}]},'usage':{'inputTokens':7,'cacheReadTokens':5,'cacheWriteTokens':0,'outputTokens':3,'totalTokens':15}},1800),
            event(7,'mesh/run-completed',{'run_id':'run-fixture'},1900)]


class FakeClient:
    scope={'company_id':'local-company','project_id':'local-project'}
    def __init__(self, fail_post=False):
        self.posts=0;self.fail_post=fail_post;self.received=None;self.pages=[];self.cursors=[]
    def request(self,method,path,query=None,body=None):
        if path.endswith('/permissions'):
            return {'mode':'read-only','revision':0} if method=='GET' else {'mode':body['mode'],'revision':1}
        if path=='/agent/runs':
            self.posts+=1;self.received=body
            if self.fail_post:raise OSError('connection lost after write')
            return {**{k:body[k] for k in ('company_id','project_id','session_id','request_id')},'run_id':'run-fixture','user_id':'local-user','agent_id':'default','replayed':False}
        if path.endswith('/token-metrics'):return {'token_usage':{'uncached_input_tokens':17,'output_tokens':8,'cache_read_tokens':25,'cache_write_tokens':2},'context_breakdown':{'estimated':999999}}
        if path.endswith('/cancel'):return {'accepted':True}
        raise AssertionError(path)
    def events(self,sid,after,generation):
        self.cursors.append(after)
        rows=self.pages.pop(0) if self.pages else []
        return {'events':rows,'next_after':rows[-1]['seq'] if rows else after}


class MeshTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()

    def test_only_loopback_no_credentials_or_redirect_targets(self):
        self.assertEqual(validate_base_url('http://127.0.0.1:18004/'),'http://127.0.0.1:18004')
        for url in ['http://example.com','http://127.0.0.1@evil.example','http://user:secret@localhost:18004','http://localhost:18004/?token=x','file:///tmp/x','http://localhost/api/../private']:
            with self.assertRaises(ValueError):validate_base_url(url)

    def test_mesh_uncached_input_is_normalized_without_double_counting(self):
        u=mesh_usage({'inputTokens':10,'cacheReadTokens':20,'cacheWriteTokens':2,'outputTokens':5,'totalTokens':37,'reasoningTokens':2})
        self.assertEqual(u['input_tokens'],32);self.assertEqual(u['total_tokens'],37)
        self.assertEqual(u['cached_input_tokens'],20)
        self.assertEqual(mesh_usage({'inputTokens':20,'outputTokens':3,'totalTokens':23})['input_tokens'],20)
        self.assertIsNone(mesh_usage({'inputTokens':20,'outputTokens':3})['input_tokens'])

    def test_allowlist_excludes_thoughts_context_credentials_and_tool_arguments(self):
        events=[sanitize_event(e) for e in raw_events()]
        text=json.dumps(events)
        for secret in ('HIDDEN_THOUGHT','PRIVATE_SYSTEM','CREDENTIAL','TOOL_SECRET'):
            self.assertNotIn(secret,text)
        self.assertIsNone(sanitize_event(event(10,'assistant/chunk',{'chunk':{'type':'reasoning-delta','text':'HIDDEN'}})))
        self.assertTrue(events[3]['data']['has_tool_calls'])

    def test_submission_is_one_write_and_uncertain_receipt_never_replays(self):
        config={'base_url':'http://127.0.0.1:18004','model':'mesh-model'}
        client=FakeClient();file=self.root/'receipt.json'
        receipt=submit(client,config,'hello',file)
        self.assertTrue(receipt['submitted']);self.assertEqual(client.posts,1)
        self.assertTrue(submit(client,config,'hello',file)['reused']);self.assertEqual(client.posts,1)
        with self.assertRaises(ValueError):submit(client,config,'different',file)
        failed=FakeClient(fail_post=True);file=self.root/'uncertain.json'
        with self.assertRaises(OSError):submit(failed,config,'hello',file)
        self.assertEqual(read(file)['phase'],'dispatch_unknown')
        self.assertFalse(submit(failed,config,'hello',file)['submitted']);self.assertEqual(failed.posts,1)

    def test_short_pages_are_drained_and_only_run_terminal_completes(self):
        client=FakeClient();data=raw_events();client.pages=[data[:3],data[3:6],data[6:],[]]
        receipt={'session_id':'s','run_id':'run-fixture','generation':0,'created_at':'2026-10-20T00:00:00Z'}
        rows=[];result=collect(client,receipt,sink=rows.append)
        self.assertEqual(result['status'],'completed');self.assertEqual(client.cursors,[-1,2,5,7])
        self.assertEqual(result['final_answer'],'323 ARC_MESH_API_OK')
        self.assertNotIn('context_breakdown',json.dumps(rows))
        # A visible final answer and a turn end do not replace the run-level terminal event.
        client=FakeClient();client.pages=[data[:-1]+[event(7,'turn/end',{'reason':{'kind':'completed'}})],[]]
        self.assertEqual(collect(client,receipt,sink=lambda row:None)['status'],'running')

    def test_parser_keeps_full_metrics_and_deduplicates_event_replays(self):
        header={'type':'mesh.session','session_id':'s','run_id':'run-fixture','generation':0}
        events=[{'type':'mesh.event','session_id':'s','event':sanitize_event(e)} for e in raw_events()]
        p=self.root/'mesh.jsonl';p.write_text('\n'.join(json.dumps(r) for r in [header]+events+events)+'\n')
        run=parse(p,'mesh')[0]
        self.assertEqual(run['selector'],{'run_id':'run-fixture'})
        self.assertEqual(run['tokens']['total_tokens']['value'],52)
        self.assertEqual(run['tokens']['input_tokens']['value'],44)
        self.assertEqual(run['tokens']['cached_input_tokens']['value'],25)
        self.assertEqual(run['tool_call_count'],1);self.assertEqual(run['tool_results_count'],1)
        self.assertEqual(run['tool_latency']['mean_ms'],250)
        self.assertEqual(run['duration']['value'],900)
        self.assertIsNone(run['llm_latency']['mean_ms'])
        self.assertEqual(run['final_answer'],'323 ARC_MESH_API_OK')
        self.assertEqual(run['models'],['mesh-model'])
        conflict=copy.deepcopy(events[0]);conflict['event']['time_ms']=9999
        with p.open('a') as stream:stream.write(json.dumps(conflict)+'\n')
        with self.assertRaisesRegex(ValueError,'冲突'):parse(p,'mesh')

    def test_api_cursor_validation_rejects_duplicates_or_wrong_generation(self):
        client=MeshClient()
        for rows,cursor in [([event(0,'turn/start',{}),event(0,'turn/start',{})],0),([event(0,'turn/start',{})],99),([{**event(0,'turn/start',{}),'session_generation':1}],0)]:
            with patch.object(client,'request',return_value={'events':rows,'next_after':cursor}),self.assertRaises(ValueError):client.events('s')

    def test_cancel_uses_exact_receipt_and_stable_request_id(self):
        c=FakeClient();r=submit(c,{},'hello',self.root/'r.json')
        cancel(c,r,self.root/'r.json');first=r['cancel_request_id']
        cancel(c,r,self.root/'r.json');self.assertEqual(r['cancel_request_id'],first)
        with self.assertRaises(ValueError):cancel(c,{'submitted':False},self.root/'bad.json')

    def test_uncertain_receipt_recovery_only_reads_original_session(self):
        c=FakeClient();c.pages=[[event(0,'mesh/run-started',{'run_id':'run-recovered'})],[]]
        r=recover_receipt(c,{'session_id':'owned-random','generation':0,'submitted':False},self.root/'recover.json')
        self.assertEqual(r['run_id'],'run-recovered');self.assertTrue(r['submitted']);self.assertEqual(c.posts,0)

    def test_console_accepts_mesh_and_does_not_allow_arbitrary_driver(self):
        import uuid
        p={'request_id':str(uuid.uuid4()),'title':'mesh','tasks':[{'id':'1','prompt':'hello'}],'providers':{'mesh':{'model':'mesh-model','base_url':'http://127.0.0.1:18004'}}}
        self.assertEqual(validate_request(p)['providers']['mesh']['driver'],'mesh_api')
        p['providers']['mesh']['base_url']='http://remote.example'
        with self.assertRaises(ValueError):validate_request(p)

    def test_four_platform_report_preserves_mesh_metrics(self):
        def write(name,rows):
            p=self.root/name;p.write_text(''.join(json.dumps(r)+'\n' for r in rows));return str(p)
        codex=write('codex.jsonl',[
            {'type':'event_msg','timestamp':'2026-10-20T00:00:00Z','payload':{'type':'task_started','turn_id':'t1'}},
            {'type':'token_usage_record','payload':{'turn_id':'t1','response_id':'r1','usage':{'input_tokens':10,'output_tokens':5,'total_tokens':15}}},
            {'type':'event_msg','timestamp':'2026-10-20T00:00:01Z','payload':{'type':'task_complete','turn_id':'t1','last_agent_message':'OK'}}])
        wb=write('workbuddy.jsonl',[
            {'type':'message','role':'user','id':'u1','content':'same','providerData':{'conversationRequestId':'w1'}},
            {'type':'message','role':'assistant','id':'a1','status':'completed','content':'OK','providerData':{'conversationRequestId':'w1','rawUsage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}}}])
        ja=write('jahead.jsonl',[{'id':'j1','metadata':{},'messages':[{'role':'user','content':'same'},{'role':'assistant','content':'OK'}]}])
        mesh=write('mesh.jsonl',[{'type':'mesh.session','session_id':'s','run_id':'run-fixture','generation':0}]+[{'type':'mesh.event','session_id':'s','event':sanitize_event(e)} for e in raw_events()])
        manifest=self.root/'manifest.json'
        save(manifest,{'schema_version':1,'tasks':[{'id':'four','prompt':'same','criteria':[{'id':'ok','description':'OK'}],
             'runs':[{'id':p,'platform':p,'path':f} for p,f in [('codex',codex),('workbuddy',wb),('jahead',ja),('mesh',mesh)]]}]})
        report=load_report(manifest);apply_reviews(report,[]);render_report(report,self.root/'report')
        self.assertEqual([r['platform'] for r in report['tasks'][0]['runs']],['codex','workbuddy','jahead','mesh'])
        self.assertEqual(report['tasks'][0]['runs'][3]['tokens']['total_tokens']['value'],52)
        self.assertIn("mesh:'Mesh API'",(self.root/'report/index.html').read_text())

    def test_local_attachments_are_blocked_before_mesh_submit(self):
        file=self.root/'input.txt';file.write_text('fixture')
        task=self.root/'task.json';save(task,{'prompt':'hello','criteria':[{'id':'a','description':'OK'}],'providers':{'mesh':{'driver':'mesh_api'}},'input_files':[{'source':str(file)}]})
        root,exp=prepare(task,self.root/'experiment')
        with patch('orchestrate.mesh_command',side_effect=AssertionError('must not publish')),self.assertRaisesRegex(ConfigurationError,'未上传'):
            preflight(root,exp,'mesh')
        self.assertEqual(roots_for(exp,'mesh'),[])


if __name__=='__main__':unittest.main()
