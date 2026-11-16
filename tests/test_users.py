import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_regressions as fixtures
import app


class UserAccessTests(unittest.TestCase):
    setUp = fixtures.RegressionTests.setUp
    tearDown = fixtures.RegressionTests.tearDown
    channel = fixtures.RegressionTests.channel

    def handler(self, path, token):
        handler = object.__new__(app.App)
        handler.path = path
        handler.headers = {"Cookie": f"hikvision_session={token}"}
        return handler

    def call(self, method, path, token, payload=None):
        handler = self.handler(path, token)
        with patch.object(app, "json_response", side_effect=lambda _handler, status, data: (status, data)) as response:
            if payload is None:
                result = method(handler)
            else:
                with patch.object(app, "read_json", return_value=payload):
                    result = method(handler)
        return result if result is not None else response.call_args.args[1:]

    def admin_token(self):
        with app.connect() as conn:
            admin_id = conn.execute("select id from app_users where username='admin'").fetchone()[0]
        token = app.create_session(admin_id)
        app.change_password(self.handler("/api/change-password", token),
                            {"current_password": "TestInitialPassword123", "new_password": "AdminPass123"})
        return token

    def create_viewer(self, admin_token, username="viewer"):
        with app.connect() as conn:
            conn.execute("insert or ignore into locations(city,block) values('Cidade A','Bloco 1')")
            location_id = conn.execute("select id from locations where city='Cidade A'").fetchone()[0]
            conn.execute("update nvrs set location_id=? where id=?", (location_id, self.nvr['id']))
        result = self.call(app.App.do_POST, "/api/users", admin_token,
                           {"username": username, "password": "InitialPass123", "role": "viewer", "cities": ["Cidade A"]})
        self.assertEqual(result[0], 201)
        return result[1]["id"]

    def test_viewer_is_read_only_at_api_boundary(self):
        admin_token = self.admin_token()
        viewer_id = self.create_viewer(admin_token)
        viewer_token = app.create_session(viewer_id)
        self.assertEqual(self.call(app.App.do_GET, "/api/me", viewer_token)[1]["user"]["role"], "viewer")
        self.assertEqual(self.call(app.App.do_GET, "/api/alerts", viewer_token)[0], 403)
        app.change_password(self.handler("/api/change-password", viewer_token),
                            {"current_password": "InitialPass123", "new_password": "ChangedPass123"})
        self.assertEqual(self.call(app.App.do_GET, "/api/alerts", viewer_token)[0], 200)
        self.assertEqual(self.call(app.App.do_GET, "/api/users", viewer_token)[0], 403)
        self.assertEqual(self.call(app.App.do_GET, "/api/settings", viewer_token)[0], 403)
        self.assertEqual(self.call(app.App.do_GET, "/api/scheduler/history", viewer_token)[0], 403)
        self.assertEqual(self.call(app.App.do_GET, "/api/nvrs/1/channels/1/snapshot", viewer_token)[0], 403)
        for path in ("/api/nvrs", "/api/locations", "/api/settings", "/api/alerts/ack",
                     "/api/verify-now/run", "/api/hdds/run", "/api/scheduler/run", "/api/users"):
            with self.subTest(path=path):
                self.assertEqual(self.call(app.App.do_POST, path, viewer_token, {})[0], 403)
        for path in ("/api/nvrs/1", "/api/alerts/acknowledged", f"/api/users/{viewer_id}"):
            with self.subTest(path=path):
                self.assertEqual(self.call(app.App.do_DELETE, path, viewer_token)[0], 403)
        with app.connect() as conn:
            self.assertEqual(conn.execute("select count(*) from nvrs").fetchone()[0], 1)
            self.assertEqual(conn.execute("select count(*) from app_users where id=?", (viewer_id,)).fetchone()[0], 1)

    def test_admin_can_manage_only_viewers_and_reset_revokes_sessions(self):
        admin_token = self.admin_token()
        rejected = self.call(app.App.do_POST, "/api/users", admin_token,
                             {"username": "otheradmin", "password": "Password123", "role": "admin", "cities": ["Cidade A"]})
        self.assertEqual(rejected[0], 400)
        viewer_id = self.create_viewer(admin_token)
        self.assertEqual(self.call(app.App.do_GET, "/api/users", admin_token)[1]["items"][0]["username"], "viewer")
        self.assertEqual(self.call(app.App.do_GET, "/api/users", admin_token)[1]["items"][0]["cities"], ["Cidade A"])
        self.assertEqual(self.call(app.App.do_POST, "/api/users", admin_token,
                                   {"username": "VIEWER", "password": "Password123", "cities": ["Cidade A"]})[0], 409)
        viewer_token = app.create_session(viewer_id)
        self.assertEqual(self.call(app.App.do_POST, f"/api/users/{viewer_id}/password", admin_token,
                                   {"password": "NewPassword123"})[0], 200)
        self.assertIsNone(app.get_session_user(self.handler("/api/me", viewer_token)))
        with app.connect() as conn:
            row = conn.execute("select role, must_change_password from app_users where id=?", (viewer_id,)).fetchone()
            self.assertEqual((row["role"], row["must_change_password"]), ("viewer", 1))
        self.assertEqual(self.call(app.App.do_DELETE, f"/api/users/{viewer_id}", admin_token)[0], 200)
        self.assertEqual(self.call(app.App.do_DELETE, "/api/users/1", admin_token)[0], 404)
        self.assertEqual(self.call(app.App.do_GET, "/api/users", admin_token)[1]["items"], [])

    def test_viewer_sees_only_assigned_city_across_all_read_endpoints(self):
        admin_token = self.admin_token()
        viewer_id = self.create_viewer(admin_token)
        first_channel = self.channel(status="offline")
        app.import_nvrs_txt('OTHER;192.0.2.2;admin;test-password')
        with app.connect() as conn:
            second_nvr = conn.execute("select id from nvrs where name='OTHER'").fetchone()[0]
            conn.execute("insert into locations(city,block) values('Cidade B','Bloco B')")
            other_location = conn.execute("select id from locations where city='Cidade B'").fetchone()[0]
            conn.execute("update nvrs set location_id=? where id=?", (other_location, second_nvr))
            conn.execute("insert into hdds(nvr_id,hdd_id,status,last_checked_at) values(?,?,?,?)",
                         (self.nvr['id'], '1', 'error', app.now_iso()))
            conn.execute("insert into hdds(nvr_id,hdd_id,status,last_checked_at) values(?,?,?,?)",
                         (second_nvr, '1', 'error', app.now_iso()))
            conn.execute("insert into channels(nvr_id,channel,track_id,last_status,last_discovered_at) values(?,?,?,?,?)",
                         (second_nvr, 1, '101', 'offline', app.now_iso()))
            conn.execute("insert into camera_ip_changes(nvr_id,channel,track_id,new_ip,change_type,detected_at,source) values(?,?,?,?,?,?,?)",
                         (self.nvr['id'], 1, '101', '192.0.2.111', 'changed', app.now_iso(), 'test'))
            conn.execute("insert into camera_ip_changes(nvr_id,channel,track_id,new_ip,change_type,detected_at,source) values(?,?,?,?,?,?,?)",
                         (second_nvr, 1, '101', '192.0.2.222', 'changed', app.now_iso(), 'test'))
        app.acknowledge_alert({'alert_type': 'channel_status', 'ref_id': first_channel,
                               'status': 'offline', 'status_changed_at': '2026-01-01T12:00:00-03:00'})
        viewer_token = app.create_session(viewer_id)
        app.change_password(self.handler('/api/change-password', viewer_token),
                            {'current_password': 'InitialPass123', 'new_password': 'ChangedPass123'})
        for route in ('/api/nvrs', '/api/channels/tree', '/api/hdds', '/api/alerts',
                      '/api/alerts/acknowledged', '/api/camera-ip-changes'):
            with self.subTest(route=route):
                result = self.call(app.App.do_GET, route, viewer_token)
                self.assertEqual(result[0], 200)
                self.assertTrue(all(item['nvr_id'] == self.nvr['id'] for item in result[1]['items'] if 'nvr_id' in item))
                self.assertNotIn('OTHER', str(result[1]))
        self.assertEqual([n['id'] for n in self.call(app.App.do_GET, '/api/nvrs', viewer_token)[1]['items']], [self.nvr['id']])
        self.assertEqual(self.call(app.App.do_GET, '/api/dashboard', viewer_token)[1]['summary']['nvrs'], 1)
        self.assertEqual(self.call(app.App.do_GET, '/api/dashboard', viewer_token)[1]['summary']['channels'], 1)
        self.assertEqual([item['city'] for item in self.call(app.App.do_GET, '/api/locations', viewer_token)[1]['items']], ['Cidade A'])
        self.assertEqual(self.call(app.App.do_GET, f'/api/nvrs/{second_nvr}/channels', viewer_token)[0], 404)
        self.assertEqual(self.call(app.App.do_GET, f'/api/nvrs/{second_nvr}/hdds', viewer_token)[0], 404)
        self.assertEqual(self.call(app.App.do_POST, f'/api/users/{viewer_id}/cities', viewer_token, {'cities': ['Cidade B']})[0], 403)
        self.assertEqual(self.call(app.App.do_POST, f'/api/users/{viewer_id}/cities', admin_token, {'cities': ['Unknown']})[0], 400)
        self.assertEqual(self.call(app.App.do_POST, f'/api/users/{viewer_id}/cities', admin_token, {'cities': ['Cidade B']})[0], 200)
        self.assertEqual([n['id'] for n in self.call(app.App.do_GET, '/api/nvrs', viewer_token)[1]['items']], [second_nvr])
        self.assertEqual(self.call(app.App.do_GET, f'/api/nvrs/{self.nvr["id"]}/channels', viewer_token)[0], 404)

    def test_existing_viewer_without_city_has_no_nvr_access(self):
        admin_token = self.admin_token()
        viewer_id = self.create_viewer(admin_token)
        with app.connect() as conn:
            conn.execute('delete from app_user_cities where user_id=?', (viewer_id,))
        viewer_token = app.create_session(viewer_id)
        app.change_password(self.handler('/api/change-password', viewer_token),
                            {'current_password': 'InitialPass123', 'new_password': 'ChangedPass123'})
        self.assertEqual(self.call(app.App.do_GET, '/api/nvrs', viewer_token)[1]['items'], [])
        self.assertEqual(self.call(app.App.do_GET, '/api/dashboard', viewer_token)[1]['summary']['nvrs'], 0)
        self.assertEqual(self.call(app.App.do_GET, '/api/alerts', viewer_token)[1]['items'], [])
        self.assertEqual(self.call(app.App.do_GET, '/api/locations', viewer_token)[1]['items'], [])

    def test_recovery_copy_preserves_viewer_city_grants(self):
        from recover_database import recover
        admin_token = self.admin_token()
        viewer_id = self.create_viewer(admin_token)
        destination = Path(self.temp.name) / 'recovered.db'
        result = recover(app.DB_PATH, destination)
        self.assertEqual(result['copied_rows']['app_user_cities'], 1)
        with app.sqlite3.connect(destination, factory=app.ClosingConnection) as conn:
            self.assertEqual(conn.execute('select city from app_user_cities where user_id=?', (viewer_id,)).fetchone()[0], 'Cidade A')

    def test_last_location_rename_revokes_city_grant(self):
        admin_token = self.admin_token()
        viewer_id = self.create_viewer(admin_token)
        with app.connect() as conn:
            location_id = conn.execute("select id from locations where city='Cidade A'").fetchone()[0]
        result = self.call(app.App.do_POST, '/api/locations', admin_token,
                           {'id': location_id, 'city': 'Cidade B', 'block': 'Bloco 1'})
        self.assertEqual(result[0], 200)
        self.assertEqual(self.call(app.App.do_GET, '/api/users', admin_token)[1]['items'][0]['cities'], [])
        with app.connect() as conn:
            conn.execute("insert into locations(city,block) values('Cidade A','Bloco 1')")
            old_city_location = conn.execute("select id from locations where city='Cidade A'").fetchone()[0]
            conn.execute('update nvrs set location_id=? where id=?', (old_city_location, self.nvr['id']))
        viewer_token = app.create_session(viewer_id)
        app.change_password(self.handler('/api/change-password', viewer_token),
                            {'current_password': 'InitialPass123', 'new_password': 'ChangedPass123'})
        self.assertEqual(self.call(app.App.do_GET, '/api/nvrs', viewer_token)[1]['items'], [])


if __name__ == "__main__":
    unittest.main()
