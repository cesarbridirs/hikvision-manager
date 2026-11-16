import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_regressions as fixtures
import app


class BootstrapTests(unittest.TestCase):
    def test_new_install_requires_private_admin_password(self):
        with tempfile.TemporaryDirectory() as directory:
            original = app.DB_PATH, app.KEY_PATH, app.CREDENTIALS
            app.DB_PATH = Path(directory) / 'test.db'
            app.KEY_PATH = Path(directory) / 'credentials.key'
            try:
                with patch.dict(os.environ, {}, clear=True):
                    with self.assertRaisesRegex(RuntimeError, 'HIKVISION_INITIAL_ADMIN_PASSWORD'):
                        app.init_db()
                    with app.connect() as conn:
                        self.assertEqual(conn.execute('select count(*) from app_users').fetchone()[0], 0)
                    os.environ['HIKVISION_INITIAL_ADMIN_PASSWORD'] = 'PrivateTestPassword123'
                    app.init_db()
                    with app.connect() as conn:
                        user = conn.execute("select password_hash,salt,must_change_password from app_users where username='admin'").fetchone()
                    self.assertEqual(user['must_change_password'], 1)
                    self.assertNotEqual(user['password_hash'], 'PrivateTestPassword123')
            finally:
                app.DB_PATH, app.KEY_PATH, app.CREDENTIALS = original


if __name__ == '__main__':
    unittest.main()
