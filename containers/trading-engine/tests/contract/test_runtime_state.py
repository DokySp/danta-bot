"""Offline regressions for ledger isolation and bounded idle account reuse."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from danta.runtime import RuntimeState


class RuntimeStateContracts(unittest.TestCase):
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
