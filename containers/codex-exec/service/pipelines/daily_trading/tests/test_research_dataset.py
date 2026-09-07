import json
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import date

from ..scripts import dart_history as dart, kis_history, research_dataset as dataset
from .test_dart_history import archive, filing, xbrl


class ResearchDatasetTests(unittest.TestCase):
    def test_reject_sidecar_backdating_and_wrong_company_code_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = dart.filing_metadata(filing())
            dart.save_json(root / 'filings-source.json', [row])
            sidecar = root / (row['rcept_no'] + '.json')
            for altered in ({**row, 'received_on': '2020-01-01', 'available_on': '2020-01-02'},
                            {**row, 'stock_code': '000020'}, {**row, 'period_end': '2023-03-31'}):
                dart.save_json(sidecar, altered)
                with self.assertRaisesRegex(ValueError, 'source filing identity'):
                    dataset.load_financials([root])
            # Even agreeing files cannot put receipt before the financial period ends.
            altered = {**row, 'received_on': '2020-01-01', 'available_on': '2020-01-02'}
            dart.save_json(sidecar, altered)
            dart.save_json(root / 'filings-source.json', [altered])
            with self.assertRaises((ValueError, dart.DartError)):
                dataset.load_financials([root])

    def test_official_received_date_can_differ_from_receipt_identifier_either_way(self):
        for receipt, received, report in [
                ('20210311001234', '20210312', '사업보고서 (2020.12)'),
                ('20250401000004', '20250331', '사업보고서 (2024.12)')]:
            with self.subTest(receipt=receipt), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                row = dart.filing_metadata(filing(receipt, received, report))
                dart.save_json(root / 'filings-source.json', [row])
                dart.save_json(root / (receipt + '.json'), row)
                records = dataset.load_financials([root])
                self.assertIsNone(dart.latest_asof(records, date.fromisoformat(row['received_on']), research=True))
                selected = dart.latest_asof(records, date.fromisoformat(row['available_on']), research=True)
                self.assertEqual(selected['rcept_no'], receipt)
                self.assertEqual(selected['received_on'], row['received_on'])

    def test_archive_identity_and_storage_specific_hash_cannot_be_substituted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = dart.filing_metadata(filing())
            latest = dart.filing_metadata(filing('20240601000001', '20240601', '[기재정정]분기보고서 (2024.03)'))
            payload = archive(xbrl())
            sha = hashlib.sha256(payload).hexdigest()
            name = old['rcept_no'] + '.zip'
            (root / name).write_bytes(payload)
            dart.save_json(root / 'filings-source.json', [old, latest])
            sidecar = root / name.replace('.zip', '.json')
            dart.save_json(sidecar, {**old, 'archive': name, 'sha256': '0' * 64, 'stored_sha256': sha})
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                dataset.load_financials([root])
            dart.save_json(sidecar, {**old, 'archive': name, 'sha256': sha})
            dart.save_json(root / (latest['rcept_no'] + '.json'), {
                **latest, 'archive': name, 'sha256': sha, 'financial_data_issue': 'conflicting facts'})
            with self.assertRaisesRegex(ValueError, 'receipt/name mismatch'):
                dataset.load_financials([root])

    def test_compact_archive_join_and_pending_correction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = dart.filing_metadata(filing())
            compact, storage = dart.compact_archive(archive(xbrl()))
            name = original['rcept_no'] + '.instance.zip'
            (root / name).write_bytes(compact)
            dart.save_json(root / name.replace('.zip', '.json'), {**original, **storage, 'archive': name})
            correction = dart.filing_metadata(filing('20240601000001', '20240601', '[기재정정]분기보고서 (2024.03)'))
            dart.save_json(root / 'filings-example.json', [original, correction])
            rows = dataset.load_financials([root])
            before = dart.latest_asof(rows, date(2024, 6, 1), research=True)
            after = dart.latest_asof(rows, date(2024, 6, 2), research=True)
            self.assertTrue(before['research_growth']['earnings_improved'])
            self.assertEqual(after['research_growth']['status'], 'unavailable')
            (root / name).write_bytes(b'bad')
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                dataset.load_financials([root])

    def test_join_uses_previous_session_and_excludes_same_day_information(self):
        universe = sqlite3.connect(':memory:')
        universe.executescript('''CREATE TABLE universe_snapshots(snapshot_date,market,status);
          CREATE TABLE universe_membership(snapshot_date,code,market);
          INSERT INTO universe_snapshots VALUES('2024-05-16','STK','complete'),('2024-05-16','KSQ','complete');
          INSERT INTO universe_membership VALUES('2024-05-16','005930','STK'),('2024-05-17','0013V0','KSQ');''')
        prices = sqlite3.connect(':memory:')
        kis_history.initialize(prices)
        for day in ('2024-05-16', '2024-05-17'):
            prices.execute('INSERT INTO history_bars VALUES (?,?,?,?)', ('index', '0001', day, '{}'))
            row = {'stck_bsop_date': day.replace('-', ''), 'stck_clpr': '100'}
            prices.execute('INSERT INTO history_bars VALUES (?,?,?,?)', ('stock', '005930', day, json.dumps(row)))
        old = dart.filing_metadata(filing())
        old['facts'] = dart.consolidated_facts(archive(xbrl()), old['corp_code'])
        future = {**old, 'received_on': '2024-05-17', 'available_on': '2024-05-18', 'rcept_no': '20240517000001', 'facts': []}
        result = dataset.join_day(universe, prices, [old, future], date(2024, 5, 17))
        self.assertEqual(len(result['candidates']), 1)
        candidate = result['candidates'][0]
        self.assertEqual(candidate['code'], '005930')
        self.assertEqual([b['date'] for b in candidate['bars']], ['2024-05-16'])
        self.assertEqual(candidate['financial']['rcept_no'], old['rcept_no'])
        self.assertFalse(candidate['history_complete_60'])
        self.assertEqual(dataset.join_day(universe, prices, [], date(2024, 5, 16))['status'], 'unavailable')
        universe.close()
        prices.close()

    def test_conflict_is_reparsed_quarantined_and_never_revives_older_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            older = dart.filing_metadata(filing())
            latest = dart.filing_metadata(filing('20240601000001', '20240601', '[기재정정]분기보고서 (2024.03)'))
            duplicate = '<ifrs-full:Revenue contextRef="c2024" unitRef="u">1300</ifrs-full:Revenue>'
            for row, xml in [(older, xbrl()), (latest, xbrl(duplicate=duplicate))]:
                packed, storage = dart.compact_archive(archive(xml))
                name = row['rcept_no'] + '.instance.zip'
                (root / name).write_bytes(packed)
                # Deliberately false flags and facts must not override the source archive.
                dart.save_json(root / name.replace('.zip', '.json'), {
                    **row, **storage, 'archive': name, 'facts': [{'amount_krw': 1}],
                    'financial_data_issue': 'invented', 'research_growth': {'status': 'available'}})
            dart.save_json(root / 'filings-source.json', [older, latest])
            records = dataset.load_financials([root])
            before = dart.latest_asof(records, date(2024, 6, 1), research=True)
            after = dart.latest_asof(records, date(2024, 6, 2), research=True)
            self.assertTrue(before['research_growth']['earnings_improved'])
            self.assertNotIn('financial_data_issue', before)
            self.assertEqual(after['rcept_no'], latest['rcept_no'])
            self.assertEqual(after['facts'], [])
            self.assertIn('conflicting facts', after['financial_data_issue'])
            self.assertEqual(after['research_growth']['status'], 'unavailable')
            packed, storage = dart.compact_archive(archive(xbrl().replace(b'>1200<', b'>NaN<')))
            name = latest['rcept_no'] + '.instance.zip'
            (root / name).write_bytes(packed)
            dart.save_json(root / name.replace('.zip', '.json'), {
                **latest, **storage, 'archive': name, 'financial_data_issue': 'conflicting facts'})
            with self.assertRaisesRegex(dart.DartError, 'finite whole KRW'):
                dataset.load_financials([root])

    def test_frozen_strategy_is_research_only_and_disables_q4_synthesis(self):
        path = Path(dataset.__file__).resolve().parent.parent / 'research-strategy-v1.json'
        rule = json.loads(path.read_text())
        self.assertEqual(rule['information_cutoff']['agent_calls'], 0)
        self.assertFalse(rule['universe']['historical_industry_filter'])
        self.assertIn('not synthetic Q4', rule['financial_filter']['annual'])
        self.assertEqual(rule['entry']['maximum_positions'], 5)
        self.assertIn('not_live_or_proven', rule['status'])


if __name__ == '__main__':
    unittest.main()
