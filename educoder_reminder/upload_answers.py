"""从答案根目录扫描 .mdj 文件，按 answerN 顺序全部上传到指定实验的虚拟环境 userfiles。

结构约定（用户导出格式）：
    答案根目录/
        answer1/step1.mdj
        answer2/step2.mdj
        ...
    从中匹配所有 .mdj，按 answer 序号（或 step 序号）排序后全部上传。

目标实验：一大关内小关共享虚拟环境 → 上传到该实验的 userfiles 一次即可。
支持按账号注入 token/cookie/username（多账号驱动），见 upload_batch。

用法（Windows 本地）：
    .venv\\Scripts\\python.exe -m educoder_reminder.upload_answers \
        --source D:/答案根目录 --classroom 2i6ng7vp --experiment 面向对象分析之用例图
    # 指定账号身份
    ... --token <t> --cookie "_educoder_session=...; autologin_trustie=..." --username <u>
    # 或指定单个关卡 gid
    ... --task ypqr829xolkf
    # 只打印计划
    ... --dry-run
"""
import argparse
import asyncio
import re
from pathlib import Path

from playwright.async_api import async_playwright

from .client import load_settings, open_client
from .errors import CollectionError
from .import_files import resolve_gid


def scan_mdj(source, extensions=('mdj', 'txt')):
    """扫描答案文件，支持 .mdj/.txt 等扩展名，按 answerN/stepN 序号排序。"""
    items = []
    root = Path(source)
    exts = tuple(e.lower().lstrip('.') for e in extensions)

    def _is_ans_file(p):
        return p.is_file() and p.suffix.lower().lstrip('.') in exts

    def _collect(cands):
        out = []
        for f in cands:
            if not _is_ans_file(f):
                continue
            key = None
            m = re.search(r'answer(\d+)', f.parent.name)
            if not m:
                m = re.search(r'step(\d+)', f.stem)
            if m:
                key = int(m.group(1))
            out.append((key, f))
        out.sort(key=lambda x: (x[0] is None, x[0] or 0, str(x[1])))
        return [f for _, f in out]

    # 优先限定 answerN 子文件夹；若不存在 answer 目录，则退化为全目录扫描（只保留有序号的 stepN）
    answer_dirs = [d for d in root.iterdir() if d.is_dir() and re.fullmatch(r'answer\d+', d.name)]
    if answer_dirs:
        cands = [f for d in answer_dirs for pat in exts for f in d.rglob(f'*.{pat}')]
        return _collect(cands)
    cands = [f for pat in exts for f in root.rglob(f'*.{pat}')
             if re.search(r'answer(\d+)', f.parent.name) or re.search(r'step(\d+)', f.stem)]
    return _collect(cands)


async def resolve_env(client, gid):
    d = await client.get(f'/api/tasks/{gid}.json', {}, strict_status=False)
    envs = d.get('shixun_environments') or []
    if not envs:
        raise CollectionError(f'任务 {gid} 无虚拟环境(shixun_environments)。')
    e = envs[0]
    return {
        'env_id': e.get('shixun_environment_id'),
        'tab_type': e.get('tab_type') or 3,
        'myshixun': (d.get('myshixun') or {}).get('identifier'),
    }


