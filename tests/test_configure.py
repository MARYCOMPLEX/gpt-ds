"""Installer checks. All targets, credentials and conversations are local fixtures."""
import argparse
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import configure as app

PARENT = '11111111-1111-4111-8111-111111111111'
CHILD = '22222222-2222-4222-8222-222222222222'
CONFIG = '''# preserve this comment
model = "gpt-6.1-sol"
model_provider = "fixture_cpa"

[model_providers.fixture_cpa]
base_url = "https://cpa.gojia.cloud/v1"
wire_api = "responses"
experimental_bearer_token = "fixture-never-print-me"

[mcp_servers.fixture]
command = "fixture-command"
'''


def fixture(root):
    (root / 'config.toml').write_text(CONFIG)
    models = [
        {'slug': app.core.MAIN_MODEL, 'base_instructions': 'main-fixture', 'multi_agent_version': 'v2', 'context_window': 272000, 'max_context_window': 872000, 'auto_compact_token_limit': 244800, 'supports_search_tool': True, 'tool_mode': 'code_mode_only'},
        {'slug': 'gpt-5.5', 'base_instructions': 'template-fixture', 'multi_agent_version': 'v2', 'use_responses_lite': True},
    ]
    (root / 'models_cache.json').write_text(json.dumps({'models': models, 'fixture_metadata': 'preserve'}))
    return argparse.Namespace(config_file='config.toml', main_model=None, source=None, worker_template='gpt-5.5', worker_context_window=1_000_000, expected_provider_host='cpa.gojia.cloud')


def records_bytes(records):
    return b''.join((json.dumps(row, ensure_ascii=False) + '\n').encode() for row in records)


class TomlChecks(unittest.TestCase):
    def test_preserves_unrelated_values_and_multiline_text(self):
        original = (CONFIG + '''
[features]
multi_agent = false # update this only
[custom]
prompt = """keep me
[features]
multi_agent = false
"""
values = ["[agents]", "# not comment"]
''').encode()
        result = app.merge_toml(original, {('features', 'multi_agent'): True, ('agents', 'enabled'): True})
        expected = tomllib.loads(original.decode())
        expected['features']['multi_agent'] = True
        expected['agents'] = {'enabled': True}
        self.assertEqual(tomllib.loads(result.decode()), expected)
        self.assertIn(b'# preserve this comment', result)
        self.assertIn(b'prompt = """keep me\n[features]\nmulti_agent = false\n"""', result)

    def test_insert_root_before_tables(self):
        original = b'[features]\nmulti_agent=true\n'
        result = app.merge_toml(original, {('model_catalog_json',): 'models.json'})
        self.assertEqual(tomllib.loads(result.decode()), {'features': {'multi_agent': True}, 'model_catalog_json': 'models.json'})

    def test_implied_parent_table_can_be_added(self):
        original = b'[agents.existing]\ndescription="keep"\n'
        result = app.merge_toml(original, {('agents', 'enabled'): True})
        self.assertEqual(tomllib.loads(result.decode())['agents'], {'existing': {'description': 'keep'}, 'enabled': True})

    def test_quoted_table_and_key(self):
        original = b'["features"]\n"multi_agent" = false\n'
        result = app.merge_toml(original, {('features', 'multi_agent'): True})
        self.assertIs(tomllib.loads(result.decode())['features']['multi_agent'], True)

    def test_arrays_and_literal_strings_are_not_headers(self):
        original = b"text = '''\n[agents]\nenabled = false\n'''\nitems = [\n '[features]',\n]\n"
        result = app.merge_toml(original, {('agents', 'enabled'): True})
        self.assertEqual(tomllib.loads(result.decode())['text'], '\n[agents]\nenabled = false\n'.lstrip('\n'))
        self.assertTrue(tomllib.loads(result.decode())['agents']['enabled'])

    def test_idempotent_and_preserves_bom_crlf(self):
        original = b'\xef\xbb\xbfmodel = "keep"\r\n'
        updates = {('features', 'multi_agent'): True}
        first = app.merge_toml(original, updates)
        self.assertTrue(first.startswith(b'\xef\xbb\xbf'))
        self.assertIn(b'\r\n', first)
        self.assertEqual(first, app.merge_toml(first, updates))

    def test_boolean_not_confused_with_integer(self):
        result = app.merge_toml(b'[features]\nmulti_agent=1\n', {('features', 'multi_agent'): True})
        self.assertIs(tomllib.loads(result.decode())['features']['multi_agent'], True)

    def test_ambiguous_inline_and_dotted_layout_stop(self):
        for original in [b'features={multi_agent=false}\n', b'features.multi_agent=false\n']:
            with self.subTest(original=original), self.assertRaises(ValueError):
                app.merge_toml(original, {('features', 'multi_agent'): True})


