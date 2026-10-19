#!/usr/bin/env python3
"""Agent-friendly Codex installer. Python 3.11+, standard library, no network."""
from __future__ import annotations

import argparse
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tomllib
from urllib.parse import urlsplit

import codex_cpa_tools as core

REPO = Path(__file__).resolve().parent
CATALOG_NAME = 'models.gpt-ds-v1.json'
BEGIN = '<!-- gpt-ds:begin -->'
END = '<!-- gpt-ds:end -->'
MISSING = object()


def emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def value_at(data: dict, path: tuple[str, ...]):
    for name in path:
        if not isinstance(data, dict) or name not in data:
            return MISSING
        data = data[name]
    return data


def assign(data: dict, path: tuple[str, ...], value) -> None:
    target = data
    for name in path[:-1]:
        target = target.setdefault(name, {})
        if not isinstance(target, dict):
            raise ValueError('托管配置项的父节点不是 TOML 表；请先核对原配置结构。')
    target[path[-1]] = value


def statements(text: str):
    """Yield logical TOML statements; skip headers inside strings and arrays."""
    start = i = depth = 0
    quote = None
    comment = False
    while i < len(text):
        char = text[i]
        if comment:
            if char != '\n':
                i += 1
                continue
            comment = False
        elif quote:
            if quote[0] == '"' and char == '\\':
                i += 2
                continue
            if text.startswith(quote, i):
                size = len(quote)
                if size == 3:
                    while i + size < len(text) and text[i + size] == quote[0]:
                        size += 1
                i += size
                quote = None
                continue
            i += 1
            continue
        elif char == '#':
            comment = True
            i += 1
            continue
        elif char in ['"', "'"]:
            quote = char * 3 if text.startswith(char * 3, i) else char
            i += len(quote)
            continue
        elif char in '[{':
            depth += 1
        elif char in ']}':
            depth -= 1
        if char == '\n' and depth == 0:
            yield start, i + 1, text[start:i + 1]
            start = i + 1
        i += 1
    if start < len(text):
        yield start, len(text), text[start:]


def table_path(statement: str) -> tuple[str, ...]:
    probe = '__gpt_ds_table_probe__'
    parsed = tomllib.loads(statement + '\n' + probe + '=true\n')
    path = []
    while probe not in parsed:
        if len(parsed) != 1:
            raise ValueError('TOML 表头结构不明确。')
        name, parsed = next(iter(parsed.items()))
        if not isinstance(parsed, dict):
            raise ValueError('托管配置项不支持数组表结构。')
        path.append(name)
    return tuple(path)


def merge_toml(original: bytes, updates: dict[tuple[str, ...], object]) -> bytes:
    """Preserve unrelated text; validate the entire semantic diff after every edit.

    Managed keys use ordinary/quoted scalar assignments and explicit tables.
    Ambiguous inline/dotted encodings fail before any file write.
    """
    text = original.decode('utf-8-sig')
    data = tomllib.loads(text)
    newline = '\r\n' if '\r\n' in text else '\n'
    bom = b'\xef\xbb\xbf' if original.startswith(b'\xef\xbb\xbf') else b''
    for path, value in updates.items():
        previous = value_at(data, path)
        if previous is not MISSING and type(previous) is type(value) and previous == value:
            continue
        expected = copy.deepcopy(data)
        assign(expected, path, value)
        context = ()
        headers = {}
        first_header = None
        location = None
        for start, end, statement in statements(text):
            stripped = statement.strip()
            if stripped.startswith('['):
                # Unrelated array-of-tables do not contain our managed scalars.
                context = ('__array_table__',) if stripped.startswith('[[') else table_path(statement)
                headers[context] = end
                if first_header is None:
                    first_header = start
            elif context == path[:-1]:
                match = re.match(r'''\s*(?:([\w-]+)|"([^"\\]+)"|'([^']+)')\s*=''', statement)
                if match and next(v for v in match.groups() if v is not None) == path[-1]:
                    location = (start, end)
        literal = json.dumps(value, ensure_ascii=False)
        line = f'{path[-1]} = {literal}{newline}'
        if location:
            start, end = location
            candidate = text[:start] + line + text[end:]
        elif previous is not MISSING:
            raise ValueError('托管键采用了内联表或复杂点号写法；请保留原配置，用 TOML 工具整理该键后再预览。')
        elif not path[:-1]:
            index = first_header if first_header is not None else len(text)
            prefix = newline if index and not text[:index].endswith('\n') else ''
            candidate = text[:index] + prefix + line + text[index:]
        elif path[:-1] in headers:
            index = headers[path[:-1]]
            prefix = newline if index and not text[:index].endswith('\n') else ''
            candidate = text[:index] + prefix + line + text[index:]
        else:
            header = '.'.join(json.dumps(p, ensure_ascii=False) for p in path[:-1])
            candidate = text.rstrip('\r\n') + newline * 2 + f'[{header}]' + newline + line
        try:
            result = tomllib.loads(candidate)
        except tomllib.TOMLDecodeError:
            raise ValueError('配置采用了与自动合并不兼容的表结构；原文件保持不变，请由安装 agent 整理托管表后重试。') from None
        if result != expected:
            raise ValueError('配置合并出现超出托管键的语义变化，未写入。')
        text, data = candidate, result
    result = bom + text.encode('utf-8')
    return result if result != original else original


