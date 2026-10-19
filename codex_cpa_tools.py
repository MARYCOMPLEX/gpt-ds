#!/usr/bin/env python3
"""CPA / DeepSeek V1 local tools. Python 3.11+. Explicit instance, dry run by default.

No network requests, credential output, process termination, or automatic restarts.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import closing
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import uuid

VERSION_PATTERN = re.compile(rb'"multi_agent_version"\s*:\s*"v2"')
MAIN_MODEL = 'gpt-6.1-sol'
WORKER_MODEL = 'deepseek-v4.1-flash'
WORKER_CONTEXT_WINDOW = 1_000_000


def within(root: Path, value: str | Path) -> Path:
    candidate = Path(value).expanduser()
    candidate = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f'写入或会话路径超出指定实例：{candidate}')
    return candidate


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, path.stat().st_mode & 0o777 if path.exists() else 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def runtime_pids(root: Path) -> list[int] | None:
    """Read environments in memory; never print command lines or env."""
    if sys.platform == 'win32':
        return None
    try:
        rows = subprocess.check_output(['ps', '-axo', 'pid=,comm='], text=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    result = []
    for row in rows.splitlines():
        parts = row.strip().split(None, 1)
        if len(parts) != 2 or Path(parts[1]).name not in ['codex', 'codex.exe']:
            continue
        pid = int(parts[0])
        if sys.platform.startswith('linux'):
            try:
                environment = dict(item.split(b'=', 1) for item in Path(f'/proc/{pid}/environ').read_bytes().split(b'\0') if b'=' in item)
                value = environment.get(b'CODEX_HOME', b'').decode(errors='replace')
            except (OSError, ValueError):
                return None
        else:
            try:
                command = subprocess.check_output(['ps', 'eww', '-p', str(pid), '-o', 'command='], text=True, stderr=subprocess.DEVNULL)
            except subprocess.CalledProcessError:
                continue
            match = re.search(r'(?:^|\s)CODEX_HOME=(.*?)(?=\s+[A-Za-z_][A-Za-z_0-9]*=|$)', command)
            value = match[1] if match else ''
        if value and Path(value).expanduser().resolve() == root:
            result.append(pid)
        elif not value and root == (Path.home() / '.codex').resolve():
            result.append(pid)
    return result


def require_offline(root: Path, confirmed: bool) -> None:
    pids = runtime_pids(root)
    if pids:
        raise ValueError(f'指定实例仍有 Codex 进程 {pids}；请手动完全退出此实例后再应用。')
    if pids is None and not confirmed:
        raise ValueError('此系统未自动确认进程状态。手动退出目标实例后，添加 --offline-confirmed 再执行。')


def configuration(root: Path) -> dict:
    return tomllib.loads((root / 'config.toml').read_text(encoding='utf-8-sig'))


def database_path(root: Path, override: str | None) -> Path | None:
    if override:
        path = within(root, override)
        if not path.is_file():
            raise ValueError('指定的 SQLite 文件不存在。')
        return path
    candidates = list(root.glob('state_*.sqlite'))
    if len(candidates) > 1:
        raise ValueError('实例内有多份 state 数据库，请用 --database 明确选择正在使用的数据库。')
    return candidates[0] if candidates else None


def resolve_rollout(root: Path, thread: str, database: str | None = None) -> Path:
    thread = str(uuid.UUID(thread))
    indexed = database_path(root, database)
    if indexed:
        with closing(sqlite3.connect(indexed.as_uri() + '?mode=ro', uri=True)) as connection:
            row = connection.execute('SELECT rollout_path FROM threads WHERE id = ?', (thread,)).fetchone()
        if row:
            selected = within(root, row[0])
            if not selected.is_file():
                raise ValueError('会话索引指向的当前文件不存在。')
            return validate_rollout(selected, thread)
    candidates = []
    for path in (root / 'sessions').rglob(f'*{thread}*.jsonl'):
        path = within(root, path)
        with path.open('rb') as stream:
            metadata = json.loads(stream.readline())
        if metadata.get('type') == 'session_meta' and metadata.get('payload', {}).get('id') == thread:
            candidates.append(path)
    if len(candidates) != 1:
        raise ValueError(f'缺少有效会话索引，找到 {len(candidates)} 份匹配快照；未自动猜测。')
    return validate_rollout(candidates[0], thread)


def validate_rollout(path: Path, thread: str) -> Path:
    with path.open('rb') as stream:
        metadata = json.loads(stream.readline())
    if metadata.get('type') != 'session_meta' or metadata.get('payload', {}).get('id') != thread:
        raise ValueError('当前会话文件头与所选对话 ID 不一致。')
    return path


def migrated_bytes(original: bytes) -> tuple[bytes, int]:
    """Change known metadata only; preserve all message bytes and file length."""
    output, count = [], 0
    for line in original.splitlines(keepends=True):
        record = json.loads(line)
        expected = copy.deepcopy(record)
        payload = expected.get('payload', {})
        kind = expected.get('type')
        owner = payload if kind in ['session_meta', 'turn_context'] else payload.get('resume_metadata', {}) if kind == 'compacted' else {}
        if isinstance(owner, dict) and owner.get('multi_agent_version') == 'v2':
            owner['multi_agent_version'] = 'v1'
            if len(VERSION_PATTERN.findall(line)) != 1:
                raise ValueError('遇到额外协议字段；当前脚本未覆盖此存储结构，原文件保持不变。')
            line = VERSION_PATTERN.sub(lambda match: match[0][:-4] + b'"v1"', line)
            if json.loads(line) != expected:
                raise ValueError('迁移发生了超出协议元数据的变化。')
            count += 1
        output.append(line)
    result = b''.join(output)
    if len(result) != len(original):
        raise ValueError('迁移意外改变了会话文件字节长度。')
    return result, count


def apply_changes(root: Path, changes: list[tuple[Path, bytes | None, bytes]], confirmed: bool) -> None:
    changes = [(within(root, p), old, new) for p, old, new in changes if old != new]
    if not changes:
        print('内容已符合目标，无需修改。')
        return
    require_offline(root, confirmed)
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    backup = within(root, root / 'repair_backups' / ('cpa-guide-' + stamp))
    backup.mkdir(parents=True, mode=0o700)
    manifest = {'schema': 1, 'files': []}
    for path, old, new in changes:
        relative = path.relative_to(root).as_posix()
        if old is not None:
            saved = backup / 'original' / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(old)
            os.chmod(saved, 0o600)
        manifest['files'].append({'path': relative, 'existed': old is not None, 'before_sha256': digest(old) if old is not None else None, 'after_sha256': digest(new)})
    (backup / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    applied = []
    try:
        require_offline(root, confirmed)
        for path, old, new in changes:
            current = path.read_bytes() if path.exists() else None
            if current != old:
                raise ValueError('操作期间文件发生变化，请重新检查。')
            private_write(path, new)
            applied.append((path, old))
    except BaseException:
        for path, old in reversed(applied):
            if old is None:
                path.unlink(missing_ok=True)
            else:
                private_write(path, old)
        raise
    print('已应用。备份目录：' + str(backup))
    print('未自动启动、终止或重启任何实例。')


def read_models(path: Path) -> tuple[dict, list[dict]]:
    catalog = json.loads(path.read_text(encoding='utf-8-sig'))
    if isinstance(catalog, list):
        catalog = {'models': catalog}
    models = catalog.get('models')
    if not isinstance(models, list) or not models or not all(isinstance(m, dict) and 'slug' in m and 'base_instructions' in m for m in models):
        raise ValueError('需要 Codex 原生完整模型目录（含 slug / base_instructions），不是 /v1/models 的模型 ID 列表。')
    return catalog, models


def prepare_catalog(root: Path, args: argparse.Namespace) -> None:
    source = Path(args.source).expanduser().resolve()
    _, models = read_models(source)
    models = copy.deepcopy(models)
    by_slug = {m['slug']: m for m in models}
    if args.main_model not in by_slug:
        raise ValueError('源目录缺少主模型；请使用目标设备同版本的完整模型目录。')
    if args.worker_model not in by_slug:
        if not args.worker_template or args.worker_template not in by_slug:
            raise ValueError('源目录缺少子模型；请用 --worker-template 明确选择格式模板（例如 gpt-5.5）。')
        worker = copy.deepcopy(by_slug[args.worker_template])
        worker.update({
            'slug': args.worker_model, 'display_name': args.worker_model,
            'description': 'CPA DeepSeek worker; locally declared capabilities.',
            'default_reasoning_level': 'high',
            'supported_reasoning_levels': [{'effort': v, 'description': v} for v in ['low', 'medium', 'high']],
            'input_modalities': ['text'], 'tool_mode': None,
            'use_responses_lite': False, 'prefer_websockets': False,
            'default_service_tier': None, 'service_tiers': [], 'upgrade': None,
            'supported_in_api': True, 'visibility': 'list',
        })
        models.append(worker)
        by_slug[args.worker_model] = worker
        print('已从明确选择的目录条目复制子模型格式；能力字段需以目标上游实测为准。')
    by_slug[args.main_model]['multi_agent_version'] = 'v1'
    worker = by_slug[args.worker_model]
    worker.update({
        'multi_agent_version': 'v1', 'supports_search_tool': False,
        'context_window': args.worker_context_window,
        'max_context_window': args.worker_context_window,
        'auto_compact_token_limit': args.worker_context_window * 9 // 10,
    })
    result = (json.dumps({'models': models}, ensure_ascii=False, indent=2) + '\n').encode()
    output = within(root, args.output)
    if output == source:
        raise ValueError('请写入独立目录文件，保留原始模型目录。')
    old = output.read_bytes() if output.exists() else None
    print(f'目录：{source} → {output}；主模型 {args.main_model}；子模型 {args.worker_model}；两者 V1。')
    print(f'子模型窗口：{worker["context_window"]}；最大窗口：{worker["max_context_window"]}；自动压缩阈值：{worker["auto_compact_token_limit"]}。')
    if args.apply:
        apply_changes(root, [(output, old, result)], args.offline_confirmed)
    else:
        print('仅预览。添加 --apply 后生成文件；随后手动合并 config.merge.toml。')


def inspect(root: Path, args: argparse.Namespace) -> None:
    config = configuration(root)
    selected = config.get('model_provider')
    provider = config.get('model_providers', {}).get(selected, {})
    from urllib.parse import urlsplit, urlunsplit
    url = urlsplit(provider.get('base_url', ''))
    address = urlunsplit((url.scheme, url.netloc.rsplit('@', 1)[-1], '', '', ''))
    summary = {'instance': str(root), 'model': config.get('model'), 'model_provider_id': selected, 'provider_url': address, 'wire_api': provider.get('wire_api'), 'features': {k: config.get('features', {}).get(k) for k in ['multi_agent', 'multi_agent_v2']}, 'default_subagent_model': config.get('agents', {}).get('default_subagent_model'), 'default_subagent_reasoning_effort': config.get('agents', {}).get('default_subagent_reasoning_effort')}
    catalog_value = config.get('model_catalog_json')
    if catalog_value:
        path = Path(catalog_value).expanduser()
        path = path if path.is_absolute() else root / path
        _, models = read_models(path)
        summary['catalog'] = {'path': str(path), 'models': [{k: m.get(k) for k in ['slug', 'multi_agent_version', 'supports_search_tool', 'context_window', 'max_context_window', 'auto_compact_token_limit', 'effective_context_window_percent']} for m in models if m['slug'] in [config.get('model'), WORKER_MODEL]]}
    role = root / 'agents' / 'deepseek_worker.toml'
    if role.exists():
        data = tomllib.loads(role.read_text(encoding='utf-8-sig'))
        summary['worker_role'] = {k: data.get(k) for k in ['name', 'model', 'model_reasoning_effort']}
    if args.thread:
        path = resolve_rollout(root, args.thread, args.database)
        last, empty_payloads = None, 0
        with path.open('rb') as stream:
            for line in stream:
                record = json.loads(line)
                payload = record.get('payload', {})
                if record.get('type') == 'turn_context':
                    last = {k: payload.get(k) for k in ['model', 'effort', 'multi_agent_version']}
                if record.get('type') == 'response_item' and payload.get('type') == 'agent_message':
                    blocks = payload.get('content', [])
                    text = ''.join(b.get('text', '') for b in blocks if isinstance(b, dict))
                    if text.rstrip().endswith('Payload:') and any(b.get('type') == 'encrypted_content' for b in blocks if isinstance(b, dict)):
                        empty_payloads += 1
        summary['thread'] = {'id': args.thread, 'current_rollout': str(path), 'latest_turn': last, 'historical_empty_payload_envelopes': empty_payloads}
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def migrate(root: Path, args: argparse.Namespace) -> None:
    changes = []
    for thread in dict.fromkeys(args.thread):
        path = resolve_rollout(root, thread, args.database)
        old = path.read_bytes()
        new, count = migrated_bytes(old)
        print(f'{thread}：{count} 处 V2→V1；当前文件 {path.name}；消息字节与 ID 保留。')
        changes.append((path, old, new))
    if args.apply:
        apply_changes(root, changes, args.offline_confirmed)
    else:
        print('仅预览。手动退出指定实例后添加 --apply；本次不修改配置、数据库或其他对话。')


def restore(root: Path, args: argparse.Namespace) -> None:
    backup = within(root, args.backup)
    manifest = json.loads((backup / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema') != 1:
        raise ValueError('备份格式与本脚本不一致。')
    items = []
    for item in manifest['files']:
        path = within(root, item['path'])
        current = path.read_bytes() if path.exists() else None
        if current is None or digest(current) != item['after_sha256']:
            raise ValueError('目标文件在应用后又发生了变化；未覆盖后续记录，请先保留新历史并人工核对。')
        saved = within(backup, Path('original') / item['path'])
        original = saved.read_bytes() if item['existed'] else None
        if original is not None and digest(original) != item['before_sha256']:
            raise ValueError('备份内容校验失败。')
        items.append((path, original, current))
        print('准备恢复：' + str(path))
    if not args.apply:
        print('仅预览；添加 --apply 执行恢复。')
        return
    require_offline(root, args.offline_confirmed)
    for path, original, current in items:
        saved = backup / 'before-restore' / path.relative_to(root)
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_bytes(current)
        os.chmod(saved, 0o600)
    restored = []
    try:
        for path, original, current in items:
            if path.read_bytes() != current:
                raise ValueError('恢复期间文件发生变化，请重新检查。')
            if original is None:
                path.unlink()
            else:
                private_write(path, original)
            restored.append((path, current))
    except BaseException:
        for path, current in reversed(restored):
            private_write(path, current)
        raise
    print('已恢复；手动编辑的配置请使用单独保留的备份。未自动重启。')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance', required=True, help='目标 Cockpit 实例的独立 CODEX_HOME 目录')
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('inspect', help='不含密钥的配置与指定对话检查')
    check.add_argument('--thread')
    check.add_argument('--database', help='实例内数据库相对路径；多份数据库时必填')
    catalog = commands.add_parser('catalog', help='从目标设备完整目录生成独立 V1 目录')
    catalog.add_argument('--source', required=True)
    catalog.add_argument('--output', default='models.cpa-deepseek-v1.json')
    catalog.add_argument('--main-model', default=MAIN_MODEL)
    catalog.add_argument('--worker-model', default=WORKER_MODEL)
    catalog.add_argument('--worker-template')
    catalog.add_argument('--worker-context-window', type=int, default=WORKER_CONTEXT_WINDOW, help='子模型窗口（默认 1000000）；新建和已有条目均更新，自动压缩阈值为窗口的 90%%')
    migration = commands.add_parser('migrate', help='只迁移明确列出的旧对话')
    migration.add_argument('--thread', action='append', required=True)
    migration.add_argument('--database')
    rollback = commands.add_parser('restore', help='恢复本工具备份；有后续变化时停止')
    rollback.add_argument('--backup', required=True)
    for command in [catalog, migration, rollback]:
        command.add_argument('--apply', action='store_true')
        command.add_argument('--offline-confirmed', action='store_true', help='自动进程识别不可用时，确认已手动退出指定实例')
    args = parser.parse_args()
    root = Path(args.instance).expanduser().resolve()
    if not (root / 'config.toml').is_file():
        raise ValueError('所选目录没有 config.toml；请先核对实例目录。')
    if args.command == 'catalog' and args.worker_context_window < 16000:
        raise ValueError('请给出至少 16000 的目录上下文窗口。')
    {'inspect': inspect, 'catalog': prepare_catalog, 'migrate': migrate, 'restore': restore}[args.command](root, args)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, sqlite3.Error) as error:
        if isinstance(error, tomllib.TOMLDecodeError):
            raise SystemExit('TOML 解析失败，请检查本机配置语法；未打印配置内容。') from None
        if isinstance(error, json.JSONDecodeError):
            raise SystemExit('JSON/会话格式解析失败，请保留原文件并核对版本。') from None
        raise SystemExit(str(error)) from None
