import unittest
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch
import test_regressions as fixtures
import app


class LocationTests(unittest.TestCase):
    setUp = fixtures.RegressionTests.setUp
    tearDown = fixtures.RegressionTests.tearDown
    response = fixtures.RegressionTests.response
    channel = fixtures.RegressionTests.channel
    # Reuse the isolated database fixture, not production or remote devices.
    def test_location_crud_and_assignment(self):
        handler = SimpleNamespace(path='/api/locations')
        def request(data):
            with patch.object(app, 'read_json', return_value=data), patch.object(app, 'json_response', side_effect=lambda h,s,d:(s,d)):
                return app.App.save_location(handler)
        self.assertEqual(request({'city':'Cidade A','block':'Bloco A'})[0], 200)
        self.assertEqual(request({'city':'Cidade A','block':'Bloco A'})[0], 409)
        self.assertEqual(request({'city':'','block':'Bloco A'})[0], 400)
        with app.connect() as conn:
            location = conn.execute('select id from locations').fetchone()[0]
            conn.execute('update nvrs set location_id=?', (location,))
        data = self.response(app.App.get_nvrs)
        self.assertEqual(data['items'][0]['city'], 'Cidade A')
        self.assertEqual(data['items'][0]['block'], 'Bloco A')
        self.assertEqual(request({'id':location,'city':'Cidade Renomeada','block':'Bloco A'})[0], 200)
        with app.connect() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute('delete from locations where id=?', (location,))
        self.assertEqual(self.response(app.App.get_nvrs)['items'][0]['city'], 'Cidade Renomeada')

    def test_location_upgrade_preserves_existing_nvr(self):
        with app.connect() as conn:
            conn.execute('alter table nvrs drop column location_id')
            conn.execute('drop table locations')
            conn.execute('pragma user_version=1')
        app.init_db()
        data = self.response(app.App.get_nvrs)['items'][0]
        self.assertEqual(data['name'], 'TEST')
        self.assertIsNone(data['location_id'])
        with app.connect() as conn:
            self.assertEqual(conn.execute('pragma user_version').fetchone()[0], app.SCHEMA_VERSION)
            self.assertEqual(conn.execute('pragma integrity_check').fetchone()[0], 'ok')

    def test_edit_nvr_saves_and_clears_location(self):
        with app.connect() as conn:
            location = conn.execute("insert into locations(city,block) values('CX','44')").lastrowid
        data = {'name':'TEST','ip':'192.0.2.1','username':'admin','location_id':str(location)}
        handler = SimpleNamespace(path=f'/api/nvrs/{self.nvr["id"]}/update')
        with patch.object(app, 'read_json', return_value=data), patch.object(app, 'json_response', side_effect=lambda h,s,d:(s,d)):
            self.assertEqual(app.App.update_nvr(handler)[0], 200)
        self.assertEqual(self.response(app.App.get_nvrs)['items'][0]['location_id'], location)
        self.assertEqual(app.nvr_password(app.active_nvrs()[0]), 'test-password')
        data['location_id'] = ''
        with patch.object(app, 'read_json', return_value=data), patch.object(app, 'json_response', side_effect=lambda h,s,d:(s,d)):
            app.App.update_nvr(handler)
        self.assertIsNone(self.response(app.App.get_nvrs)['items'][0]['location_id'])

    def test_camera_tree_only_includes_active_channels(self):
        self.channel(number=1, status='offline')
        self.channel(number=2, status='ok')
        with app.connect() as conn:
            conn.execute('update channels set enabled=0 where channel=2')
        data = self.response(app.App.get_channel_tree, '/api/channels/tree')
        self.assertEqual(len(data['items']), 1)
        self.assertEqual(data['items'][0]['camera_ip'], '192.0.2.101')
        self.assertNotIn('password', data['items'][0])
