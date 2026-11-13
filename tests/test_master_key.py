import os
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet
from nokri.db import DB
from nokri.master_key import master_key_path, resolve_master_key
from nokri.provider import Vault


class MasterKeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / 'state' / 'nokri.sqlite')
        self.key_file = master_key_path(self.db.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_blank_key_creates_reusable_local_secret(self):
        key1 = resolve_master_key(self.db, '')
        self.assertEqual(Fernet(key1).decrypt(Fernet(key1).encrypt(b'test')), b'test')
        self.assertTrue(self.key_file.is_file())
        if os.name == 'posix':
            self.assertEqual(self.key_file.stat().st_mode & 0o777, 0o600)
        v = Vault(self.db, key1)
        v.put('anymodel', 'example-secret-key')
        self.assertEqual(key1, resolve_master_key(DB(self.db.path)))
        self.assertEqual(Vault(self.db, resolve_master_key(self.db)).get('anymodel'), 'example-secret-key')

    def test_explicit_key_does_not_create_file(self):
        provided = Fernet.generate_key().decode()
        self.assertEqual(resolve_master_key(self.db, provided), provided.encode())
        self.assertFalse(self.key_file.exists())

    def test_no_silent_key_rotation_for_existing_secrets(self):
        old = resolve_master_key(self.db)
        Vault(self.db, old).put('anymodel', 'example-secret-key')
        self.key_file.unlink()
        with self.assertRaisesRegex(RuntimeError, 'Восстановите прежний ключ'):
            resolve_master_key(self.db)
        self.assertFalse(self.key_file.exists())
        self.assertEqual(resolve_master_key(self.db, old), old)

    def test_invalid_or_wrong_key_fails(self):
        key = resolve_master_key(self.db)
        Vault(self.db, key).put('anymodel', 'example-secret-key')
        with self.assertRaisesRegex(RuntimeError, 'не соответствует'):
            resolve_master_key(self.db, Fernet.generate_key())
        self.key_file.write_text('broken-key')
        with self.assertRaisesRegex(RuntimeError, 'некорректный ключ'):
            resolve_master_key(self.db)

    @unittest.skipUnless(os.name == 'posix', 'Unix permissions required')
    def test_insecure_file_permissions_are_rejected(self):
        resolve_master_key(self.db)
        self.key_file.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, 'chmod 600'):
            resolve_master_key(self.db)

    @unittest.skipUnless(os.name == 'posix', 'Unix symlinks required')
    def test_symlink_is_rejected(self):
        target = Path(self.tmp.name) / 'target'
        target.write_bytes(Fernet.generate_key())
        self.key_file.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, 'символической ссылкой'):
            resolve_master_key(self.db)
