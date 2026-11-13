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
                with gzip.open(bundled_path('data/curated.jsonl.gz'), 'rt', encoding='utf-8') as src:
                    self.assertIsInstance(json.loads(next(line for line in src if line.strip())), dict)
            finally:
                os.chdir(previous)

    def test_pip_install_falls_back_to_packaged_resources(self):
        with tempfile.TemporaryDirectory() as missing_source:
            with patch('nokri.paths.SOURCE_ROOT', Path(missing_source)):
                for resource in ('config/budgets.json', 'config/deployment.json', 'config/models.json',
                                 'data/curated.jsonl.gz'):
                    self.assertTrue(bundled_path(resource).is_file())

    def test_resource_paths_cannot_escape(self):
        for path in ('../private', '/etc/passwd'):
            with self.assertRaises(ValueError):
                bundled_path(path)


if __name__ == '__main__':
    unittest.main()
