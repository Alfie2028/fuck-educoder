"""头歌多账号答案导入工具（桌面 GUI）。

功能与状态机（用户设计）：
    账密入口(写入账号+密码)  →  点「开始执行」自动 Playwright Edge 有头登录抓 cookie
        → 登录成功账号归入「待导入列表」
        → 点「导入全部」按统一目标(课堂/实验/答案目录)上传到虚拟环境 userfiles
        → 导入完成账号归入「完成列表」
    状态持久化到本地 state.json，重启后保留。

运行（Windows 本地）：
    .venv\\Scripts\\python.exe -m educoder_reminder.gui_app
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

from .errors import CollectionError
from .login_account import login_get_cookie
from .upload_answers import scan_mdj, upload_batch

STATE_FILE = Path(__file__).resolve().parents[1] / 'state.json'

STATUS_LABEL = {'pending': '待登录', 'ready': '待导入', 'done': '已完成', 'failed': '失败'}
# 用于清理旧状态枚举（旧版本可能用别的词），统一为下方四态
VALID_STATUS = ('pending', 'ready', 'done', 'failed')


def _default_state():
    return {
        'config': {'classroom': '2i6ng7vp', 'experiment': '面向对象分析之用例图',
                   'source': ''},
        'accounts': [],
    }


def _token_from_cookie(cookie: str) -> str:
    """从 cookie 串里取 _educoder_session 值，即 Pc-Authorization token。"""
    for part in (cookie or '').split(';'):
        if '=' in part:
            k, _, v = part.strip().partition('=')
            if k == '_educoder_session':
                return v
    return ''


class GuiApp:
    def __init__(self, root: tk.Tk, state_file: Path = STATE_FILE):
        self.root = root
        self.state_file = Path(state_file)
        self.state = self._load_state()
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        root.title('头歌多账号答案导入工具')
        root.geometry('860x640')
        self._build_ui()
        self.root.after(100, self._poll)
        self._refresh_list()

    # ---------- 状态存取 ----------

    def _load_state(self):
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding='utf-8'))
                data.setdefault('config', _default_state()['config'])
                data.setdefault('accounts', [])
                return data
            except Exception:
                pass
        return _default_state()

    def _save(self):
        try:
            self.state_file.write_text(json.dumps(self.state, ensure_ascii=False, indent=2),
                                       encoding='utf-8')
        except Exception as e:
            self.log(f'保存状态失败: {e}')

    # ---------- UI ----------

    def _build_ui(self):
        pad = {'padx': 6, 'pady': 4}
        frm_cfg = ttk.LabelFrame(self.root, text='导入目标配置（统一固定）')
        frm_cfg.pack(fill='x', padx=8, pady=(8, 4))
        self.var_classroom = tk.StringVar(value=self.state['config'].get('classroom', ''))
        self.var_experiment = tk.StringVar(value=self.state['config'].get('experiment', ''))
        self.var_source = tk.StringVar(value=self.state['config'].get('source', ''))
        ttk.Label(frm_cfg, text='课堂 code').grid(row=0, column=0, sticky='w', **pad)
        ttk.Entry(frm_cfg, textvariable=self.var_classroom, width=18).grid(row=0, column=1, **pad)
        ttk.Label(frm_cfg, text='答案主目录（其下每个子文件夹名=实验名）').grid(row=1, column=0, columnspan=2, sticky='w', **pad)
        ttk.Entry(frm_cfg, textvariable=self.var_source, width=52).grid(row=1, column=2, columnspan=2, sticky='we', **pad)
        ttk.Button(frm_cfg, text='浏览…', command=self._browse_source).grid(row=1, column=4, **pad)

        frm_acc = ttk.LabelFrame(self.root, text='账密入口（写入账号密码，可批量添加）')
        frm_acc.pack(fill='x', padx=8, pady=4)
        self.var_phone = tk.StringVar()
        self.var_password = tk.StringVar()
        ttk.Label(frm_acc, text='账号(手机号)').grid(row=0, column=0, sticky='w', **pad)
        ttk.Entry(frm_acc, textvariable=self.var_phone, width=20).grid(row=0, column=1, **pad)
        ttk.Label(frm_acc, text='密码').grid(row=0, column=2, sticky='w', **pad)
        ttk.Entry(frm_acc, textvariable=self.var_password, width=24, show='*').grid(row=0, column=3, **pad)
        ttk.Button(frm_acc, text='添加账号', command=self._add_account).grid(row=0, column=4, **pad)
        ttk.Button(frm_acc, text='删除选中', command=self._remove_selected).grid(row=0, column=5, **pad)

        frm_list = ttk.LabelFrame(self.root, text='账号列表（状态流转：待登录 → 待导入 → 已完成）')
        frm_list.pack(fill='both', expand=True, padx=8, pady=4)
        cols = ('account', 'status', 'username', 'detail')
        self.tree = ttk.Treeview(frm_list, columns=cols, show='headings', height=12)
        for c, txt, w in (('account', '账号', 130), ('status', '状态', 70),
                          ('username', 'username', 130), ('detail', '详情', 380)):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor='w')
        vs = ttk.Scrollbar(frm_list, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side='left', fill='both', expand=True)
        vs.pack(side='right', fill='y')

        frm_btn = ttk.Frame(self.root)
        frm_btn.pack(fill='x', padx=8, pady=4)
        self.btn_login = ttk.Button(frm_btn, text='开始执行（获取 Cookie）', command=self._start_login)
        self.btn_login.pack(side='left', padx=4)
        self.btn_import = ttk.Button(frm_btn, text='导入全部', command=self._start_import)
        self.btn_import.pack(side='left', padx=4)
        self.btn_reimport = ttk.Button(frm_btn, text='重新导入选中', command=self._reimport_selected)
        self.btn_reimport.pack(side='left', padx=4)
        self.btn_check = ttk.Button(frm_btn, text='检测识别实验', command=self._log_experiments)
        self.btn_check.pack(side='left', padx=4)

        frm_log = ttk.LabelFrame(self.root, text='日志')
        frm_log.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        self.txt_log = tk.Text(frm_log, height=8, wrap='word', state='disabled')
        slog = ttk.Scrollbar(frm_log, orient='vertical', command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=slog.set)
        self.txt_log.pack(side='left', fill='both', expand=True)
        slog.pack(side='right', fill='y')

    def _browse_source(self):
        d = filedialog.askdirectory(initialdir=self.var_source.get() or None,
                                    title='选择答案主目录（其下每个子文件夹名=实验名）')
        if d:
            self.var_source.set(d)
            self._log_experiments()

    def log(self, msg: str):
        self.txt_log.configure(state='normal')
        self.txt_log.insert('end', msg + '\n')
        self.txt_log.see('end')
        self.txt_log.configure(state='disabled')

    # ---------- 账号操作 ----------

    def _add_account(self):
        phone = self.var_phone.get().strip()
        pwd = self.var_password.get()
        if not phone or not pwd:
            messagebox.showwarning('提示', '请填写账号和密码。')
            return
        if any(a['phone'] == phone for a in self.state['accounts']):
            messagebox.showwarning('提示', f'账号 {phone} 已存在。')
            return
        self.state['accounts'].append(
            {'phone': phone, 'password': pwd, 'status': 'pending',
             'cookie': '', 'username': '', 'user_id': None, 'error': ''})
        self._save()
        self._refresh_list()
        self.log(f'已添加账号 {phone}')
        self.var_phone.set('')
        self.var_password.set('')

    def _remove_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        if not messagebox.askyesno('确认', '删除选中的账号？'):
            return
        idxs = [int(iid) for iid in sel]
        kept = [a for i, a in enumerate(self.state['accounts']) if i not in idxs]
        self.state['accounts'] = kept
        self._save()
        self._refresh_list()

    def _reimport_selected(self):
        """把选中账号重置为可重新导入：有 Cookie→待导入，无 Cookie→待登录。"""
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo('提示', '请先选择要重新导入的账号（可多选）。')
            return
        idxs = [int(iid) for iid in sel]
        need_login = 0
        for i in idxs:
            a = self.state['accounts'][i]
            if a.get('cookie'):
                a['status'] = 'ready'
            else:
                a['status'] = 'pending'
                need_login += 1
            a['error'] = ''
        self._save()
        self._refresh_list()
        msg = f'已将 {len(idxs)} 个账号重置。'
        if need_login:
            msg += f' 其中 {need_login} 个无 Cookie，需先点「开始执行」重新登录。'
        else:
            msg += ' 点「导入全部」即可重新导入。'
        self.log(msg)

    def _refresh_list(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for i, a in enumerate(self.state['accounts']):
            st = STATUS_LABEL.get(a.get('status', 'pending'), a.get('status', ''))
            detail = a.get('error') or ('user_id=%s' % a.get('user_id') if a.get('user_id') else '')
            self.tree.insert('', 'end', iid=str(i), values=(a['phone'], st, a.get('username', ''), detail))

    def _commit_cfg(self):
        self.state['config'] = {'classroom': self.var_classroom.get().strip(),
                                'experiment': self.var_experiment.get().strip(),
                                'source': self.var_source.get().strip()}
        self._save()

    # ---------- 后台执行 ----------

    def _post(self, item):
        self.q.put(item)

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == 'log':
                    self.log(item[1])
                elif kind == 'refresh':
                    self._refresh_list()
                elif kind == 'done':
                    self.busy = False
                    self.btn_login.configure(state='normal')
                    self.btn_import.configure(state='normal')
                    self.btn_reimport.configure(state='normal')
                    self.btn_check.configure(state='normal')
                    self.log(item[1])
                elif kind == 'busy':
                    self.busy = True
                    self.btn_login.configure(state='disabled')
                    self.btn_import.configure(state='disabled')
                    self.btn_reimport.configure(state='disabled')
                    self.btn_check.configure(state='disabled')
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _start_login(self):
        if self.busy:
            return
        self._commit_cfg()
        targets = [a for a in self.state['accounts'] if a['status'] in ('pending', 'failed')]
        if not targets:
            messagebox.showinfo('提示', '没有待登录的账号（先添加账号）。')
            return
        self._post(('busy',))
        threading.Thread(target=self._login_worker, args=(targets,), daemon=True).start()

    def _login_worker(self, targets):
        try:
            total = len(targets)
            for i, acc in enumerate(targets, 1):
                phone = acc['phone']
                try:
                    cookie, username, uid = login_get_cookie(phone, acc['password'])
                    acc.update(status='ready', cookie=cookie, username=username or '',
                               user_id=uid, error='')
                    self._post(('log', f'[{i}/{total}] {phone} 登录成功，username={username}'))
                except Exception as e:
                    acc.update(status='failed', cookie='', error=str(e))
                    self._post(('log', f'[{i}/{total}] {phone} 登录失败: {e}'))
                self._save()
                self._post(('refresh',))
        finally:
            self._post(('done', '开始执行完成。'))

    def _start_import(self):
        if self.busy:
            return
        self._commit_cfg()
        targets = [a for a in self.state['accounts'] if a['status'] == 'ready']
        if not targets:
            messagebox.showinfo('提示', '没有待导入的账号（先执行“开始执行”获取 Cookie）。')
            return
        cfg = self.state['config']
        if not cfg.get('source'):
            messagebox.showwarning('提示', '请先在配置区选择答案主目录。')
            return
        self._post(('busy',))
        threading.Thread(target=self._import_worker, args=(targets,), daemon=True).start()

    def _experiments(self):
        """扫描答案主目录下所有子文件夹（子文件夹名=实验名），返回 [(实验名, 目录)]。"""
        root = Path(self.state['config'].get('source', ''))
        if not root.is_dir():
            return []
        exps = []
        for d in sorted(root.iterdir()):
            if d.is_dir() and scan_mdj(str(d)):
                exps.append((d.name, str(d)))
        return exps

    def _log_experiments(self):
        """打印识别到的实验列表，确认主目录下多个实验子文件夹都被识别。"""
        self._commit_cfg()
        exps = self._experiments()
        if not exps:
            self.log('未识别到含答案(.mdj/.txt)的实验子文件夹，请确认答案主目录。')
            return
        self.log(f'识别到 {len(exps)} 个实验（导入时将全部上传）：')
        for n, d in exps:
            self.log(f'  · {n}（{len(scan_mdj(d))} 个答案文件）')

    def _import_worker(self, targets):
        try:
            cfg = self.state['config']
            exps = self._experiments()
            if not exps:
                self._post(('log', '答案主目录下没有找到含 .mdj 答案的实验子文件夹。'))
                return
            total = len(targets)
            for i, acc in enumerate(targets, 1):
                phone = acc['phone']
                token = _token_from_cookie(acc.get('cookie', ''))
                all_ok = True
                detail_lines = []
                try:
                    for exp_name, exp_dir in exps:
                        out = asyncio.run(upload_batch(
                            exp_dir, cfg['classroom'], exp_name, None,
                            token=token, cookie=acc.get('cookie', ''),
                            username=acc.get('username', '')))
                        n_ok = sum(1 for r in out['results'] if r['ok'])
                        n_total = len(out['results'])
                        ok = n_ok == n_total
                        all_ok = all_ok and ok
                        detail_lines.append(f'{exp_name}: {n_ok}/{n_total}')
                    acc['status'] = 'done' if all_ok else 'failed'
                    acc['error'] = '' if all_ok else '部分实验失败'
                    self._post(('log',
                                f'[{i}/{total}] {phone} 导入完成: ' + ' | '.join(detail_lines)))
                except Exception as e:
                    acc['status'] = 'failed'
                    acc['error'] = str(e)
                    self._post(('log', f'[{i}/{total}] {phone} 导入失败: {e}'))
                self._save()
                self._post(('refresh',))
        finally:
            self._post(('done', '导入全部完成。'))


def main():
    root = tk.Tk()
    GuiApp(root, STATE_FILE)
    root.mainloop()


if __name__ == '__main__':
    main()
