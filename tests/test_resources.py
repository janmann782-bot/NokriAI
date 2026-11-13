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

    def test_verified_canon_exists_in_both_install_layouts(self):
        from nokri.paths import SOURCE_ROOT,PACKAGE_ROOT
        for root in (SOURCE_ROOT,PACKAGE_ROOT):
            p=root/'assets/canon_2061.jsonl'
            self.assertTrue(p.is_file(),str(p))
            item=json.loads(p.read_text(encoding='utf-8').splitlines()[0])
            self.assertIn('3 автономии',item['text'])
            self.assertEqual(item['access'],'public')

    def test_public_wiki_export_and_author_patch_are_packaged(self):
        from nokri.paths import SOURCE_ROOT, PACKAGE_ROOT
        for root in (SOURCE_ROOT, PACKAGE_ROOT):
            parts=list((root/'assets').glob('wiki_20261112.part*.xml'))
            self.assertEqual(len(parts), 8)
            self.assertTrue((root/'assets/canon_2061_additions.jsonl').is_file())

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
            self.assertEqual(db.setting('canon_2061_v1_loaded'),True)
            self.assertTrue(any('3 автономии' in row['text'] for row in
                                db.rows("SELECT text FROM knowledge WHERE id='canon-kefirstan-autonomies-2061-v1'")))


if __name__ == '__main__':
    unittest.main()