async def upload_batch(source, classroom, experiment, task, token=None, cookie=None,
                       username=None, dry_run=False):
    """扫描答案根目录 .mdj 并按顺序全部上传到目标实验虚拟环境 userfiles。

    返回结构化结果：
        {"files": [路径...],
         "env": {"myshixun","env_id","gid","tab_type"},
         "results": [{"name","ok"(None=dry-run),"detail","bytes"}]}
    失败抛 CollectionError。
    """
    files = scan_mdj(source)
    if not files:
        raise CollectionError(f'在 {source} 下未匹配到任何 .mdj 文件。')

    async with open_client(token, cookie, username) as client:
        gid = task
        if not gid:
            gid = await resolve_gid(client, classroom, experiment)
        env = await resolve_env(client, gid)
        url = ('https://data.educoder.net/api/tasks/%(gid)s/upload_file'
               '?debug=admin&shixun_environment_id=%(env)s&tab_type=%(tab)s&is_jupyter=false'
               % {'gid': gid, 'env': env['env_id'], 'tab': env['tab_type']})
        out = {
            "files": [str(f) for f in files],
            "env": {"myshixun": env['myshixun'], "env_id": env['env_id'],
                    "gid": gid, "tab_type": env['tab_type']},
            "results": [],
        }

        if dry_run:
            for f in files:
                out["results"].append({"name": f.name, "ok": None,
                                       "detail": f"dry-run {f.stat().st_size} bytes",
                                       "bytes": f.stat().st_size})
            return out

        username, headers = load_settings(token, cookie, username)
        ctx_headers = {k: v for k, v in headers.items() if k != 'Content-Type'}
        async with async_playwright() as p:
            req = await p.request.new_context(
                extra_http_headers={**ctx_headers, 'User-Agent': 'Mozilla/5.0'})
            for fp in files:
                buf = fp.read_bytes()
                suffix = fp.suffix.lower()
                if suffix == '.mdj':
                    mime = 'application/json'
                elif suffix == '.txt':
                    mime = 'text/plain'
                else:
                    mime = 'application/octet-stream'
                try:
                    r = await req.post(url, multipart={
                        'file': {'name': fp.name, 'mimeType': mime, 'buffer': buf}})
                    try:
                        j = await r.json()
                        ok = j.get('status') == 0
                        detail = f"HTTP {r.status} {j if ok else str(j)[:120]}"
                    except Exception:
                        ok = False
                        detail = f"HTTP {r.status} body={(await r.text())[:150]!r}"
                except Exception as e:
                    ok = False
                    detail = str(e)[:150]
                out["results"].append({"name": fp.name, "ok": ok,
                                       "detail": detail, "bytes": len(buf)})
            await req.dispose()
        return out


async def run(source, classroom, experiment, task, dry_run,
              token=None, cookie=None, username=None):
    out = await upload_batch(source, classroom, experiment, task, token, cookie,
                             username, dry_run)
    files = out["files"]
    print(f'匹配到 {len(files)} 个 .mdj：')
    for i, f in enumerate(files, 1):
        print(f'   [{i}] {f}')
    env = out["env"]
    print(f'目标虚拟环境 myshixun={env["myshixun"]} env_id={env["env_id"]} '
          f'(来自任务 {env["gid"]})，全部上传 → /data/workspace/userfiles')
    for i, r in enumerate(out["results"], 1):
        if r["ok"] is None:
            print(f'  [{i}/{len(files)}] {r["name"]}: (dry-run) 将上传 {r["bytes"]} 字节')
        else:
            print(f'  [{i}/{len(files)}] {r["name"]}: {"OK" if r["ok"] else "FAIL"} → {r["detail"]}')


def main():
    ap = argparse.ArgumentParser(description='扫描答案根目录 .mdj 并批量上传到目标实验虚拟环境')
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--task', help='关卡 gid（直接指定目标）')
    g.add_argument('--classroom', help='课堂 code（配合 --experiment）')
    ap.add_argument('--experiment', help='实验名（配合 --classroom）')
    ap.add_argument('--source', required=True, help='答案根目录（递归扫描 .mdj）')
    ap.add_argument('--token', help='账号 Pc-Authorization token（=_educoder_session 值）')
    ap.add_argument('--cookie', help='账号 Cookie（_educoder_session=...; autologin_trustie=...）')
    ap.add_argument('--username', help='账号 username（zzud 参数）')
    ap.add_argument('--dry-run', action='store_true', help='只打印计划不上传')
    args = ap.parse_args()
    if not args.task and not args.experiment:
        raise SystemExit('使用 --task 或同时提供 --classroom 与 --experiment。')
    try:
        asyncio.run(run(args.source, args.classroom, args.experiment, args.task, args.dry_run,
                        args.token, args.cookie, args.username))
    except CollectionError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == '__main__':
    main()
