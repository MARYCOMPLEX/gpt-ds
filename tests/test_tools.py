"""Fixture checks for session preservation, active-snapshot selection and rollback."""
import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('cpa_tools', Path(__file__).resolve().parents[1] / 'codex_cpa_tools.py')
tools = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tools)
THREAD = '11111111-1111-4111-8111-111111111111'


def encoded(records):
    return b''.join((json.dumps(r, ensure_ascii=False) + '\n').encode() for r in records)


class MigrationChecks(unittest.TestCase):
    def test_only_runtime_metadata_changes(self):
        records = [
            {'type': 'session_meta', 'payload': {'id': THREAD, 'multi_agent_version': 'v2'}},
            {'type': 'turn_context', 'payload': {'model': 'gpt-6.1-sol', 'multi_agent_version': 'v2'}},
            {'type': 'compacted', 'payload': {'resume_metadata': {'multi_agent_version': 'v2'}, 'message': '保留摘要'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': '不要改正文："multi_agent_version":"v2"'}]}},
            {'type': 'response_item', 'payload': {'type': 'agent_message', 'content': [{'type': 'encrypted_content', 'encrypted_content': 'fixture-opaque-data'}]}},
        ]
        original = encoded(records)
        migrated, count = tools.migrated_bytes(original)
        self.assertEqual(count, 3)
        self.assertEqual(len(original), len(migrated))
        after = [json.loads(line) for line in migrated.splitlines()]
        self.assertEqual(after[-2:], records[-2:])
        self.assertEqual(after[2]['payload']['resume_metadata']['multi_agent_version'], 'v1')
        self.assertEqual(tools.migrated_bytes(migrated), (migrated, 0))

    def test_unexpected_metadata_layout_is_rejected(self):
        original = encoded([{'type': 'turn_context', 'payload': {'multi_agent_version': 'v2', 'other': {'multi_agent_version': 'v2'}}}])
        with self.assertRaises(ValueError):
            tools.migrated_bytes(original)

    def test_index_picks_current_suffixed_rollout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'sessions').mkdir()
            old = root / 'sessions' / f'old-{THREAD}.jsonl'
            current = root / 'sessions' / f'current-{THREAD}_revision.jsonl'
            data = encoded([{'type': 'session_meta', 'payload': {'id': THREAD}}])
            old.write_bytes(data)
            current.write_bytes(data)
            with contextlib.closing(sqlite3.connect(root / 'state_5.sqlite')) as connection, connection:
                connection.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT)')
                connection.execute('INSERT INTO threads VALUES (?, ?)', (THREAD, str(current)))
            self.assertEqual(tools.resolve_rollout(root, THREAD), current)
            (root / 'state_5.sqlite').unlink()
            with self.assertRaises(ValueError):
                tools.resolve_rollout(root, THREAD)

    def test_paths_outside_instance_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with self.assertRaises(ValueError):
                tools.within(root, '../other-instance/session.jsonl')

    def test_active_runtime_blocks_writes(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, 'runtime_pids', return_value=[123]):
            root = Path(directory).resolve()
            with self.assertRaises(ValueError):
                tools.apply_changes(root, [(root / 'test', None, b'new')], confirmed=True)
            self.assertFalse((root / 'test').exists())

    def test_backup_and_restore_preserve_bytes(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, 'runtime_pids', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory).resolve()
            target = root / 'session.jsonl'
            target.write_bytes(b'original\n')
            tools.apply_changes(root, [(target, b'original\n', b'migrated\n')], confirmed=False)
            backup = next((root / 'repair_backups').iterdir())
            args = argparse.Namespace(backup=str(backup), apply=True, offline_confirmed=False)
            tools.restore(root, args)
            self.assertEqual(target.read_bytes(), b'original\n')

    def test_restore_preserves_later_conversation(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, 'runtime_pids', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory).resolve()
            target = root / 'session.jsonl'
            target.write_bytes(b'original\n')
            tools.apply_changes(root, [(target, b'original\n', b'migrated\n')], confirmed=False)
            backup = next((root / 'repair_backups').iterdir())
            target.write_bytes(b'migrated\nnew-turn\n')
            args = argparse.Namespace(backup=str(backup), apply=True, offline_confirmed=False)
            with self.assertRaises(ValueError):
                tools.restore(root, args)
            self.assertEqual(target.read_bytes(), b'migrated\nnew-turn\n')

    def test_catalog_generation_preserves_main_and_registers_worker(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, 'runtime_pids', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory).resolve()
            source = root / 'native.json'
            main = {'slug': 'gpt-6.1-sol', 'base_instructions': 'native-fixture', 'multi_agent_version': 'v2', 'tool_mode': 'code_mode_only', 'supports_search_tool': True}
            template = {'slug': 'gpt-5.5', 'base_instructions': 'native-fixture', 'use_responses_lite': True}
            source.write_text(json.dumps({'models': [main, template]}))
            original = source.read_bytes()
            args = argparse.Namespace(source=str(source), main_model='gpt-6.1-sol', worker_model='deepseek-v4.1-flash', worker_template='gpt-5.5', worker_context_window=1_000_000, output='generated.json', apply=True, offline_confirmed=False)
            tools.prepare_catalog(root, args)
            result = json.loads((root / 'generated.json').read_text())['models']
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(result[0], {**main, 'multi_agent_version': 'v1'})
            worker = next(m for m in result if m['slug'] == 'deepseek-v4.1-flash')
            self.assertEqual(worker['multi_agent_version'], 'v1')
            self.assertFalse(worker['supports_search_tool'])
            self.assertFalse(worker['use_responses_lite'])
            self.assertIsNone(worker['tool_mode'])
            self.assertEqual(worker['context_window'], 1_000_000)
            self.assertEqual(worker['max_context_window'], 1_000_000)
            self.assertEqual(worker['auto_compact_token_limit'], 900_000)

    def test_existing_worker_window_is_updated_without_changing_other_models(self):
        for window in [1_000_000, 256_000]:
            with self.subTest(window=window), tempfile.TemporaryDirectory() as directory, patch.object(tools, 'runtime_pids', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                root = Path(directory).resolve()
                source = root / 'native.json'
                main = {'slug': 'gpt-6.1-sol', 'base_instructions': 'main', 'multi_agent_version': 'v1', 'context_window': 272000, 'max_context_window': 872000, 'auto_compact_token_limit': 244800}
                worker = {'slug': 'deepseek-v4.1-flash', 'base_instructions': 'worker', 'multi_agent_version': 'v1', 'supports_search_tool': False, 'context_window': 128000, 'max_context_window': 128000, 'auto_compact_token_limit': 115200, 'tool_mode': None, 'effective_context_window_percent': 95}
                other = {'slug': 'another-model', 'base_instructions': 'other', 'context_window': 64000}
                source.write_text(json.dumps({'models': [main, worker, other]}))
                original = source.read_bytes()
                args = argparse.Namespace(source=str(source), main_model=tools.MAIN_MODEL, worker_model=tools.WORKER_MODEL, worker_template=None, worker_context_window=window, output='generated.json', apply=True, offline_confirmed=False)
                tools.prepare_catalog(root, args)
                result = json.loads((root / 'generated.json').read_text())['models']
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual(result[0], main)
                self.assertEqual(result[2], other)
                self.assertEqual(result[1], {**worker, 'context_window': window, 'max_context_window': window, 'auto_compact_token_limit': window * 9 // 10})

    def test_cli_default_worker_window_is_one_million(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, 'runtime_pids', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory).resolve()
            config = root / 'config.toml'
            config.write_text('model = "gpt-6.1-sol"\nmodel_provider = "fixture"\n')
            original_config = config.read_bytes()
            source = root / 'native.json'
            source.write_text(json.dumps({'models': [
                {'slug': 'gpt-6.1-sol', 'base_instructions': 'main'},
                {'slug': 'gpt-5.5', 'base_instructions': 'template'},
            ]}))
            with patch('sys.argv', ['codex_cpa_tools.py', '--instance', str(root), 'catalog', '--source', str(source), '--worker-template', 'gpt-5.5', '--apply']):
                tools.main()
            models = json.loads((root / 'models.cpa-deepseek-v1.json').read_text())['models']
            worker = next(m for m in models if m['slug'] == tools.WORKER_MODEL)
            self.assertEqual(worker['context_window'], 1_000_000)
            self.assertEqual(worker['max_context_window'], 1_000_000)
            self.assertEqual(worker['auto_compact_token_limit'], 900_000)
            self.assertEqual(config.read_bytes(), original_config)

    def test_catalog_preview_does_not_write_files(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory).resolve()
            source = root / 'native.json'
            source.write_text(json.dumps({'models': [
                {'slug': tools.MAIN_MODEL, 'base_instructions': 'main'},
                {'slug': tools.WORKER_MODEL, 'base_instructions': 'worker', 'context_window': 128000},
            ]}))
            original = source.read_bytes()
            args = argparse.Namespace(source=str(source), main_model=tools.MAIN_MODEL, worker_model=tools.WORKER_MODEL, worker_template=None, worker_context_window=1_000_000, output='generated.json', apply=False, offline_confirmed=False)
            tools.prepare_catalog(root, args)
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse((root / 'generated.json').exists())
            self.assertFalse((root / 'repair_backups').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
