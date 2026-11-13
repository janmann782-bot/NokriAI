"""Catch hosting failures where Python runs outside the project root."""

import gzip
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nokri.paths import bundled_path


class ResourceTests(unittest.TestCase):
    def test_resources_are_independent_of_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            try:
                os.chdir(directory)
                models = json.loads(bundled_path('config/models.json').read_text(encoding='utf-8'))
                deployment = json.loads(bundled_path('config/deployment.json').read_text(encoding='utf-8'))
                budgets = json.loads(bundled_path('config/budgets.json').read_text(encoding='utf-8'))
                self.assertIn('flash', models)
                self.assertIn('owner_id', deployment)
                self.assertIn('default_day', budgets)
                with gzip.open(bundled_path('assets/curated.jsonl.gz'), 'rt', encoding='utf-8') as src:
                    self.assertIsInstance(json.loads(next(line for line in src if line.strip())), dict)
            finally:
                os.chdir(previous)

    def test_pip_install_falls_back_to_packaged_resources(self):
        with tempfile.TemporaryDirectory() as missing_source:
            with patch('nokri.paths.SOURCE_ROOT', Path(missing_source)):
                for resource in ('config/budgets.json', 'config/deployment.json', 'config/models.json',
                                 'assets/curated.jsonl.gz'):
                    self.assertTrue(bundled_path(resource).is_file())

    def test_resource_paths_cannot_escape(self):
        for path in ('../private', '/etc/passwd'):
            with self.assertRaises(ValueError):
                bundled_path(path)


class MissingSeedTests(unittest.TestCase):
    def test_missing_archive_does_not_block_initialization(self):
        from nokri.cli import initialize
        from nokri.db import DB
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            db = DB(Path(directory) / 'nokri.sqlite')
            original = bundled_path

            def missing_seed(name):
                if name == 'assets/curated.jsonl.gz':
                    raise FileNotFoundError('test missing')
                return original(name)

            with patch('nokri.cli.bundled_path', side_effect=missing_seed):
                initialize(db)
            self.assertFalse(db.setting('seed_loaded', False))
            self.assertEqual(len(db.rows('SELECT id FROM models')), 4)


if __name__ == '__main__':
    unittest.main()
