"""Offline regressions for ledger isolation and bounded idle account reuse."""
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from danta.runtime import RuntimeState


class RuntimeStateContracts(unittest.TestCase):
    def test_large_originals_migrate_per_receipt_and_are_loaded_only_on_request(self):
        from danta.disclosure_cache import iter_records
        import io
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'state.sqlite'
            original = {'event': {'available_at': '2026-09-23T01:00:00+00:00'},
                        'documents': {'fact': {'receipt_id': 'old', 'sha256': 'digest', 'content': '한글' * 600000}}}
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE runtime_cache(namespace TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            db.execute('INSERT INTO runtime_cache VALUES (?,?)', ('disclosure_records', json.dumps({'old': original}, ensure_ascii=False)))
            db.commit(); db.close()
            state = RuntimeState(path)
            self.assertNotIn('content', state.data['disclosure_records']['old']['documents']['fact'])
            self.assertEqual(state.disclosure_documents('old'), original['documents'])
            self.assertEqual(state.data['disclosure_records']['old']['event'], original['event'])
            self.assertIsNotNone(state.db.execute("SELECT 1 FROM runtime_cache WHERE namespace='disclosure_records'").fetchone())
            state.close()
            with patch('danta.disclosure_cache.iter_records', side_effect=AssertionError('already migrated')):
                restored = RuntimeState(path)
                restored.save()
                self.assertEqual(restored.disclosure_documents('old'), original['documents'])
                restored.close()
            for invalid in (b'{"a":1,}', b'{"a":1', b'{"a":1} trailing'):
                with self.assertRaises(ValueError):
                    list(iter_records(io.BytesIO(invalid)))

    def test_interrupted_migration_retries_legacy_without_losing_originals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'state.sqlite'
            legacy = sqlite3.connect(path)
            legacy.execute('CREATE TABLE runtime_cache(namespace TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            legacy.execute('INSERT INTO runtime_cache VALUES (?,?)', ('disclosure_records', '{"old":{"documents":{"fact":{"content":"original"}}}}'))
            legacy.commit(); legacy.close()
            cache = sqlite3.connect(path.with_name('state-cache.sqlite'))
            cache.execute('CREATE TABLE runtime_cache(namespace TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            cache.execute("INSERT INTO runtime_cache VALUES ('events','{}')")
            cache.commit(); cache.close()
            state = RuntimeState(path)
            self.assertEqual(state.disclosure_documents('old')['fact']['content'], 'original')
            state.close()

    def test_cache_blob_remains_on_disk_without_reimport_or_reserialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'state.sqlite'
            cache = sqlite3.connect(path.with_name('state-cache.sqlite'))
            cache.execute('CREATE TABLE runtime_cache(namespace TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            original = '{"old":{"documents":{"fact":{"content":"original"}}}}'
            cache.execute('INSERT INTO runtime_cache VALUES (?,?)', ('disclosure_records', original))
            cache.commit(); cache.close()
            RuntimeState(path).close()
            with patch('danta.disclosure_cache.iter_records', side_effect=AssertionError('already migrated')):
                state = RuntimeState(path)
                state.save()
                self.assertNotIn('content', state.data['disclosure_records']['old']['documents']['fact'])
                self.assertEqual(state.disclosure_documents('old')['fact']['content'], 'original')
                self.assertEqual(state.cache_db.execute("SELECT payload FROM runtime_cache WHERE namespace='disclosure_records'").fetchone()[0], original)
                state.close()

    def test_legacy_cache_migrates_and_cannot_block_ledger_transactions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'state.sqlite'
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE runtime_cache(namespace TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            db.execute('INSERT INTO runtime_cache VALUES (?,?)',('disclosure_records',json.dumps({'old':'official document'})))
            db.commit()
            db.close()
            state = RuntimeState(path)
            try:
                self.assertEqual(state.data['disclosure_records'],{'old':'official document'})
                state.db.execute('BEGIN IMMEDIATE')
                state.data['observations']['test'] = {'revision':2}
                state.save(('observations',))
                self.assertTrue(state.db.in_transaction)
                state.db.execute('ROLLBACK')
                self.assertEqual(state.db.execute('SELECT COUNT(*) FROM runtime_cache').fetchone()[0],1)
            finally:
                state.close()
            restored = RuntimeState(path)
            try:
                self.assertEqual(restored.data['observations']['test']['revision'],2)
            finally:
                restored.close()



if __name__ == '__main__':
    unittest.main()
