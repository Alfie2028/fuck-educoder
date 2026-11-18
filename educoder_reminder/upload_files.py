"""向头歌虚拟环境 /data/workspace/userfiles 上传本地文件（基于 upload_file 接口）。

接口（已实测，返回 {"status":0,"message":"success"}）：
    POST /api/tasks/{关卡gid}/upload_file
        ?debug=admin&shixun_environment_id={env_id}&tab_type={tab_type}&is_jupyter=false
    multipart 字段：file
    Headers：Pc-Authorization=<token>; Cookie=<_educoder_session=...>

与 update_file.json（写"学员文件"路径，受限制）不同：本接口可上传任意文件到
虚拟环境 /data/workspace/userfiles（单文件 ≤200MB），不受学员文件路径限制。

用法（Windows 本地，用 venv python）：
    .venv\\Scripts\\python.exe -m educoder_reminder.upload_files --task obhnxg692tau --file C:/本地/a.txt --file C:/本地/b.md
    # 或一次性多个
    .venv\\Scripts\\python.exe -m educoder_reminder.upload_files --task obhnxg692tau --files a.txt,b.md
"""
import argparse
import json
import mimetypes
import re
from pathlib import Path

from dotenv import dotenv_values
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


from .auth import signing_headers
from .client import load_settings


def main():
    ap = argparse.ArgumentParser(description='上传本地文件到头歌虚拟环境 userfiles')
    ap.add_argument('--task', required=True, help='关卡 gid')
    ap.add_argument('--file', action='append', default=[], help='本地文件路径，可多次')
    ap.add_argument('--files', help='逗号分隔的多个本地文件路径')
    ap.add_argument('--env-id', help='可选：显式指定 shixun_environment_id（默认自动取）')
    ap.add_argument('--tab-type', type=int, default=3, help='tab_type，默认 3')
    ap.add_argument('--dry-run', action='store_true', help='只打印计划不上传')
    args = ap.parse_args()

    paths = list(args.file)
    if args.files:
        paths += [p.strip() for p in args.files.split(',') if p.strip()]
    paths = [Path(p) for p in paths]
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise SystemExit(f'本地文件不存在：{[str(p) for p in missing]}')
    if not paths:
        raise SystemExit('未提供 --file。')

    token, cookie = None, None
    username, headers = load_settings()
    # multipart 上传不能带 JSON Content-Type，context 级就去掉（GET/POST 都无 JSON body）
    ctx_headers = {k: v for k, v in headers.items() if k != 'Content-Type'}
    with sync_playwright() as p:
        req = p.request.new_context(extra_http_headers={**ctx_headers, 'User-Agent': 'Mozilla/5.0'})
        get_headers = {**ctx_headers, **signing_headers('GET'), 'User-Agent': 'Mozilla/5.0'}
        detail = req.get(f'https://data.educoder.net/api/tasks/{args.task}.json?zzud={username}',
                         headers=get_headers)
        if detail.status != 200:
            raise SystemExit(f'读取任务详情失败 HTTP {detail.status}')
        detail = detail.json()
        env_id = args.env_id
        if not env_id:
            envs = (detail.get('shixun_environments') or [])
            if envs:
                env_id = envs[0].get('shixun_environment_id')
        if not env_id:
            raise SystemExit('无法确定 shixun_environment_id，请用 --env-id 显式指定。')
        url = ('https://data.educoder.net/api/tasks/%(gid)s/upload_file'
               '?debug=admin&shixun_environment_id=%(env)s&tab_type=%(tab)s&is_jupyter=false'
               % {'gid': args.task, 'env': env_id, 'tab': args.tab_type})
        print(f'目标虚拟环境 shixun_environment_id={env_id}，待上传 {len(paths)} 个文件 → /data/workspace/userfiles')
        for i, fp in enumerate(paths, 1):
            if args.dry_run:
                print(f'  [{i}/{len(paths)}] {fp.name}: (dry-run) 将上传 {fp.stat().st_size} 字节')
                continue
            mime = mimetypes.guess_type(str(fp))[0] or 'application/octet-stream'
            buf = fp.read_bytes()
            # multipart 上传必须去掉 JSON Content-Type，否则会覆盖 multipart 头导致 500
            post_headers = {k: v for k, v in headers.items() if k != 'Content-Type'}
            r = req.post(url, headers=post_headers, multipart={
                'file': {'name': fp.name, 'mimeType': mime, 'buffer': buf}})
            try:
                j = r.json()
                ok = j.get('status') == 0
                print(f'  [{i}/{len(paths)}] {fp.name}: '
                      f'{"OK" if ok else "FAIL"} → HTTP {r.status} {json.dumps(j, ensure_ascii=False)[:120]}')
            except Exception:
                print(f'  [{i}/{len(paths)}] {fp.name}: HTTP {r.status} body={r.text()[:150]!r}')
        req.dispose()


if __name__ == '__main__':
    main()