class InstructionsChecks(unittest.TestCase):
    def test_managed_block_preserves_user_instructions(self):
        original = b'# existing custom instructions\n'
        result = app.merge_instructions(original, 'worker policy')
        self.assertTrue(result.startswith(original))
        self.assertEqual(result, app.merge_instructions(result, 'worker policy'))
        updated = app.merge_instructions(result + b'\n# tail\n', 'new policy')
        self.assertIn(b'# tail', updated)
        self.assertIn(b'new policy', updated)
        self.assertNotIn(b'worker policy', updated)

    def test_broken_markers_stop(self):
        for original in [app.BEGIN, app.END, app.BEGIN + app.BEGIN + app.END + app.END, app.END + app.BEGIN]:
            with self.subTest(original=original), self.assertRaises(ValueError):
                app.merge_instructions(original.encode(), 'policy')


class InstallerChecks(unittest.TestCase):
    def test_discovery_lists_but_never_selects_instances(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            home = Path(directory).resolve()
            for relative in ['.codex', '.antigravity_cockpit/instances/codex/one', '.antigravity_cockpit/instances/codex/two']:
                target = home / relative
                target.mkdir(parents=True)
                fixture(target)
            result = app.discover(home)
            self.assertEqual(len(result['candidates']), 3)
            self.assertFalse(result['writes_performed'])
            self.assertNotIn('fixture-never-print-me', json.dumps(result))

    def test_plan_is_read_only_and_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            args = fixture(root)
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            summary, changes = app.plan(root, args)
            self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, before)
            self.assertEqual(len(changes), 4)
            self.assertNotIn('fixture-never-print-me', json.dumps(summary))
            self.assertFalse(summary['runtime_verified'])

    def test_install_and_restore_are_scoped_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app.core, 'runtime_pids', return_value=[]), contextlib.redirect_stdout(io.StringIO()) as output:
            root = Path(directory).resolve()
            args = fixture(root)
            original_config = (root / 'config.toml').read_bytes()
            original_source = (root / 'models_cache.json').read_bytes()
            summary, changes = app.plan(root, args)
            app.core.apply_changes(root, changes, confirmed=False)
            config = tomllib.loads((root / 'config.toml').read_text())
            original = tomllib.loads(original_config.decode())
            self.assertEqual(config['model'], original['model'])
            self.assertEqual(config['model_provider'], original['model_provider'])
            self.assertEqual(config['model_providers'], original['model_providers'])
            self.assertEqual(config['mcp_servers'], original['mcp_servers'])
            self.assertEqual((root / 'models_cache.json').read_bytes(), original_source)
            self.assertEqual(app.plan(root, args)[1], [])
            with patch.object(app.core, 'runtime_pids', return_value=[123]):
                app.core.apply_changes(root, [], confirmed=False)
            backup = next((root / 'repair_backups').iterdir())
            app.core.restore(root, argparse.Namespace(backup=str(backup), apply=True, offline_confirmed=False))
            self.assertEqual((root / 'config.toml').read_bytes(), original_config)
            self.assertFalse((root / app.CATALOG_NAME).exists())
            self.assertFalse((root / 'agents/deepseek_worker.toml').exists())
            self.assertFalse((root / 'AGENTS.md').exists())
            self.assertNotIn('fixture-never-print-me', output.getvalue())

    def test_existing_worker_is_upgraded_without_overwriting_custom_instructions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            args = fixture(root)
            (root / 'agents').mkdir()
            role = b'name="old"\nmodel="old"\nmodel_reasoning_effort="low"\ndescription="keep"\ndeveloper_instructions="custom instructions"\n'
            (root / 'agents/deepseek_worker.toml').write_bytes(role)
            (root / 'AGENTS.override.md').write_text('keep override\n')
            (root / 'AGENTS.md').write_text('keep other\n')
            _, changes = app.plan(root, args)
            values = {p.relative_to(root).as_posix(): new for p, old, new in changes}
            result = tomllib.loads(values['agents/deepseek_worker.toml'].decode())
            self.assertEqual(result['developer_instructions'], 'custom instructions')
            self.assertEqual(result['model'], app.core.WORKER_MODEL)
            self.assertEqual(result['model_reasoning_effort'], 'high')
            self.assertIn('AGENTS.override.md', values)
            self.assertNotIn('AGENTS.md', values)
            self.assertTrue(values['AGENTS.override.md'].startswith(b'keep override'))

    def test_launch_model_override_does_not_change_config_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            args = fixture(root)
            args.main_model = 'gpt-5.5'
            summary, changes = app.plan(root, args)
            updated = next(new for p, old, new in changes if p.name == 'config.toml')
            self.assertEqual(tomllib.loads(updated.decode())['model'], app.core.MAIN_MODEL)
            self.assertEqual(summary['main_model_for_catalog'], 'gpt-5.5')

    def test_selected_profile_file_preserves_base_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            args = fixture(root)
            args.config_file = 'work.config.toml'
            (root / args.config_file).write_text('model="gpt-5.5"\n')
            _, changes = app.plan(root, args)
            self.assertNotIn(root / 'config.toml', [p for p, _, _ in changes])
            updated = next(new for p, old, new in changes if p.name == args.config_file)
            self.assertEqual(tomllib.loads(updated.decode())['model'], 'gpt-5.5')

    def test_wrong_provider_and_global_window_overrides_stop(self):
        for replacement in [CONFIG.replace('cpa.gojia.cloud', 'other.invalid'), CONFIG.replace('wire_api = "responses"', 'wire_api = "chat"'), 'model_context_window=272000\n' + CONFIG]:
            with self.subTest(replacement=replacement[:40]), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                args = fixture(root)
                (root / 'config.toml').write_text(replacement)
                with self.assertRaises(ValueError):
                    app.plan(root, args)
                self.assertFalse((root / app.CATALOG_NAME).exists())

    def test_active_runtime_blocks_install(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app.core, 'runtime_pids', return_value=[123]):
            root = Path(directory).resolve()
            args = fixture(root)
            original = (root / 'config.toml').read_bytes()
            _, changes = app.plan(root, args)
            with self.assertRaises(ValueError):
                app.core.apply_changes(root, changes, confirmed=True)
            self.assertEqual((root / 'config.toml').read_bytes(), original)

    def test_write_failure_rolls_back_previous_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app.core, 'runtime_pids', return_value=[]):
            root = Path(directory).resolve()
            args = fixture(root)
            original = (root / 'config.toml').read_bytes()
            _, changes = app.plan(root, args)
            real_write = app.core.private_write
            calls = 0

            def fail_second(path, data):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError('fixture write failure')
                return real_write(path, data)

            with patch.object(app.core, 'private_write', side_effect=fail_second), self.assertRaises(OSError):
                app.core.apply_changes(root, changes, confirmed=False)
            self.assertEqual((root / 'config.toml').read_bytes(), original)
            self.assertFalse((root / app.CATALOG_NAME).exists())

    def test_restore_failure_rolls_back_previous_restore(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app.core, 'runtime_pids', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory).resolve()
            first, second = root / 'first', root / 'second'
            first.write_bytes(b'first-original')
            second.write_bytes(b'second-original')
            app.core.apply_changes(root, [(first, b'first-original', b'first-applied'), (second, b'second-original', b'second-applied')], confirmed=False)
            backup = next((root / 'repair_backups').iterdir())
            real_write = app.core.private_write
            calls = 0

            def fail_second(path, data):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError('fixture restore failure')
                return real_write(path, data)

            args = argparse.Namespace(backup=str(backup), apply=True, offline_confirmed=False)
            with patch.object(app.core, 'private_write', side_effect=fail_second), self.assertRaises(OSError):
                app.core.restore(root, args)
            self.assertEqual(first.read_bytes(), b'first-applied')
            self.assertEqual(second.read_bytes(), b'second-applied')
            app.core.restore(root, args)
            self.assertEqual(first.read_bytes(), b'first-original')
            self.assertEqual(second.read_bytes(), b'second-original')

    @unittest.skipIf(os.name == 'nt', 'Windows symlink privileges vary; path traversal has separate tests')
    def test_symlink_outside_target_stops_before_writes(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other:
            root = Path(directory).resolve()
            args = fixture(root)
            external = Path(other) / 'external.json'
            external.write_bytes(b'{}')
            (root / app.CATALOG_NAME).symlink_to(external)
            with self.assertRaises(ValueError):
                app.plan(root, args)
            self.assertEqual(external.read_bytes(), b'{}')

    def test_catalog_preserves_other_models_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture(root)
            source = root / 'models_cache.json'
            before = json.loads(source.read_text())
            after = json.loads(app.build_catalog(source, app.core.MAIN_MODEL, 'gpt-5.5', 1_000_000))
            main = copy.deepcopy(before['models'][0])
            main['multi_agent_version'] = 'v1'
            self.assertEqual(after['models'][0], main)
            self.assertEqual(after['models'][1], before['models'][1])
            self.assertEqual(after['fixture_metadata'], 'preserve')
            worker = after['models'][-1]
            self.assertEqual(worker['context_window'], 1_000_000)
            self.assertEqual(worker['max_context_window'], 1_000_000)
            self.assertEqual(worker['auto_compact_token_limit'], 900_000)
            self.assertFalse(worker['supports_search_tool'])
            self.assertIsNone(worker['tool_mode'])

    def test_existing_worker_catalog_is_upgraded_to_one_million(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture(root)
            source = root / 'models_cache.json'
            data = json.loads(source.read_text())
            data['models'].append({'slug': app.core.WORKER_MODEL, 'base_instructions': 'existing worker', 'context_window': 128000, 'max_context_window': 128000, 'auto_compact_token_limit': 115200, 'supports_search_tool': True, 'tool_mode': 'code_mode_only'})
            source.write_text(json.dumps(data))
            result = json.loads(app.build_catalog(source, app.core.MAIN_MODEL, None, 1_000_000))
            self.assertEqual(len(result['models']), 3)
            worker = result['models'][-1]
            self.assertEqual(worker['base_instructions'], 'existing worker')
            self.assertEqual(worker['context_window'], 1_000_000)
            self.assertEqual(worker['auto_compact_token_limit'], 900_000)
            self.assertEqual(worker['multi_agent_version'], 'v1')
            self.assertFalse(worker['supports_search_tool'])
            self.assertIsNone(worker['tool_mode'])

    def test_manifest_matches_defaults(self):
        manifest = json.loads((app.REPO / 'gpt-ds.json').read_text())
        self.assertEqual(manifest['supported_targets'], ['codex'])
        self.assertEqual(manifest['defaults']['worker_model'], app.core.WORKER_MODEL)
        self.assertEqual(manifest['defaults']['worker_context_window'], app.core.WORKER_CONTEXT_WINDOW)
        self.assertFalse(manifest['invariants']['automatic_restart'])


class NativeVerificationChecks(unittest.TestCase):
    def make_native(self, root, window=950000, tools=False, cli=False, reply='RESTART_OK'):
        fixture(root)
        (root / 'sessions').mkdir()
        for thread, model, is_worker in [(PARENT, app.core.MAIN_MODEL, False), (CHILD, app.core.WORKER_MODEL, True)]:
            source = {'subagent': {'thread_spawn': {'parent_thread_id': PARENT}}} if is_worker and not cli else 'cli'
            records = [
                {'type': 'session_meta', 'payload': {'id': thread, 'source': source, 'model_provider': 'fixture_cpa'}},
                {'type': 'turn_context', 'payload': {'model': model, 'effort': 'high', 'multi_agent_version': 'v1'}},
                {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'model_context_window': window}}},
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': reply}]}},
                {'type': 'event_msg', 'payload': {'type': 'task_complete'}},
            ]
            if tools and is_worker:
                records.insert(2, {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'exec'}})
            (root / 'sessions' / f'fixture-{thread}.jsonl').write_bytes(records_bytes(records))
        return argparse.Namespace(config_file='config.toml', parent_thread=PARENT, thread=CHILD, database=None, main_model=None, expected_provider_host='cpa.gojia.cloud')

    def test_native_fixture_passes(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as output:
            args = self.make_native(Path(directory).resolve())
            self.assertTrue(app.verify(Path(directory).resolve(), args))
            self.assertEqual(json.loads(output.getvalue())['status'], 'native_verified')

    def test_old_window_cli_tools_or_wrong_reply_do_not_pass(self):
        variants = [{'window': 121600}, {'cli': True}, {'tools': True}, {'reply': 'private-fixture-response'}]
        for variant in variants:
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as output:
                root = Path(directory).resolve()
                args = self.make_native(root, **variant)
                self.assertFalse(app.verify(root, args))
                self.assertNotIn('private-fixture-response', output.getvalue())

    def test_database_provider_overrides_stale_header(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory).resolve()
            args = self.make_native(root)
            with contextlib.closing(sqlite3.connect(root / 'state_5.sqlite')) as connection, connection:
                connection.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT, model_provider TEXT)')
                for thread in [PARENT, CHILD]:
                    connection.execute('INSERT INTO threads VALUES (?,?,?)', (thread, str(root / 'sessions' / f'fixture-{thread}.jsonl'), 'different' if thread == CHILD else 'fixture_cpa'))
            self.assertFalse(app.verify(root, args))

    def test_incomplete_new_turn_does_not_reuse_prior_success(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory).resolve()
            args = self.make_native(root)
            path = root / 'sessions' / f'fixture-{CHILD}.jsonl'
            with path.open('ab') as stream:
                stream.write(records_bytes([{'type': 'turn_context', 'payload': {'model': app.core.WORKER_MODEL, 'effort': 'high', 'multi_agent_version': 'v1'}}]))
            self.assertFalse(app.verify(root, args))


