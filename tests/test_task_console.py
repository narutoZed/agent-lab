import copy
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import uuid
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from task_console import ConsoleStore, make_handler, validate_request
from dispatch import ConfigurationError, desktop_command
from types import SimpleNamespace
from orchestrate import read, save
from orchestrate import launch as real_launch, observe as real_observe
from xlsx_tasks import read_xlsx
from adapters import parse_codex


def fixture_xlsx(sheet_xml=None, shared=None, second=False):
    content = sheet_xml or '''<sheetData>
    <row r="1"><c r="A1" t="inlineStr"><is><t>任务名称</t></is></c><c r="C1" t="inlineStr"><is><t>任务正文</t></is></c></row>
    <row r="3"><c r="A3" t="inlineStr"><is><t>示例任务</t></is></c><c r="C3" t="inlineStr"><is><t>只回复 OK</t></is></c></row>
    </sheetData>'''
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="任务列表" sheetId="1" r:id="rId1"/>' + ('<sheet name="隐藏页" state="hidden" sheetId="2" r:id="rId2"/>' if second else '') + '</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Target="/xl/worksheets/sheet2.xml"/></Relationships>')
        for name in (["sheet1", "sheet2"] if second else ["sheet1"]):
            z.writestr(f"xl/worksheets/{name}.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'+content+'</worksheet>')
        if shared is not None:
            z.writestr("xl/sharedStrings.xml", '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'+shared+'</sst>')
    return buffer.getvalue()


def request_payload(platforms=("codex", "workbuddy")):
    return {"request_id": str(uuid.uuid4()), "title": "选中的两条任务",
            "tasks": [{"id": "T2", "title": "任务二", "prompt": "只回复 SECOND", "criteria": "只输出 SECOND"},
                      {"id": "T4", "title": "任务四", "prompt": "只回复 FOURTH"}],
            "providers": {p: {"model": "requested-model"} for p in platforms}}


def complete_fixture(store, batch_id):
    batch = store.batches[batch_id]
    for index in range(len(batch['tasks'])):
        root = store.root / batch_id / f'experiment-{index+1}'
        exp = read(root/'experiment.json')
        dest = root/'runs/codex'
        events = [
            {'type':'agent_run_compare.prompt','prompt':exp['published_prompt'],'timestamp':'2026-10-20T00:00:00Z'},
            {'type':'thread.started','thread_id':f'fixture-{index}'},
            {'type':'turn.started','turn_id':'t1','observed_at':'2026-10-20T00:00:00Z'},
            {'type':'item.started','item':{'id':'call1','type':'command_execution'},'observed_at':'2026-10-20T00:00:01Z'},
            {'type':'item.completed','item':{'id':'call1','type':'command_execution','aggregated_output':'OK'},'observed_at':'2026-10-20T00:00:01.200Z'},
            {'type':'item.completed','item':{'id':'answer','type':'agent_message','text':'OK'},'observed_at':'2026-10-20T00:00:02Z'},
            {'type':'turn.completed','usage':{'input_tokens':100+index,'output_tokens':20,'cached_input_tokens':60},'observed_at':'2026-10-20T00:00:03Z'}]
        log = dest/'events.jsonl'
        log.write_text(''.join(json.dumps(e,ensure_ascii=False)+'\n' for e in events))
        save(dest/'observed-binding.json',{'path':str(log),'selector':{'turn_id':'t1'}})
        state=read(dest/'state.json');state.update(status='completed',session_id=f'fixture-{index}')
        save(dest/'state.json',state)
        next(j for j in batch['jobs'] if j['task_index']==index)['status']='completed'


class XlsxImportTests(unittest.TestCase):
    def test_sparse_cells_multisheet_and_original_row_number(self):
        result = read_xlsx(fixture_xlsx(second=True))
        self.assertEqual(len(result["sheets"]), 2)
        self.assertTrue(result["sheets"][1]["hidden"])
        sheet = result["sheets"][0]
        self.assertEqual(sheet["headers"], ["任务名称", "未命名列 2", "任务正文"])
        self.assertEqual(sheet["rows"][0]["cells"], ["示例任务", "", "只回复 OK"])
        self.assertEqual(sheet["rows"][0]["row_number"], 3)

    def test_shared_strings_rich_text_and_formula_never_evaluated(self):
        sheet = '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row><row r="2"><c r="A2"><f>HYPERLINK("https://example.com")</f><v>cached unsafe prompt</v></c></row></sheetData>'
        result = read_xlsx(fixture_xlsx(sheet, '<si><r><t>任务</t></r><r><t>正文</t></r></si>'))
        row = result["sheets"][0]["rows"][0]
        self.assertEqual(result["sheets"][0]["headers"], ["任务正文"])
        self.assertEqual(row["cells"], [""])
        self.assertIn("公式", row["errors"][0]["message"])

    def test_invalid_file_and_invalid_shared_index_rejected(self):
        with self.assertRaises(ValueError):
            read_xlsx(b"not excel")
        with self.assertRaises(ValueError):
            read_xlsx(fixture_xlsx('<sheetData><row><c r="A1" t="s"><v>-1</v></c></row></sheetData>', '<si><t>x</t></si>'))

    def test_dtd_and_excessive_width_rejected(self):
        with self.assertRaises(ValueError):
            read_xlsx(fixture_xlsx('<!DOCTYPE root [<!ENTITY a "bad">]><sheetData/>'))
        with self.assertRaises(ValueError):
            read_xlsx(fixture_xlsx('<sheetData><row><c r="ZZ1"><v>x</v></c></row></sheetData>'))

    def test_decompression_budget_rejected(self):
        with patch('xlsx_tasks.MAX_UNPACKED', 20), self.assertRaises(ValueError):
            read_xlsx(fixture_xlsx())


class ConsoleQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.launched = []
        self.states = {}

        def launcher(root, exp, platform):
            self.launched.append((root.name, platform, exp["prompt"], exp["providers"][platform].get("model")))
            self.states[(root.name, platform)] = {"status": "running", "last_event": "execution.status"}
            return {"status": "running"}

        def observer(root, exp, platform):
            return self.states.get((root.name, platform), {"status": "prepared"})

        self.store = ConsoleStore(self.root, launcher, observer)

    def tearDown(self):
        self.temp.cleanup()

    def test_selected_rows_only_and_model_propagation(self):
        payload = request_payload()
        result = self.store.create(payload)
        self.assertEqual(len(self.store.batches[result["id"]]["jobs"]), 4)
        self.assertEqual(self.launched, [])  # Preparing the queue itself does not execute.
        self.store.tick("codex")
        self.store.tick("workbuddy")
        self.assertEqual([r[2] for r in self.launched], ["只回复 SECOND", "只回复 SECOND"])
        self.assertEqual([r[3] for r in self.launched], ["requested-model", "requested-model"])
        self.store.tick("codex")
        self.assertEqual(len(self.launched), 2)  # The next same-platform task remains queued.
        self.states[("experiment-1", "codex")] = {"status": "completed", "final_answer": "SECOND"}
        self.store.tick("codex")
        self.store.tick("codex")
        self.assertEqual(self.launched[-1][2], "只回复 FOURTH")
        self.assertEqual(self.launched[-1][1], "codex")

    def test_duplicate_request_reused_and_mutation_rejected(self):
        payload = request_payload()
        self.store.create(payload)
        self.assertTrue(self.store.create(payload)["reused"])
        payload["tasks"][0]["prompt"] = "changed"
        with self.assertRaises(ValueError):
            self.store.create(payload)
        self.assertEqual(len(self.store.listing()["batches"]), 1)

    def test_restart_keeps_creation_order_instead_of_uuid_sort_order(self):
        first=request_payload(('codex',));first['request_id']='ffffffff-ffff-4fff-8fff-ffffffffffff'
        second=request_payload(('codex',));second['request_id']='00000000-0000-4000-8000-000000000000'
        self.store.create(first);self.store.create(second)
        recovered=ConsoleStore(self.root,self.store.launcher,self.store.observer)
        self.assertEqual(recovered.listing()['batches'][0]['id'],second['request_id'])

    def test_pause_only_blocks_queued_jobs(self):
        batch_id = self.store.create(request_payload())["id"]
        self.store.tick("codex")
        self.store.set_paused(batch_id, True)
        self.states[("experiment-1", "codex")] = {"status": "completed"}
        self.store.tick("codex")
        self.store.tick("codex")
        self.store.tick("workbuddy")
        self.assertEqual(len(self.launched), 1)
        self.store.set_paused(batch_id, False)
        self.store.tick("codex")
        self.assertEqual(len(self.launched), 2)

    def test_restart_pauses_queue_and_does_not_resubmit_active_job(self):
        payload = request_payload()
        self.store.create(payload)
        self.store.tick("codex")
        recovered = ConsoleStore(self.root, self.store.launcher, self.store.observer)
        self.assertTrue(recovered.batches[payload["request_id"]]["paused"])
        recovered.tick("codex")
        recovered.tick("workbuddy")
        self.assertEqual(len(self.launched), 1)

    def test_unknown_dispatch_blocks_platform_instead_of_replaying(self):
        self.store.create(request_payload())
        self.store.tick("codex")
        self.states[("experiment-1", "codex")] = {"status": "timed_out", "desktop_stop_requested": False}
        self.store.tick("codex")
        self.store.tick("codex")
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.store.listing()["batches"][0]["jobs"][0]["status"], "needs_confirmation")

    def test_connection_failure_holds_following_queue_and_safe_retry_is_idempotent(self):
        batch_id = self.store.create(request_payload())["id"]
        original = self.store.launcher
        self.store.launcher = lambda *args: (_ for _ in ()).throw(ConfigurationError('two windows'))
        self.store.tick('workbuddy')
        self.store.tick('workbuddy')
        jobs = [j for j in self.store.batches[batch_id]['jobs'] if j['platform']=='workbuddy']
        self.assertEqual([j['status'] for j in jobs], ['needs_configuration','queued'])
        self.assertEqual(self.store.retry_unsubmitted(batch_id)['requeued'], 1)
        self.assertEqual(self.store.retry_unsubmitted(batch_id)['requeued'], 0)
        self.store.launcher = original
        self.store.tick('workbuddy')
        self.assertEqual(len(self.launched), 1)

    def test_retry_never_removes_a_native_submission_marker(self):
        batch_id = self.store.create(request_payload())["id"]
        job = self.store.batches[batch_id]['jobs'][1]
        job['status'] = 'needs_configuration'
        lock = self.root / batch_id / 'experiment-1/runs/workbuddy/dispatch.lock'
        lock.write_text('possible write')
        result = self.store.retry_unsubmitted(batch_id)
        self.assertEqual(result['requeued'], 0)
        self.assertEqual(len(result['blocked']), 1)
        self.assertEqual(lock.read_text(), 'possible write')

    def test_desktop_preflight_preserves_actionable_native_error(self):
        result = SimpleNamespace(returncode=2,stdout=json.dumps({'ok':False,'error':'找到两个不同配置的窗口'}))
        with patch('dispatch.resolve_program',return_value=['node']), patch('dispatch.subprocess.run',return_value=result):
            with self.assertRaisesRegex(ConfigurationError,'找到两个不同配置的窗口'):
                desktop_command({'driver':'workbuddy_desktop'},self.root,self.root/'prompt.txt',self.root/'receipt.json')

    def test_requested_and_actual_models_are_distinct(self):
        batch_id = self.store.create(request_payload())["id"]
        self.store.tick("codex")
        save(self.root / batch_id / "experiment-1/runs/codex/normalized.json", {"models": ["actual-model"]})
        self.store.tick("codex")
        job = self.store.detail(batch_id, 0)["jobs"][0]
        self.assertEqual(job["snapshot"]["models"], ["actual-model"])
        self.assertIn("不同", job["snapshot"]["model_note"])

    def test_web_cannot_supply_shell_or_arbitrary_driver(self):
        for key in ("executable", "argv", "driver", "token", "workspace"):
            payload = request_payload()
            payload["providers"]["codex"][key] = "untrusted"
            with self.assertRaises(ValueError):
                self.store.create(payload)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_rows_are_rejected_before_preparation(self):
        for mutate in (lambda p: p["tasks"][0].update(prompt=""),
                       lambda p: p["tasks"][0].update(criteria="\n".join(["x"] * 51)),
                       lambda p: p["tasks"][1].update(id="T2")):
            payload = request_payload()
            mutate(payload)
            with self.assertRaises(ValueError):
                self.store.create(payload)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_queue_through_real_worker_with_local_fake_cli(self):
        stub = self.root / 'fake_cli.py'
        stub.write_text('''import json, sys
prompt = sys.stdin.read()
for item in [dict(type='thread.started', thread_id='console-fixture'),dict(type='turn.started'),
             dict(type='item.completed', item={'type':'agent_message','id':'answer','text':'LOCAL_FIXTURE_OK'}),
             dict(type='turn.completed', usage={'input_tokens':10,'output_tokens':2})]:
    print(json.dumps(item), flush=True)
''')
        def local_launcher(root, exp, platform):
            with patch('orchestrate.preflight', return_value=[sys.executable, str(stub)]):
                return real_launch(root, exp, platform, background=False)
        store = ConsoleStore(self.root / 'real-worker-fixture', local_launcher, real_observe)
        payload = request_payload(('codex',))
        payload['tasks'] = payload['tasks'][:1]
        batch_id = store.create(payload)['id']
        store.tick('codex')
        self.assertEqual(store.detail(batch_id, 0)['jobs'][0]['status'], 'completed')
        store.tick('codex')
        self.assertEqual(store.detail(batch_id, 0)['jobs'][0]['snapshot']['final_answer'], 'LOCAL_FIXTURE_OK')
        experiment = self.root / 'real-worker-fixture' / batch_id / 'experiment-1'
        state = read(experiment / 'runs/codex/state.json')
        self.assertEqual(state['last_event'], 'turn.completed')
        result = real_observe(experiment, read(experiment/'experiment.json'), 'codex')
        self.assertEqual(result['final_answer'], 'LOCAL_FIXTURE_OK')
        self.assertEqual(result['metrics']['total_tokens']['value'], 12)

    def test_comparison_reuses_original_metrics_and_caches_unchanged_evidence(self):
        batch_id = self.store.create(request_payload(('codex',)))['id']
        complete_fixture(self.store,batch_id)
        result = self.store.comparison(batch_id)
        self.assertFalse(result['reused'])
        report = read(Path(result['report_path']).with_name('results.json'))
        self.assertEqual(len(report['tasks']),2)
        self.assertEqual(self.store.detail(batch_id,0)['jobs'][0]['snapshot']['metrics']['total_tokens']['value'],120)
        for index, task in enumerate(report['tasks']):
            source=self.root/batch_id/f'experiment-{index+1}/runs/codex/events.jsonl'
            expected=parse_codex(source)[0]
            run=task['runs'][0]
            self.assertEqual(run['tokens'],expected['tokens'])
            self.assertEqual(run['tool_latency'],expected['tool_latency'])
            self.assertEqual(run['tool_call_count'],1)
            self.assertEqual(run['tool_latency']['p95_ms'],200)
            self.assertEqual(run['reviews'],[])
        html=Path(result['report_path']).read_text()
        for label in ['总 Token','输入 Token','输出 Token','缓存输入（输入子集）','工具响应 P95','LLM 响应 P95','任务首 Token 耗时']:
            self.assertIn(label,html)
        again=self.store.comparison(batch_id)
        self.assertTrue(again['reused'])
        self.assertEqual(result['url'],again['url'])
        source=self.root/batch_id/'experiment-1/runs/codex/events.jsonl'
        with source.open('a') as stream: stream.write('{"type":"later_metadata"}\n')
        refreshed=self.store.comparison(batch_id)
        self.assertNotEqual(result['url'],refreshed['url'])
        self.assertTrue(Path(result['report_path']).exists())

    def test_running_batch_cannot_be_falsely_exported_as_finished(self):
        batch_id=self.store.create(request_payload(('codex',)))['id']
        with self.assertRaisesRegex(ValueError,'执行中或排队'):
            self.store.comparison(batch_id)


class ConsoleHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ConsoleStore(self.temp.name)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.store, 'test-token'))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = f'127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temp.cleanup()

    def send(self, method, path, body=None, headers=None):
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        status = response.status
        connection.close()
        return status, data

    def auth(self):
        return {'Origin': 'http://' + self.host, 'X-Console-Token': 'test-token', 'Content-Type': 'application/json'}

    def test_cross_origin_and_missing_token_cannot_publish(self):
        payload = json.dumps(request_payload())
        for headers in ({}, {**self.auth(), 'Origin': 'https://external.example'}, {**self.auth(), 'X-Console-Token': 'wrong'}, {**self.auth(), 'Host': 'external.example'}):
            self.assertEqual(self.send('POST', '/api/batches', payload, headers)[0], 403)
        self.assertEqual(self.store.listing()['batches'], [])

    def test_import_select_publish_replay_and_read_details(self):
        status, data = self.send('POST', '/api/import', fixture_xlsx(), self.auth())
        self.assertEqual(status, 200)
        sheet = json.loads(data)['sheets'][0]
        payload = request_payload(('codex',))
        payload['tasks'] = [{'id':'chosen-row-3','title':sheet['rows'][0]['cells'][0], 'prompt':sheet['rows'][0]['cells'][2]}]
        status, data = self.send('POST', '/api/batches', json.dumps(payload), self.auth())
        self.assertEqual(status, 200)
        batch_id = json.loads(data)['id']
        status, replay = self.send('POST', '/api/batches', json.dumps(payload), self.auth())
        self.assertTrue(json.loads(replay)['reused'])
        status, detail = self.send('GET', f'/api/tasks/{batch_id}/0')
        self.assertEqual(json.loads(detail)['task']['prompt'], '只回复 OK')
        self.assertEqual(json.loads(detail)['jobs'][0]['status'], 'queued')
        self.assertEqual(self.send('GET', f'/api/tasks/{batch_id}/-1')[0], 404)

    def test_html_and_error_responses(self):
        status, html = self.send('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'test-token', html)
        self.assertNotIn(b'__CSRF_TOKEN__', html)
        self.assertEqual(self.send('POST', '/api/import', b'invalid', self.auth())[0], 400)
        self.assertEqual(self.send('POST', '/api/batches', b'null', self.auth())[0], 400)
        self.assertEqual(self.send('GET', '/', headers={'Host': 'untrusted.example'})[0], 403)

    def test_report_route_and_inline_script_policy(self):
        batch_id=self.store.create(request_payload(('codex',)))['id']
        complete_fixture(self.store,batch_id)
        code,data=self.send('POST',f'/api/batches/{batch_id}/comparison','{}',self.auth())
        self.assertEqual(code,200)
        info=json.loads(data)
        connection=HTTPConnection('127.0.0.1',self.server.server_port)
        connection.request('GET',info['url'])
        response=connection.getresponse();html=response.read()
        self.assertEqual(response.status,200)
        self.assertIn("script-src 'sha256-",response.getheader('Content-Security-Policy'))
        self.assertEqual(response.getheader('X-Frame-Options'),'SAMEORIGIN')
        self.assertIn('工具响应 P95'.encode(),html)
        connection.close()
        self.assertEqual(self.send('GET',f'/comparisons/{batch_id}/../index.html')[0],404)
        self.assertEqual(self.send('GET',info['json_url'])[0],200)


if __name__ == '__main__':
    unittest.main()
