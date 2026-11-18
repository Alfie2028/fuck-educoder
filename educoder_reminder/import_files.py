"""向头歌虚拟环境(myshixun)自动化导入/写入多个文件（基于 update_file.json 循环）。

验证结论（已实测）：
- 接口：POST /api/myshixuns/{myshixun.identifier}/update_file.json
- 同一虚拟环境可循环写入多个学员文件，全部成功；
- 限制：path 必须是该虚拟环境的"学员代码文件"(challenge.path 列出的路径)，
  任意路径会被平台拒绝(非学员代码文件，不允许修改)；
- evaluate=0 仅导入文件、不触发评测。

用法（Windows 本地，用 venv python）：
    # 方式一：指定一个关卡 gid，用它解析虚拟环境上下文，导入 spec 里的多个文件
    .\.venv\Scripts\python.exe -m educoder_reminder.import_files --task 5bky6n2cgom4 --spec spec.json

    # 方式二：指定课堂+实验，自动解析第一个关卡上下文
    .\.venv\Scripts\python.exe -m educoder_reminder.import_files --classroom 2i6ng7vp --experiment 编程风格 --spec spec.json

    # dry-run：只打印将导入哪些文件，不真实写入
    .\.venv\Scripts\python.exe -m educoder_reminder.import_files --task 5bky6n2cgom4 --spec spec.json --dry-run

spec.json 支持三种形态：
    1) [{"path": "src/step1/source.java", "content": "..."}, ...]
    2) {"src/step1/source.java": "内容", ...}
    3) [{"path": "src/step1/source.java", "file": "D:/本地文件.txt"}, ...]   # content 从本地文件读取
"""
import argparse
import asyncio
import json
from pathlib import Path

from .client import open_client
from .errors import CollectionError
from .submit import fetch_homework_rows, match_homework, resolve_entry, walk_prev_to_head

ROOT = Path(__file__).resolve().parents[1]


def load_spec(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    items = []
    if isinstance(data, dict):
        for p, c in data.items():
            items.append({'path': p, 'content': c if isinstance(c, str) else None})
    elif isinstance(data, list):
        for row in data:
            if not isinstance(row, dict) or not row.get('path'):
                continue
            item = {'path': row['path']}
            if row.get('content'):
                item['content'] = row['content']
            elif row.get('file'):
                item['content'] = Path(row['file']).read_text(encoding='utf-8')
            items.append(item)
    return items


async def task_context(client, gid):
    d = await client.get(f'/api/tasks/{gid}.json', {}, strict_status=False)
    ms = d.get('myshixun') or {}
    ch = d.get('challenge') or {}
    if not ms.get('identifier'):
        raise CollectionError(f'任务 {gid} 无 myshixun.identifier')
    return {
        'ident': ms['identifier'],
        'game_id': (d.get('game') or {}).get('id'),
        'challenge_id': ch.get('id'),
        'user_id': (d.get('user') or {}).get('user_id'),
        'hw_id': d.get('homework_common_id'),
    }


async def resolve_gid(client, classroom, experiment):
    rows = await fetch_homework_rows(client, classroom)
    hw = match_homework(experiment, rows)
    if not hw:
        raise CollectionError(f'课堂 {classroom} 中未找到实验「{experiment}」')
    entry = await resolve_entry(client, hw.get('shixun_identifier'), hw.get('homework_id'))
    ordered, _ = await walk_prev_to_head(client, entry)
    if not ordered:
        raise CollectionError(f'实验「{experiment}」关卡链为空')
    return ordered[0]


async def run(task, classroom, experiment, spec, dry_run):
    files = load_spec(spec)
    if not files:
        print('✗ spec 中没有可导入的文件。')
        return
    async with open_client() as client:
        gid = task
        if not gid:
            gid = await resolve_gid(client, classroom, experiment)
        ctx = await task_context(client, gid)
        print(f'虚拟环境 myshixun={ctx["ident"]}（来自任务 {gid}），待导入 {len(files)} 个文件')
        for i, f in enumerate(files, 1):
            content = f.get('content')
            if content is None:
                print(f'  [{i}/{len(files)}] {f["path"]}: 无内容，跳过')
                continue
            if dry_run:
                print(f'  [{i}/{len(files)}] {f["path"]}: (dry-run) 将写入 {len(content)} 字符')
                continue
            extras = {
                'challenge_id': ctx['challenge_id'], 'homework_common_id': ctx['hw_id'],
                'currentUserId': ctx['user_id'], 'exercise_id': '', 'question_id': '',
                'subject_id': '', 'competition_entry_id': '',
            }
            resp = await client.post(
                f'/api/myshixuns/{ctx["ident"]}/update_file.json', {}, {
                    'path': f['path'], 'evaluate': 0, 'content': content,
                    'game_id': ctx['game_id'], 'tab_type': 1, 'homework_common_id': ctx['hw_id'],
                    'extras': extras,
                })
            status = resp.get('status')
            c = resp.get('content') or {}
            ok = status in (None, 0, 200) and bool(c.get('commitID'))
            print(f'  [{i}/{len(files)}] {f["path"]}: '
                  f'{"OK commit=" + str(c.get("commitID",""))[:12] if ok else "FAIL " + str(resp)[:200]}')


def main():
    parser = argparse.ArgumentParser(description='向头歌虚拟环境自动化导入多个文件')
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument('--task', help='关卡 gid（用它解析虚拟环境上下文）')
    g.add_argument('--classroom', help='课堂 code（配合 --experiment）')
    parser.add_argument('--experiment', help='实验名（配合 --classroom）')
    parser.add_argument('--spec', required=True, help='spec.json 路径')
    parser.add_argument('--dry-run', action='store_true', help='只打印计划，不真实写入')
    args = parser.parse_args()
    if not args.task and not args.experiment:
        raise SystemExit('使用 --task 或同时提供 --classroom 与 --experiment。')
    try:
        asyncio.run(run(args.task, args.classroom, args.experiment, args.spec, args.dry_run))
    except CollectionError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == '__main__':
    main()