class CliChecks(unittest.TestCase):
    def call(self, root, *arguments):
        environment = {**os.environ, 'CODEX_HOME': str(root), 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
        result = subprocess.run([sys.executable, str(app.REPO / 'configure.py'), '--instance', str(root), *arguments], cwd=root, env=environment, text=True, encoding='utf-8', capture_output=True, timeout=30)
        self.assertNotIn('fixture-never-print-me', result.stdout + result.stderr)
        return result

    def test_outside_repo_preview_install_status_and_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture(root)
            original = (root / 'config.toml').read_bytes()
            result = self.call(root, 'install', '--worker-template', 'gpt-5.5')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['status'], 'preview')
            self.assertEqual((root / 'config.toml').read_bytes(), original)
            result = self.call(root, 'install', '--worker-template', 'gpt-5.5', '--apply', '--offline-confirmed')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('configured_pending_manual_restart', result.stdout)
            result = self.call(root, 'status')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['status'], 'configured_on_disk')
            backup = next((root / 'repair_backups').iterdir())
            result = self.call(root, 'restore', '--backup', str(backup), '--apply', '--offline-confirmed')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'config.toml').read_bytes(), original)
            self.assertFalse((root / app.CATALOG_NAME).exists())

    def test_cli_native_verification_reports_real_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            NativeVerificationChecks().make_native(root)
            result = self.call(root, 'verify', '--parent-thread', PARENT, '--thread', CHILD)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['status'], 'native_verified')

    def test_cli_explicit_history_migration_and_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture(root)
            (root / 'sessions').mkdir()
            path = root / 'sessions' / f'fixture-{PARENT}.jsonl'
            original = records_bytes([{'type': 'session_meta', 'payload': {'id': PARENT, 'multi_agent_version': 'v2'}}, {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'fixture message'}]}}])
            path.write_bytes(original)
            result = self.call(root, 'migrate', '--thread', PARENT)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(path.read_bytes(), original)
            result = self.call(root, 'migrate', '--thread', PARENT, '--apply', '--offline-confirmed')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(path.read_bytes()), len(original))
            self.assertEqual(json.loads(path.read_bytes().splitlines()[0])['payload']['multi_agent_version'], 'v1')
            backup = next((root / 'repair_backups').iterdir())
            result = self.call(root, 'restore', '--backup', str(backup), '--apply', '--offline-confirmed')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main(verbosity=2)
