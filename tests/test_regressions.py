import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import app


TRACKS = '<TrackList><Track><id>101</id><SrcChannel>1</SrcChannel></Track></TrackList>'
EMPTY_SEARCH = '<CMSearchResult><numOfMatches>0</numOfMatches></CMSearchResult>'


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = app.DB_PATH, app.KEY_PATH, app.CREDENTIALS
        self.initial_password = patch.dict(os.environ, {'HIKVISION_INITIAL_ADMIN_PASSWORD': 'TestInitialPassword123'})
        self.initial_password.start()
        app.DB_PATH = Path(self.temp.name) / "test.db"
        app.KEY_PATH = Path(self.temp.name) / "credentials.key"
        app.init_db()
        app.import_nvrs_txt('TEST;192.0.2.1;admin;test-password')
        self.nvr = app.active_nvrs()[0]
        self.network = patch.object(app.socket, 'create_connection', side_effect=AssertionError('Real network forbidden'))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.initial_password.stop()
        app.DB_PATH, app.KEY_PATH, app.CREDENTIALS = self.original
        self.temp.cleanup()

    def channel(self, number=1, status='ok', nvr_id=None):
        with app.connect() as conn:
            cursor = conn.execute('''insert into channels(nvr_id,channel,track_id,camera_ip,
                channel_online,channel_state,last_status,last_status_changed_at,last_discovered_at)
                values(?,?,?,?,1,'online',?,?,?)''',
                (nvr_id or self.nvr['id'], number, str(number * 100 + 1),
                 '192.0.2.' + str(100 + number), status, '2026-01-01T12:00:00-03:00', app.now_iso()))
            return cursor.lastrowid

    def response(self, method, path='/api/nvrs'):
        with patch.object(app, 'json_response', side_effect=lambda handler, status, data: data), \
             patch.object(app, 'get_session_user', return_value={'id': 1, 'role': 'admin'}):
            return method(SimpleNamespace(path=path))

    def assert_database_writable(self):
        with app.connect() as conn:
            conn.execute('pragma busy_timeout=50')
            conn.execute("update app_settings set value=value where key='global_lookback_minutes'")

    def test_settings_read_during_write(self):
        with app.connect() as writer:
            writer.execute("update nvrs set name='uncommitted'")
            self.assertEqual(app.get_settings()['global_lookback_minutes'], 720)

    def test_connections_close(self):
        with app.connect() as conn:
            self.assertEqual(conn.execute('pragma foreign_keys').fetchone()[0], 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute('select 1')

    def test_recovery_skips_corrupt_history_preserves_credentials_and_ids(self):
        from recover_database import recover
        import hashlib
        channel_id = self.channel(status='offline')
        app.acknowledge_alert({'alert_type':'channel_status','ref_id':channel_id,
                              'status':'offline','status_changed_at':'2026-01-01T12:00:00-03:00'})
        with app.connect() as conn:
            conn.execute("update alert_acknowledgements set hidden_at='hidden'")
            conn.execute("insert into monitor_results(nvr_id,channel_id,status,checked_at) values(?,?,'ok','old')", (self.nvr['id'],channel_id))
        with app.connect() as conn:
            conn.execute('pragma wal_checkpoint(TRUNCATE)')
            root = conn.execute("select rootpage from sqlite_schema where name='monitor_results'").fetchone()[0]
            page_size = conn.execute('pragma page_size').fetchone()[0]
        with app.DB_PATH.open('r+b') as stream:
            stream.seek((root-1)*page_size)
            stream.write(b'\x00')
        before = hashlib.sha256(app.DB_PATH.read_bytes()).digest()
        destination = Path(self.temp.name)/'recovered.db'
        result = recover(app.DB_PATH,destination)
        self.assertEqual(result['status'],'validated')
        self.assertEqual(result['copied_rows']['monitor_results'],0)
        self.assertEqual(result['copied_rows']['channels'],1)
        self.assertEqual(hashlib.sha256(app.DB_PATH.read_bytes()).digest(),before)
        with sqlite3.connect(destination,factory=app.ClosingConnection) as conn:
            self.assertEqual(conn.execute('select password from nvrs').fetchone()[0],self.nvr['password'])
            self.assertEqual(conn.execute('select id from channels').fetchone()[0],channel_id)
            self.assertEqual(conn.execute('select hidden_at from alert_acknowledgements').fetchone()[0],'hidden')
            self.assertEqual(conn.execute('pragma integrity_check').fetchone()[0],'ok')
        report = Path(str(destination)+'.report.json').read_text()
        self.assertNotIn(self.nvr['password'],report)
        app.DB_PATH=destination
        app.init_db()
        self.assertEqual(app.nvr_password(app.active_nvrs()[0]),'test-password')

    def test_recovery_refuses_overwrite(self):
        from recover_database import recover
        with self.assertRaises(RuntimeError): recover(app.DB_PATH,app.DB_PATH)
        target=Path(self.temp.name)/'existing.db'
        target.write_bytes(b'keep')
        with self.assertRaises(RuntimeError): recover(app.DB_PATH,target)
        self.assertEqual(target.read_bytes(),b'keep')

    def test_recovery_refuses_orphans_without_discarding_rows(self):
        from recover_database import recover
        with sqlite3.connect(app.DB_PATH,factory=app.ClosingConnection) as conn:
            conn.execute("insert into channels(nvr_id,track_id,last_discovered_at) values(999,'101','old')")
        target=Path(self.temp.name)/'recovered.db'
        with self.assertRaises(RuntimeError): recover(app.DB_PATH,target)
        self.assertFalse(target.exists())
        self.assertEqual(json.loads(Path(str(target)+'.report.json').read_text())['status'],'failed')

    def test_recovery_fails_on_corruption_outside_history(self):
        from recover_database import recover
        with app.connect() as conn:
            conn.execute('pragma wal_checkpoint(TRUNCATE)')
            root=conn.execute("select rootpage from sqlite_schema where name='nvrs'").fetchone()[0]
            page_size=conn.execute('pragma page_size').fetchone()[0]
        with app.DB_PATH.open('r+b') as stream:
            stream.seek((root-1)*page_size)
            stream.write(b'\x00')
        target=Path(self.temp.name)/'recovered.db'
        with self.assertRaises(sqlite3.DatabaseError): recover(app.DB_PATH,target)
        self.assertFalse(target.exists())

    def test_integrity_guard_before_migration(self):
        from check_database import check_integrity
        check_integrity(app.DB_PATH)
        damaged = Path(self.temp.name) / 'damaged.db'
        damaged.write_bytes(b'not a sqlite database' * 200)
        app.DB_PATH = damaged
        before = damaged.read_bytes()
        with patch.object(app, 'CredentialStore') as credentials:
            with self.assertRaises(RuntimeError): app.init_db()
            credentials.assert_not_called()
        self.assertEqual(damaged.read_bytes(), before)
        self.assertFalse((damaged.parent / 'backups').exists())

    def test_credentials_encrypted_and_missing_key_fails(self):
        self.assertNotEqual(self.nvr['password'], 'test-password')
        self.assertEqual(app.nvr_password(self.nvr), 'test-password')
        app.init_db()
        key = app.KEY_PATH.read_bytes()
        app.KEY_PATH.unlink()
        with self.assertRaises(RuntimeError):
            app.init_db()
        app.KEY_PATH.write_bytes(key)
        app.init_db()
        self.assertEqual(app.nvr_password(app.active_nvrs()[0]), 'test-password')

    def test_legacy_migration_backup_and_orphans(self):
        with app.connect() as conn:
            conn.execute("update nvrs set password='legacy-password',password_encrypted=0")
            conn.execute('pragma user_version=0')
        with sqlite3.connect(app.DB_PATH, factory=app.ClosingConnection) as conn:
            conn.execute("insert into channels(nvr_id,track_id,last_discovered_at) values(999,'101','old')")
        app.init_db()
        self.assertEqual(app.nvr_password(app.active_nvrs()[0]), 'legacy-password')
        self.assertTrue(list((app.DB_PATH.parent / 'backups').glob(f'before-v{app.SCHEMA_VERSION}-*.db')))
        with app.connect() as conn:
            self.assertEqual(conn.execute('select count(*) from channels').fetchone()[0], 0)
            self.assertFalse(conn.execute('pragma foreign_key_check').fetchall())

    def test_delete_nvr_cascades_acknowledgements(self):
        channel_id = self.channel(status='offline')
        app.acknowledge_alert({'alert_type':'channel_status','ref_id':channel_id,
                               'status':'offline','status_changed_at':'2026-01-01T12:00:00-03:00'})
        with app.connect() as conn:
            conn.execute('delete from nvrs where id=?', (self.nvr['id'],))
        with app.connect() as conn:
            self.assertEqual(conn.execute('select count(*) from channels').fetchone()[0], 0)
            self.assertEqual(conn.execute('select count(*) from alert_acknowledgements').fetchone()[0], 0)

    def test_import_inherits_global_preserves_override(self):
        self.assertEqual(self.nvr['lookback_minutes'], 0)
        with app.connect() as conn:
            conn.execute('update nvrs set lookback_minutes=180')
        app.import_nvrs_txt('Renamed;192.0.2.1;admin;changed-password')
        self.assertEqual(app.active_nvrs()[0]['lookback_minutes'], 180)

    def test_recording_window_global_and_override(self):
        channel_id = self.channel()
        with app.connect() as conn:
            channel = dict(conn.execute('select * from channels where id=?',(channel_id,)).fetchone())
        for individual, expected in ((0,720),(180,180)):
            nvr = dict(self.nvr, lookback_minutes=individual)
            with patch.object(app,'hikvision_request',return_value=(200,EMPTY_SEARCH)) as request:
                app.check_channel_recording(nvr,channel,720)
                root = ET.fromstring(request.call_args.kwargs['body'])
                start = datetime.fromisoformat(app.xml_text(root,'startTime').replace('Z','+00:00'))
                end = datetime.fromisoformat(app.xml_text(root,'endTime').replace('Z','+00:00'))
                self.assertEqual((end-start).total_seconds()/60, expected)

    def test_discovery_network_without_write_lock_and_partial_metadata(self):
        self.channel()
        def request(*args, **kwargs):
            self.assert_database_writable()
            return 200, TRACKS
        def statuses(*args):
            self.assert_database_writable()
            return {1:{'online':True,'status':'online'}}
        with patch.object(app,'ensure_nvr_reachable'), patch.object(app,'hikvision_request',side_effect=request), patch.object(app,'fetch_channel_statuses',side_effect=statuses):
            app.discover_nvr_channels(self.nvr)
        with app.connect() as conn:
            self.assertEqual(conn.execute('select camera_ip from channels').fetchone()[0], '192.0.2.101')
            self.assertEqual(conn.execute('select count(*) from camera_ip_changes').fetchone()[0], 0)

    def test_invalid_discovery_preserves_channels(self):
        self.channel()
        with patch.object(app,'ensure_nvr_reachable'), patch.object(app,'hikvision_request',return_value=(200,'<ResponseStatus/>')):
            result = app.discover_all_channels_job()
        self.assertEqual(result['summary']['error'], 1)
        with app.connect() as conn:
            self.assertEqual(conn.execute('select enabled from channels').fetchone()[0], 1)

    def test_removed_channel_hidden_but_change_remains(self):
        self.channel()
        with app.connect() as conn:
            app.save_discovered_channels(conn,self.nvr,[],{},'test')
        self.assertEqual(self.response(app.App.get_channels,f'/api/nvrs/{self.nvr["id"]}/channels')['items'], [])
        self.assertEqual(self.response(app.App.get_nvrs)['items'][0]['channel_count'], 0)
        alerts = self.response(app.App.get_alerts)['items']
        self.assertEqual(len(alerts),1)
        self.assertEqual(alerts[0]['change_type'],'ip_removed')

    def test_discarded_alert_counts_and_new_occurrence(self):
        channel_id = self.channel(status='no_recording')
        self.assertEqual(self.response(app.App.get_dashboard)['summary']['alerts'],1)
        app.acknowledge_alert({'alert_type':'channel_status','ref_id':channel_id,
                               'status':'no_recording','status_changed_at':'2026-01-01T12:00:00-03:00'})
        with app.connect() as conn:
            conn.execute('update alert_acknowledgements set hidden_at=?',(app.now_iso(),))
        self.assertEqual(self.response(app.App.get_alerts)['items'],[])
        self.assertEqual(self.response(app.App.get_dashboard)['summary']['alerts'],0)
        item = self.response(app.App.get_nvrs)['items'][0]
        self.assertEqual(item['no_recording_channels'],1)
        self.assertEqual(item['pending_no_recording'],0)
        with app.connect() as conn:
            conn.execute("update channels set last_status_changed_at='2026-01-02T12:00:00-03:00'")
        self.assertEqual(len(self.response(app.App.get_alerts)['items']),1)

    def test_offline_transitions_remain_history_not_active_alerts(self):
        offline_id = self.channel(status='offline')
        self.channel(number=2,status='no_recording')
        with app.connect() as conn:
            for old_status, new_status in [('ok','offline'),('offline','ok')]:
                app.register_status_change(conn,nvr_id=self.nvr['id'],channel_id=offline_id,
                    channel=1,track_id='101',old_status=old_status,new_status=new_status,
                    old_message='',new_message='',source='test')
            self.assertEqual(conn.execute('select count(*) from channel_status_changes').fetchone()[0],2)
        alerts = self.response(app.App.get_alerts)['items']
        self.assertEqual([(item['alert_type'],item['last_status']) for item in alerts],
                         [('channel_status','no_recording')])
        summary = self.response(app.App.get_dashboard)['summary']
        self.assertEqual((summary['alerts'],summary['offline_channels']), (1,1))
        nvr = self.response(app.App.get_nvrs)['items'][0]
        self.assertEqual((nvr['pending_alerts'],nvr['pending_offline'],nvr['status_change_alerts']), (1,0,0))

    def test_network_failure_stops_nvr_not_other_nvrs(self):
        for number in range(1,17): self.channel(number)
        app.import_nvrs_txt('OTHER;192.0.2.2;admin;test-password')
        other = next(nvr for nvr in app.active_nvrs() if nvr['id'] != self.nvr['id'])
        self.channel(nvr_id=other['id'])
        statuses = {number:{'online':True,'status':'online'} for number in range(1,17)}
        def request(nvr,*args,**kwargs):
            self.assert_database_writable()
            if nvr['id']==self.nvr['id']: raise app.NvrUnavailable('timeout')
            return 200,EMPTY_SEARCH
        with patch.object(app,'ensure_nvr_reachable'), patch.object(app,'fetch_channel_statuses',return_value=statuses), patch.object(app,'hikvision_request',side_effect=request) as network:
            result=app.monitor_channels_job()
        self.assertEqual(network.call_count,2)
        self.assertEqual(result['summary']['error'],16)
        self.assertEqual(result['summary']['no_recording'],1)

    def test_failed_discovery_blocks_following_phases(self):
        self.channel()
        blocked={}
        with patch.object(app,'ensure_nvr_reachable',side_effect=app.NvrUnavailable('timeout')) as reachable:
            app.discover_all_channels_job(blocked=blocked)
            app.monitor_channels_job(blocked=blocked)
            app.update_all_hdds_job(blocked=blocked)
        self.assertEqual(reachable.call_count,1)

    def test_hdd_network_without_write_lock(self):
        def fetch(*args):
            self.assert_database_writable()
            return [{'hdd_id':'1','status':'ok','name':'Disk','capacity':'1000',
                     'free_space':'500','property':'RW','type':'SATA'}], '/test'
        with patch.object(app,'ensure_nvr_reachable'), patch.object(app,'fetch_hdds',side_effect=fetch):
            result=app.update_all_hdds_job()
        self.assertEqual(result['summary']['error'],0)

    def test_hdd_failure_alert_acknowledgement_and_recovery(self):
        disk = {'hdd_id':'1','status':'normal','name':'Disk 1','capacity':'1000',
                'free_space':'500','property':'RW','type':'SATA'}
        with app.connect() as conn:
            app.save_nvr_hdds(conn,self.nvr,[disk],'/test')
        self.assertEqual(self.response(app.App.get_alerts)['items'],[])

        disk['status'] = 'error'
        with app.connect() as conn:
            app.save_nvr_hdds(conn,self.nvr,[disk],'/test')
        alerts = self.response(app.App.get_alerts)['items']
        self.assertEqual(len(alerts),1)
        alert = alerts[0]
        self.assertEqual((alert['alert_type'],alert['last_status'],alert['channel']),
                         ('hdd_status','hdd_error','HDD 1'))
        self.assertEqual(self.response(app.App.get_dashboard)['summary']['hdd_errors'],1)
        self.assertEqual(self.response(app.App.get_nvrs)['items'][0]['pending_hdd_error'],1)
        app.acknowledge_alert({'alert_type':'hdd_status','ref_id':alert['ref_id'],
                               'status':alert['alert_status'],
                               'status_changed_at':alert['last_status_changed_at']})
        self.assertEqual(self.response(app.App.get_alerts)['items'],[])
        self.assertEqual(self.response(app.App.get_nvrs)['items'][0]['pending_alerts'],0)
        acknowledged = self.response(app.App.get_acknowledged_alerts)['items'][0]
        self.assertEqual((acknowledged['nvr_name'],acknowledged['channel'],acknowledged['hdd_status']),
                         ('TEST','HDD 1','error'))
        self.assertEqual(acknowledged['status'],'error')
        with app.connect() as conn:
            app.save_nvr_hdds(conn,self.nvr,[disk],'/test')
        self.assertEqual(self.response(app.App.get_alerts)['items'],[])

        disk['status'] = 'normal'
        with app.connect() as conn:
            app.save_nvr_hdds(conn,self.nvr,[disk],'/test')
        self.assertEqual(self.response(app.App.get_alerts)['items'],[])
        disk['status'] = 'error'
        with app.connect() as conn:
            app.save_nvr_hdds(conn,self.nvr,[disk],'/test')
        self.assertEqual(len(self.response(app.App.get_alerts)['items']),1)

    def test_idle_hdd_does_not_alert(self):
        disk = {'hdd_id':'1','status':' Idle ','name':'Disk 1','capacity':'1000',
                'free_space':'500','property':'RW','type':'SATA'}
        with app.connect() as conn:
            app.save_nvr_hdds(conn,self.nvr,[disk],'/test')
        self.assertTrue(app.hdd_is_ok(disk['status']))
        self.assertEqual(self.response(app.App.get_alerts)['items'],[])
        self.assertEqual(self.response(app.App.get_dashboard)['summary']['hdd_errors'],0)
        self.assertEqual(self.response(app.App.get_nvrs)['items'][0]['pending_hdd_error'],0)

    def test_notexist_hdd_does_not_alert(self):
        disk = {'hdd_id':'2','status':' notExist ','name':'Empty bay','capacity':None,
                'free_space':None,'property':None,'type':None}
        with app.connect() as conn:
            app.save_nvr_hdds(conn,self.nvr,[disk],'/test')
        self.assertTrue(app.hdd_is_ok(disk['status']))
        self.assertEqual(self.response(app.App.get_alerts)['items'],[])
        self.assertEqual(self.response(app.App.get_nvrs)['items'][0]['pending_alerts'],0)

    def test_hdd_disappearance_and_failed_query(self):
        disk = {'hdd_id':'2','status':'ok','name':'Disk 2','capacity':'1000',
                'free_space':'500','property':'RW','type':'SATA'}
        with app.connect() as conn:
            app.save_nvr_hdds(conn,self.nvr,[disk],'/test')
        with patch.object(app,'ensure_nvr_reachable'), patch.object(app,'fetch_hdds',side_effect=RuntimeError('timeout')):
            result = app.update_all_hdds_job()
        self.assertEqual(result['summary']['error'],1)
        self.assertEqual(self.response(app.App.get_alerts)['items'],[])
        with app.connect() as conn:
            app.save_nvr_hdds(conn,self.nvr,[], '/test')
        self.assertIn('nao encontrado',self.response(app.App.get_alerts)['items'][0]['last_message'])
        with app.connect() as conn:
            app.save_nvr_hdds(conn,self.nvr,[disk],'/test')
        self.assertEqual(self.response(app.App.get_alerts)['items'],[])

    def test_empty_hdd_list_is_valid_but_error_response_is_not(self):
        with patch.object(app,'hikvision_request',return_value=(200,'<hddList/>')):
            self.assertEqual(app.fetch_hdds(self.nvr),([], '/ISAPI/ContentMgmt/Storage/hdd'))
        with patch.object(app,'hikvision_request',return_value=(200,'<ResponseStatus/>')):
            with self.assertRaises(RuntimeError):
                app.fetch_hdds(self.nvr)

    def test_hdd_schema_upgrade_preserves_disk(self):
        with app.connect() as conn:
            conn.execute("insert into hdds(nvr_id,hdd_id,status,last_checked_at) values(?, '1', 'error', '2026-01-01T12:00:00-03:00')", (self.nvr['id'],))
            conn.execute('alter table hdds drop column last_status_changed_at')
            conn.execute('pragma user_version=2')
        app.init_db()
        with app.connect() as conn:
            row = conn.execute('select status,last_status_changed_at from hdds').fetchone()
            self.assertEqual((row['status'],row['last_status_changed_at']),
                             ('error','2026-01-01T12:00:00-03:00'))
        self.assertEqual(len(self.response(app.App.get_alerts)['items']),1)

    def test_scheduler_reports_failure_and_partial(self):
        for good,status in ((0,'error'),(1,'partial')):
            with patch.object(app,'discover_all_channels_job',return_value={'summary':{'error':1,'ok':good}}), patch.object(app,'monitor_channels_job',return_value={'summary':{'error':1}}), patch.object(app,'update_all_hdds_job',return_value={'summary':{'error':1}}):
                app.run_scheduled_cycle(force=True)
            with app.connect() as conn:
                self.assertEqual(conn.execute('select status from scheduler_runs order by id desc limit 1').fetchone()[0],status)
        self.assertFalse(app.JOB_LOCK.locked())

    def test_tls_missing_files_never_falls_back_to_http(self):
        settings=dict(app.get_settings(),https_enabled=True,https_cert_path='missing',https_key_path='missing')
        with patch.object(app,'ThreadingHTTPServer') as server:
            with self.assertRaises(RuntimeError): app.create_http_server(settings)
            server.assert_not_called()
        settings['https_enabled']=False
        with patch.object(app,'ThreadingHTTPServer'):
            _,protocol,_=app.create_http_server(settings)
            self.assertEqual(protocol,'http')

    def test_scheduler_recovers_after_unexpected_database_error(self):
        event = Mock()
        event.wait.side_effect = [False, False, KeyboardInterrupt]
        with patch.object(app,'SCHEDULER_WAKE',event), patch.object(app,'traceback'), patch.object(app,'run_scheduled_cycle',side_effect=[sqlite3.OperationalError('locked'),{}]) as cycle:
            with self.assertRaises(KeyboardInterrupt): app.scheduler_worker()
        self.assertEqual(cycle.call_count,2)
        self.assertEqual(event.wait.call_args_list[1].args,(30,))

    def test_tls_accept_does_not_wait_for_handshake(self):
        cert=Path(self.temp.name)/'cert'
        cert.touch()
        settings=dict(app.get_settings(),https_enabled=True,https_cert_path=str(cert),https_key_path=str(cert))
        with patch.object(app,'ThreadingHTTPServer'), patch.object(app.ssl,'SSLContext') as context:
            app.create_http_server(settings)
            self.assertIs(context.return_value.wrap_socket.call_args.kwargs['do_handshake_on_connect'],False)

    def test_tls_worker_has_timeout_before_handshake(self):
        request=Mock(spec=app.ssl.SSLSocket)
        handler=object.__new__(app.App)
        handler.request=request
        with patch.object(app.BaseHTTPRequestHandler,'setup'):
            handler.setup()
        self.assertEqual(request.method_calls[:2], [unittest.mock.call.settimeout(15), unittest.mock.call.do_handshake()])

    def test_restart_does_not_repeat_legacy_history_cleanup(self):
        statements=[]
        original=app.connect
        def traced():
            conn=original()
            conn.set_trace_callback(statements.append)
            return conn
        with patch.object(app,'connect',side_effect=traced): app.init_db()
        self.assertFalse(any('delete from monitor_results' in sql.lower() for sql in statements))
        self.assertFalse(any('update monitor_results set channel_id' in sql.lower() for sql in statements))

    def test_wrong_key_fails_without_resetting_credentials(self):
        from cryptography.fernet import Fernet
        app.KEY_PATH.write_bytes(Fernet.generate_key())
        with self.assertRaises(RuntimeError): app.init_db()
        self.assertEqual(app.active_nvrs()[0]['password'],self.nvr['password'])


if __name__ == '__main__':
    unittest.main()