def merge_instructions(original: bytes, prompt: str) -> bytes:
    text = original.decode('utf-8-sig')
    if text.count(BEGIN) != text.count(END) or text.count(BEGIN) > 1:
        raise ValueError('实例指令中的 gpt-ds 标记不完整或重复；未覆盖原有内容。')
    block = BEGIN + '\n' + prompt.strip() + '\n' + END
    if BEGIN in text:
        start, end = text.index(BEGIN), text.index(END) + len(END)
        if start >= end - len(END):
            raise ValueError('实例指令标记顺序错误。')
        text = text[:start] + block + text[end:]
    else:
        text = text + ('\n\n' if text and not text.endswith('\n\n') else '') + block + '\n'
    return text.encode('utf-8')


def discover(home: Path | None = None) -> dict:
    home = (home or Path.home()).resolve()
    candidates: dict[Path, list[str]] = {}

    def add(path: Path, reason: str):
        path = path.expanduser().resolve()
        if (path / 'config.toml').is_file():
            candidates.setdefault(path, []).append(reason)

    if os.environ.get('CODEX_HOME'):
        add(Path(os.environ['CODEX_HOME']), 'CODEX_HOME environment; confirm this is the intended target')
    add(home / '.codex', 'default Codex directory; not selected automatically')
    cockpit = home / '.antigravity_cockpit' / 'instances' / 'codex'
    if cockpit.is_dir():
        for child in sorted(cockpit.iterdir()):
            if child.is_dir():
                add(child, 'Cockpit instance')
    rows = []
    for root, reasons in candidates.items():
        try:
            config = core.configuration(root)
            provider = config.get('model_providers', {}).get(config.get('model_provider'), {})
            rows.append({'instance': str(root), 'reasons': reasons, 'config_model': config.get('model'), 'provider_id': config.get('model_provider'), 'provider_host': urlsplit(provider.get('base_url', '')).hostname})
        except (ValueError, OSError):
            rows.append({'instance': str(root), 'reasons': reasons, 'error': 'Configuration needs inspection; content not printed'})
    return {'supported_target': 'codex', 'candidates': rows, 'writes_performed': False, 'selection': 'Pass --instance explicitly; never install into every candidate'}


def load_settings(root: Path, config_name: str) -> tuple[Path, bytes, dict]:
    if Path(config_name).name != config_name or not config_name.endswith('.toml'):
        raise ValueError('--config-file 必须是实例根目录内的 TOML 文件名。')
    path = core.within(root, config_name)
    original = path.read_bytes()
    selected = tomllib.loads(original.decode('utf-8-sig'))
    if config_name == 'config.toml':
        if selected.get('profile'):
            raise ValueError('检测到配置选择了 profile。请先核对实际启动配置；独立 profile 文件用 --config-file 指定。内嵌 profile 请先由安装 agent 核对覆盖项。')
        return path, original, selected

    def overlay(base: dict, extra: dict):
        result = copy.deepcopy(base)
        for key, value in extra.items():
            result[key] = overlay(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
        return result

    return path, original, overlay(core.configuration(root), selected)


def build_catalog(source: Path, main_model: str, template: str | None, window: int) -> bytes:
    catalog, models = core.read_models(source)
    catalog = copy.deepcopy(catalog)
    models = catalog['models']
    by_slug = {m['slug']: m for m in models}
    if len(by_slug) != len(models):
        raise ValueError('模型目录含重复 slug，未自动猜测。')
    if main_model not in by_slug or main_model == core.WORKER_MODEL:
        raise ValueError('源目录缺少独立的主模型条目；请核对 --main-model 与当前版本原生目录。')
    if core.WORKER_MODEL not in by_slug:
        if not template or template not in by_slug:
            raise ValueError('源目录没有 DeepSeek 条目；请用 --worker-template 明确选择现有格式模板。')
        worker = copy.deepcopy(by_slug[template])
        worker.update({'slug': core.WORKER_MODEL, 'display_name': core.WORKER_MODEL, 'description': 'DeepSeek delegated worker', 'input_modalities': ['text'], 'default_reasoning_level': 'high', 'supported_reasoning_levels': [{'effort': v, 'description': v} for v in ['low', 'medium', 'high']], 'supported_in_api': True, 'visibility': 'list', 'upgrade': None, 'default_service_tier': None, 'service_tiers': []})
        models.append(worker)
        by_slug[core.WORKER_MODEL] = worker
    by_slug[main_model]['multi_agent_version'] = 'v1'
    by_slug[core.WORKER_MODEL].update({'multi_agent_version': 'v1', 'supports_search_tool': False, 'tool_mode': None, 'use_responses_lite': False, 'prefer_websockets': False, 'context_window': window, 'max_context_window': window, 'auto_compact_token_limit': window * 9 // 10, 'effective_context_window_percent': 95})
    return (json.dumps(catalog, ensure_ascii=False, indent=2) + '\n').encode()


def plan(root: Path, args: argparse.Namespace) -> tuple[dict, list]:
    config_path, config_bytes, config = load_settings(root, args.config_file)
    main = args.main_model or config.get('model')
    if not main:
        raise ValueError('主模型不在文件配置中；请传 --main-model，使用 Cockpit 实际启动选择的值。')
    provider_id = config.get('model_provider', 'openai')
    provider = config.get('model_providers', {}).get(provider_id, {})
    host = urlsplit(provider.get('base_url', '')).hostname
    if host != args.expected_provider_host or provider.get('wire_api', 'responses') != 'responses':
        raise ValueError('父 Provider 尚未匹配预期的 Responses 端点；先在 Cockpit 绑定账号。工具保留 Provider，不修改地址或密钥；自建兼容端点用 --expected-provider-host 指定主机名。')
    for key in ['model_context_window', 'model_auto_compact_token_limit']:
        if config.get(key) is not None:
            raise ValueError('存在根级窗口/压缩覆盖，可能同时影响主、子模型。请由安装 agent 保留主模型预算并整理为模型目录值后再安装；工具不擅自删除覆盖项。')
    if args.source:
        source = Path(args.source).expanduser().resolve()
    elif config.get('model_catalog_json'):
        source = Path(config['model_catalog_json']).expanduser()
        source = (source if source.is_absolute() else root / source).resolve()
    else:
        source = root / 'models_cache.json'
    if not source.is_file():
        raise ValueError('未找到当前版本的完整模型目录。先正常启动目标 Codex 一次，或用 --source 指定原生完整目录；不要使用 /v1/models 的 ID 列表。')
    catalog = build_catalog(source, main, args.worker_template, args.worker_context_window)
    updates = {('model_catalog_json',): CATALOG_NAME, ('features', 'multi_agent'): True, ('features', 'multi_agent_v2'): False, ('agents', 'enabled'): True, ('agents', 'default_subagent_model'): core.WORKER_MODEL, ('agents', 'default_subagent_reasoning_effort'): 'high'}
    if value_at(config, ('agents', 'max_concurrent_threads_per_session')) is MISSING and value_at(config, ('agents', 'max_threads')) is MISSING:
        updates[('agents', 'max_concurrent_threads_per_session')] = 4
    if value_at(config, ('agents', 'max_depth')) is MISSING:
        updates[('agents', 'max_depth')] = 2
    config_new = merge_toml(config_bytes, updates)
    role_path = core.within(root, 'agents/deepseek_worker.toml')
    role_old = role_path.read_bytes() if role_path.exists() else None
    if role_old is None:
        role_new = (REPO / 'templates/deepseek_worker.toml').read_bytes()
    else:
        role_data = tomllib.loads(role_old.decode('utf-8-sig'))
        for key in ['model_context_window', 'model_auto_compact_token_limit']:
            if role_data.get(key) is not None:
                raise ValueError('已有角色含额外窗口覆盖，请先由安装 agent 核对，不覆盖自定义设置。')
        fields = {('name',): 'deepseek_worker', ('model',): core.WORKER_MODEL, ('model_reasoning_effort',): 'high'}
        template = tomllib.loads((REPO / 'templates/deepseek_worker.toml').read_text(encoding='utf-8'))
        for key in ['description', 'developer_instructions']:
            if key not in role_data:
                fields[(key,)] = template[key]
        role_new = merge_toml(role_old, fields)
    instruction_name = 'AGENTS.override.md' if (root / 'AGENTS.override.md').is_file() else 'AGENTS.md'
    instructions_path = core.within(root, instruction_name)
    instructions_old = instructions_path.read_bytes() if instructions_path.exists() else None
    instructions_new = merge_instructions(instructions_old or b'', (REPO / 'templates/main-agent-prompt.md').read_text(encoding='utf-8'))
    catalog_path = core.within(root, CATALOG_NAME)
    changes = [(config_path, config_bytes, config_new), (catalog_path, catalog_path.read_bytes() if catalog_path.exists() else None, catalog), (role_path, role_old, role_new), (instructions_path, instructions_old, instructions_new)]
    changes = [(p, old, new) for p, old, new in changes if old != new]
    summary = {'status': 'preview', 'target': 'codex', 'instance': str(root), 'config_file': args.config_file, 'main_model_for_catalog': main, 'provider_id': provider_id, 'provider_host': host, 'source_catalog': str(source), 'worker': {'role': 'deepseek_worker', 'model': core.WORKER_MODEL, 'effort': 'high', 'context_window': args.worker_context_window, 'max_context_window': args.worker_context_window, 'auto_compact_token_limit': args.worker_context_window * 9 // 10}, 'files_to_change': [{'path': str(p.relative_to(root)), 'existed': old is not None, 'before_sha256': core.digest(old) if old is not None else None, 'after_sha256': core.digest(new)} for p, old, new in changes], 'preserved': ['main model selection and window', 'Provider accounts and authentication', 'MCP, permissions, plugins', 'other instances', 'conversation history'], 'runtime_verified': False, 'automatic_restart': False, 'next': 'After applying, manually restart this instance and create a NEW deepseek_worker in the original or a new parent chat. Run the native self-check.'}
    return summary, changes


def thread_evidence(root: Path, thread: str, database: str | None) -> dict:
    path = core.resolve_rollout(root, thread, database)
    result = {'turn': {}, 'meta': {}, 'reply_exact': False, 'tool_calls': 0, 'window': None, 'provider': None, 'completed': False}
    with path.open('rb') as stream:
        for line in stream:
            record = json.loads(line)
            payload = record.get('payload', {})
            if record.get('type') == 'session_meta':
                result['meta'] = payload
                result['provider'] = payload.get('model_provider')
            elif record.get('type') == 'turn_context':
                result['turn'] = {key: payload.get(key) for key in ['model', 'effort', 'multi_agent_version']}
                result.update({'completed': False, 'reply_exact': False, 'window': None})
            elif record.get('type') == 'event_msg' and payload.get('type') == 'token_count':
                result['window'] = (payload.get('info') or {}).get('model_context_window', result['window'])
            elif record.get('type') == 'event_msg' and payload.get('type') == 'task_complete':
                result['completed'] = True
            elif record.get('type') == 'response_item':
                kind = payload.get('type', '')
                if kind.endswith('_call'):
                    result['tool_calls'] += 1
                if kind == 'message' and payload.get('role') == 'assistant':
                    text = ''.join(c.get('text', '') for c in payload.get('content', []) if isinstance(c, dict))
                    result['reply_exact'] = text == 'RESTART_OK'
    database_path = core.database_path(root, database)
    if database_path:
        with closing(sqlite3.connect(database_path.as_uri() + '?mode=ro', uri=True)) as connection:
            columns = {row[1] for row in connection.execute('PRAGMA table_info(threads)')}
            if 'model_provider' in columns:
                row = connection.execute('SELECT model_provider FROM threads WHERE id=?', (thread,)).fetchone()
                if row:
                    result['provider'] = row[0]
    return result


def parent_ids(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == 'parent_thread_id':
                yield child
            else:
                yield from parent_ids(child)
    elif isinstance(value, list):
        for child in value:
            yield from parent_ids(child)


def verify(root: Path, args: argparse.Namespace) -> bool:
    parent = thread_evidence(root, args.parent_thread, args.database)
    worker = thread_evidence(root, args.thread, args.database)
    _, _, config = load_settings(root, args.config_file)
    main = args.main_model or config.get('model')
    provider_id = config.get('model_provider', 'openai')
    provider = config.get('model_providers', {}).get(provider_id, {})
    source = worker['meta'].get('source')
    native_source = isinstance(source, dict) and isinstance(source.get('subagent'), dict)
    checks = {'parent_model': parent['turn'].get('model') == main, 'parent_v1': parent['turn'].get('multi_agent_version') == 'v1', 'native_parent_link': native_source and args.parent_thread in list(parent_ids(source)), 'worker_model': worker['turn'].get('model') == core.WORKER_MODEL, 'worker_high': worker['turn'].get('effort') == 'high', 'worker_v1': worker['turn'].get('multi_agent_version') == 'v1', 'same_provider': bool(parent['provider']) and parent['provider'] == worker['provider'], 'provider_matches_config': parent['provider'] == provider_id, 'provider_host': urlsplit(provider.get('base_url', '')).hostname == args.expected_provider_host, 'reply_exact': worker['reply_exact'], 'worker_completed': worker['completed'], 'no_worker_tools': worker['tool_calls'] == 0, 'one_million_window_loaded': isinstance(worker['window'], int) and 900_000 <= worker['window'] <= 1_000_000}
    passed = all(checks.values())
    emit({'status': 'native_verified' if passed else 'native_verification_incomplete', 'checks': checks, 'worker_effective_context_window': worker['window'], 'provider_id': worker['provider'], 'note': 'Read-only native rollout evidence. No HTTP request, separate CLI test, or model call was made.'})
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance', help='Explicit target CODEX_HOME; required except for discover')
    parser.add_argument('--config-file', default='config.toml', help='Selected config filename in the instance root; preserve launcher/profile selection')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('discover', help='Read-only candidate discovery, no automatic selection')
    for name in ['plan', 'install', 'status']:
        command = commands.add_parser(name)
        command.add_argument('--source', help='Complete native model catalog, not /v1/models')
        command.add_argument('--main-model', help='Actual launcher/session selection; does not change config model')
        command.add_argument('--worker-template', help='Explicit native slug to copy only when DeepSeek is absent')
        command.add_argument('--worker-context-window', type=int, default=1_000_000)
        command.add_argument('--expected-provider-host', default='cpa.gojia.cloud')
        if name == 'install':
            command.add_argument('--apply', action='store_true')
            command.add_argument('--offline-confirmed', action='store_true')
    check = commands.add_parser('verify', help='Read-only native parent/child proof, no model calls')
    check.add_argument('--parent-thread', required=True)
    check.add_argument('--thread', required=True, help='NEW native self-check child UUID')
    check.add_argument('--main-model')
    check.add_argument('--expected-provider-host', default='cpa.gojia.cloud')
    check.add_argument('--database')
    for name in ['migrate', 'restore']:
        command = commands.add_parser(name)
        command.add_argument('--apply', action='store_true')
        command.add_argument('--offline-confirmed', action='store_true')
        if name == 'migrate':
            command.add_argument('--thread', action='append', required=True)
            command.add_argument('--database')
        else:
            command.add_argument('--backup', required=True)
    args = parser.parse_args()
    if args.command == 'discover':
        emit(discover())
        return 0
    if not args.instance:
        parser.error('--instance is required; discover never chooses a write target')
    root = Path(args.instance).expanduser().resolve()
    if not (root / 'config.toml').is_file():
        raise ValueError('目标目录没有 config.toml，请核对 CODEX_HOME。')
    if args.command in ['plan', 'install', 'status']:
        if args.worker_context_window < 16000:
            raise ValueError('子模型窗口至少为 16000。')
        summary, changes = plan(root, args)
        if args.command == 'status':
            summary['status'] = 'configured_on_disk' if not changes else 'configuration_differs'
        emit(summary)
        if args.command == 'install' and args.apply:
            core.apply_changes(root, changes, args.offline_confirmed)
            emit({'status': 'configured_pending_manual_restart', 'instance': str(root), 'runtime_verified': False, 'restart_performed': False})
    elif args.command == 'verify':
        return 0 if verify(root, args) else 2
    elif args.command == 'migrate':
        core.migrate(root, args)
    elif args.command == 'restore':
        core.restore(root, args)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except tomllib.TOMLDecodeError:
        raise SystemExit('TOML 解析失败；请核对本机语法。未打印配置或凭据。') from None
    except json.JSONDecodeError:
        raise SystemExit('JSON/会话格式解析失败；保留原文件并核对版本。') from None
    except (ValueError, KeyError, OSError, sqlite3.Error) as error:
        raise SystemExit(str(error)) from None
