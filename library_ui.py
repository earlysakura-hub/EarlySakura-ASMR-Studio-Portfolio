"""Persistent embeddable library page, reusing Studio jobs/player/SRT engine."""
from pathlib import Path
import os
import queue
import re
import stat
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter import font as tkfont
from tkinter.scrolledtext import ScrolledText

import task_store as tasks
from gui_preferences import GuiPreferences, DEFAULT_PLOT_FONT_SIZE, MIN_PLOT_FONT_SIZE, MAX_PLOT_FONT_SIZE, PreferencesError
from media_library import LibraryError
from library_files import archive, import_refinement, read_plot, recover_archive, recover_import
from make_srt import prepare_srt, confirm_srt, generate_srt
from audio_duration import DurationReader, UNKNOWN as UNKNOWN_DURATION


def audio_is_missing(path):
    """Hide confirmed missing/non-file paths; do not treat access errors as deletion."""
    try:
        return not stat.S_ISREG(Path(path).stat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False


def fit_library_columns(widths, available):
    """Fit four preferences to the viewport without overwriting preferences."""
    available = max(160, available)
    fitted = dict(widths)
    others = tuple(key for key in fitted if key != 'title')
    extra = sum(fitted[key] - 40 for key in others)
    budget = available - 160
    if extra > budget:
        remaining = budget
        for key in others[:-1]:
            share = (fitted[key] - 40) * budget // extra
            fitted[key] = 40 + share; remaining -= share
        fitted[others[-1]] = 40 + remaining
    fitted['title'] = available - sum(fitted[key] for key in others)
    return fitted


def resize_library_columns(widths, key, delta):
    """Move a boundary by transferring pixels, keeping all columns visible."""
    result = dict(widths); keys = tuple(result); index = keys.index(key)
    neighbors = keys[index + 1:] if index < len(keys) - 1 else tuple(reversed(keys[:index]))
    if delta >= 0:
        remaining = min(delta, sum(result[name] - 40 for name in neighbors))
        result[key] += remaining
        for name in neighbors:
            take = min(remaining, result[name] - 40)
            result[name] -= take; remaining -= take
    else:
        give = min(-delta, result[key] - 40)
        result[key] -= give; result[neighbors[0]] += give
    return result


def wrap_library_buttons(frame, buttons):
    """Wrap existing buttons using their actual theme/DPI-aware requested size."""
    def arrange(event=None):
        cell_width = max(button.winfo_reqwidth() for button in buttons) + 4
        available = event.width if event is not None else cell_width * min(2, len(buttons))
        columns = max(1, min(len(buttons), available // cell_width))
        for index, button in enumerate(buttons):
            row, column = divmod(index, columns)
            old = button.grid_info()
            if old.get('row') != row or old.get('column') != column:
                button.grid(row=row, column=column, sticky='w', padx=(0, 4), pady=(0, 4))
    frame.bind('<Configure>', arrange)
    arrange()


class LibraryWindow:
    def __init__(self, app, parent=None):
        self.app, self.library = app, app.library
        self.events = queue.Queue(); self.busy = False; self.rows = {}; self.current = None
        library_root = self.library.root()  # Fail before creating a partial window.
        self.closing = False
        self.duration_events = queue.Queue()
        self.duration_reader = DurationReader(self.duration_events)
        self._duration_request = None
        parent = parent or app.pages['library']
        self.window = window = ttk.Frame(parent)
        window.pack(fill='both',expand=True)
        self.root_text = tk.StringVar(value=str(library_root))
        top = ttk.Frame(window, padding=10); top.pack(fill='x')
        ttk.Label(top, text='媒体库目录：').pack(side='left')
        ttk.Entry(top, textvariable=self.root_text, state='readonly').pack(side='left', fill='x', expand=True)
        ttk.Button(top, text='设置目录', command=self.configure).pack(side='left', padx=6)
        ttk.Button(top, text='扫描待处理音频', command=self.scan_inbox).pack(side='left')
        filters = ttk.Frame(window, padding=(10, 0, 10, 8)); filters.pack(fill='x')
        self.search = tk.StringVar(); self.status_filter = tk.StringVar(value='全部'); self.tag_filter = tk.StringVar(value='全部标签')
        ttk.Label(filters, text='标题 / 文件名 / RJ：').pack(side='left')
        entry = ttk.Entry(filters, textvariable=self.search, width=28); entry.pack(side='left')
        entry.bind('<Return>', lambda event: self.refresh())
        status = ttk.Combobox(filters, textvariable=self.status_filter, state='readonly', width=10,
                              values=('全部', '已处理', '待处理', '待精修', '已精修', '喜爱'))
        status.pack(side='left', padx=6); status.bind('<<ComboboxSelected>>', lambda event: self.refresh())
        self.tag_box = ttk.Combobox(filters, textvariable=self.tag_filter, state='readonly', width=15)
        self.tag_box.pack(side='left'); self.tag_box.bind('<<ComboboxSelected>>', lambda event: self.refresh())
        ttk.Button(filters, text='搜索 / 刷新', command=self.refresh).pack(side='left', padx=6)
        ttk.Button(filters, text='标签管理', command=self.manage_tags).pack(side='left')
        panes = ttk.Panedwindow(window, orient='horizontal'); panes.pack(fill='both', expand=True, padx=10)
        left, right = ttk.Frame(panes, width=480), ttk.Frame(panes, padding=(12, 0, 0, 0))
        # Column preferences must not enlarge the left pane's requested width.
        # The pane still expands/resizes normally; only geometry propagation stops.
        left.grid_propagate(False)
        panes.add(left, weight=1); panes.add(right, weight=1)
        self.preferences = GuiPreferences(self.library.database.parent / 'gui_preferences.json')
        self._saved_column_widths = self.preferences.library_column_widths()
        self._column_widths = dict(self._saved_column_widths)
        self._column_drag_start = None
        self._column_layout_id = None
        self._column_drag = False; self._column_save_id = None
        self.list = ttk.Treeview(left, columns=('title', 'rj', 'state', 'favorite'), show='headings', selectmode='browse')
        for key, label in (('title', '作品标题'), ('rj', '编号 / 文件'), ('state', '状态'), ('favorite', '喜爱')):
            self.list.heading(key, text=label)
            self.list.column(key, width=self._column_widths[key], minwidth=40, stretch=False)
        scroll = ttk.Scrollbar(left, orient='vertical', command=self.list.yview)
        self.list.configure(yscrollcommand=scroll.set)
        horizontal = ttk.Scrollbar(left,orient='horizontal',command=self.list.xview)
        self.list.configure(xscrollcommand=horizontal.set)
        self.list.grid(row=0,column=0,sticky='nsew'); scroll.grid(row=0,column=1,sticky='ns')
        horizontal.grid(row=1,column=0,sticky='ew')
        left.rowconfigure(0,weight=1); left.columnconfigure(0,weight=1)
        self.list.bind('<<TreeviewSelect>>', self.select)
        self.list.bind('<Double-1>', self.column_double_click)
        self.list.bind('<ButtonPress-1>', self.column_press, add='+')
        self.list.bind('<B1-Motion>', self.column_motion, add='+')
        self.list.bind('<ButtonRelease-1>', self.column_release, add='+')
        self.list.bind('<Configure>', self.schedule_column_layout, add='+')
        self.title = tk.StringVar(); self.info = tk.StringVar(value='请选择作品。')
        ttk.Label(right, text='作品显示标题').pack(anchor='w')
        ttk.Entry(right, textvariable=self.title).pack(fill='x', pady=4)
        buttons = ttk.Frame(right); buttons.pack(fill='x')
        self.work_buttons = []; title_buttons = []
        for label, callback in (('保存标题', self.save_title), ('♡ 喜爱切换', self.favorite), ('编辑作品标签', self.assign_tags)):
            button = ttk.Button(buttons, text=label, command=callback)
            title_buttons.append(button); self.work_buttons.append(button)
        wrap_library_buttons(buttons, title_buttons)
        playback_actions = ttk.Frame(right); playback_actions.pack(fill='x', pady=(4, 0))
        self.play_button = ttk.Button(playback_actions, text='播放', command=self.play)
        self.folder_button = ttk.Button(playback_actions, text='打开文件夹', command=self.open_folder)
        for button in (self.play_button, self.folder_button):
            self.work_buttons.append(button)
        wrap_library_buttons(playback_actions, (self.play_button, self.folder_button))
        # Fill the allocated width without feeding wrap requests back into pane sizing.
        detail=ttk.Label(right, textvariable=self.info, justify='left', wraplength=500, width=1)
        detail.pack(anchor='w',fill='x',pady=8)
        right.bind('<Configure>',lambda event:detail.configure(wraplength=max(180,event.width-12)))
        actions = ttk.Frame(right); actions.pack(fill='x')
        file_buttons = []
        for label, callback in (('导入精修成果', self.import_final), ('导入完整剧情', self.import_plot), ('重试归档', self.retry_archive)):
            button = ttk.Button(actions, text=label, command=callback)
            file_buttons.append(button); self.work_buttons.append(button)
        self.srt_button = ttk.Button(actions, text='生成 SRT', command=self.make_srt)
        file_buttons.append(self.srt_button); self.work_buttons.append(self.srt_button)
        wrap_library_buttons(actions, file_buttons)
        plot_header = ttk.Frame(right); plot_header.pack(fill='x', pady=(12, 4))
        plot_header.columnconfigure(0, weight=1)
        ttk.Label(plot_header, text='完整剧情介绍（含剧透）').grid(row=0, column=0, sticky='w')
        font_controls = ttk.Frame(plot_header); font_controls.grid(row=0, column=1, sticky='e')
        self.plot_font_size = self.preferences.plot_font_size()
        # A private font: positive Tk sizes are points, and respect Windows scaling.
        self.plot_font = tkfont.nametofont('TkTextFont', root=window).copy()
        families = set(tkfont.families(window))
        family = next((name for name in ('Microsoft YaHei UI', 'Microsoft YaHei') if name in families), self.plot_font.cget('family'))
        self.plot_font.configure(family=family, size=self.plot_font_size)
        self.plot_font_label = tk.StringVar(value=f'{self.plot_font_size} pt')
        self.plot_font_minus = ttk.Button(font_controls, text='A−', width=3, command=lambda: self.change_plot_font(-1))
        self.plot_font_minus.pack(side='left')
        ttk.Label(font_controls, textvariable=self.plot_font_label, width=5, anchor='center').pack(side='left')
        self.plot_font_plus = ttk.Button(font_controls, text='A+', width=3, command=lambda: self.change_plot_font(1))
        self.plot_font_plus.pack(side='left')
        ttk.Button(font_controls, text='重置', width=4, command=self.reset_plot_font).pack(side='left', padx=(4, 0))
        self.update_plot_font_controls()
        self.plot = ScrolledText(right, width=1, height=8, wrap='word', state='disabled',
                                 font=self.plot_font, spacing1=4, spacing2=2, spacing3=4)
        self.plot.pack(fill='both', expand=True)
        bottom = ttk.Frame(window, padding=10); bottom.pack(fill='x')
        self.notice = tk.StringVar(value='标签和显示标题只改元数据，不移动文件。')
        ttk.Label(bottom, textvariable=self.notice, wraplength=800).pack(side='left', fill='x', expand=True)
        ttk.Button(bottom, text='恢复中断操作', command=self.recover).pack(side='right')
        ttk.Button(bottom, text='旧作品迁移预览', command=self.preview).pack(side='right', padx=6)
        self.refresh(); self._pump_id = window.after(100, self.pump)

    def column_double_click(self, event):
        # Tk delivers Double-1 instead of ButtonPress-1 for rapid nearby drags.
        # A separator is still a resize operation, never a playback request.
        if self.list.identify_region(event.x, event.y) == 'separator': return self.column_press(event)
        else: self.play()

    def schedule_column_layout(self, event=None):
        if not self.closing and self._column_layout_id is None:
            self._column_layout_id = self.window.after_idle(self.layout_columns)

    def layout_columns(self):
        self._column_layout_id = None
        if self.closing or self._column_drag or self.list.winfo_width() < 3: return
        self.apply_column_widths(fit_library_columns(self._column_widths, self.list.winfo_width() - 2))

    def apply_column_widths(self, widths):
        for key, width in widths.items():
            if self.list.column(key, 'width') != width: self.list.column(key, width=width)
        self.list.xview_moveto(0)

    def column_press(self, event):
        self._column_drag = self.list.identify_region(event.x, event.y) == 'separator'
        if self._column_drag:
            index = int(self.list.identify_column(event.x)[1:]) - 1
            keys = tuple(self._column_widths)
            if not 0 <= index < len(keys):
                self._column_drag = False; return
            key = keys[index]
            self.list.focus_set()
            self._column_drag_start = (key, event.x, {name: self.list.column(name, 'width') for name in keys})
        if self._column_drag and self._column_save_id is not None:
            self.window.after_cancel(self._column_save_id)
            self._column_save_id = None
        if self._column_drag: return 'break'  # Do not also run Tk's unbounded drag.

    def column_motion(self, event):
        if not self._column_drag or self.closing: return
        key, start_x, widths = self._column_drag_start
        self.apply_column_widths(resize_library_columns(widths, key, event.x - start_x))
        return 'break'

    def column_release(self, event):
        if self.closing or not self._column_drag: return
        self.column_motion(event)
        self.capture_column_drag()
        if self._column_save_id is not None: self.window.after_cancel(self._column_save_id)
        # Runs after Tk's own resize binding; combine consecutive adjustments.
        self._column_save_id = self.window.after(350, self.save_column_widths)
        return 'break'

    def capture_column_drag(self):
        if self._column_drag_start is not None:
            _, _, before = self._column_drag_start
            widths = {key: self.list.column(key, 'width') for key in self._column_widths}
            if widths != before: self._column_widths = widths
        self._column_drag = False; self._column_drag_start = None
        if self._column_layout_id is not None:
            self.window.after_cancel(self._column_layout_id); self._column_layout_id = None
        self.layout_columns()

    def save_column_widths(self):
        self._column_save_id = None
        if self._column_drag: self.capture_column_drag()
        widths = dict(self._column_widths)
        if widths == self._saved_column_widths: return
        try:
            self.preferences.save_library_column_widths(widths)
            self._saved_column_widths = widths
        except (OSError, PreferencesError) as error:
            text = f'列宽已调整，但界面偏好未保存：{error}'
            self.notice.set(text)
            if hasattr(self.app, 'append_log'): self.app.append_log(text + '\n')

    def run(self, title, operation, callback=None, task=None):
        if self.busy or self.app.exiting: return
        if task is not None and self.app.task_in_use(task):
            messagebox.showinfo('作品正在使用', '请先结束此作品的播放或汉化，再修改其文件。其他作品可继续播放。', parent=self.window)
            return
        self.busy = True; self.notice.set(title)
        if task is not None: self.app.library_busy_tasks.add(task.identity['task_id'])
        self.app.update_buttons(); self.controls()
        def execute():
            try:
                self.events.put((True, operation(), callback, task))
            except Exception as error:
                self.events.put((False, str(error), callback, task))
        threading.Thread(target=execute, daemon=True).start()

    def pump(self):
        if self.closing or not self.window.winfo_exists(): return
        for _ in range(20):
            try: self.apply_duration(self.duration_events.get_nowait())
            except queue.Empty: break
        try:
            success, value, callback, task = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            if task is not None: self.app.library_busy_tasks.discard(task.identity['task_id'])
            self.app.update_buttons()
            if success:
                self.notice.set('操作完成。')
                try:
                    if callback is not None: callback(value)
                except Exception as error:
                    messagebox.showerror('媒体库显示未更新', str(error), parent=self.window)
            else:
                self.notice.set('操作未完成，原文件或恢复资料保留。')
                messagebox.showerror('媒体库操作未完成', value, parent=self.window)
            self.controls()
        self._pump_id = self.window.after(100, self.pump)

    def controls(self):
        for button in self.work_buttons:
            button.configure(state='normal' if self.current is not None and not self.busy and not self.app.exiting else 'disabled')

    def refresh(self):
        if self.busy: return
        search, status, tag = self.search.get(), self.status_filter.get(), self.tag_filter.get()
        def fetch():
            labels = self.library.tags()
            mapping = {row['name']: row['id'] for row in labels}
            works = self.library.list(search, status, mapping.get(tag))
            visible = [work for work in works if not audio_is_missing(work['audio_path'])]
            return visible, labels, len(works) - len(visible)
        def render(result):
            works, labels, missing = result
            previous = self.current['work_id'] if self.current else None
            self.rows = {work['work_id']: work for work in works}
            for key in self.list.get_children(): self.list.delete(key)
            for work in works:
                self.list.insert('', 'end', iid=work['work_id'], values=(work['display_title'], work['rj'] or work['original_name'], work['state'], '♥' if work['favorite'] else ''))
            self.tag_box.configure(values=['全部标签', *[tag['name'] for tag in labels]])
            if previous not in self.rows:
                self.current = None; self._duration_request = None; self.duration_reader.request(None, None)
                self.title.set(''); self.info.set('请选择作品。'); self.set_plot('暂无剧情介绍')
            if previous in self.rows:
                self.list.selection_set(previous)
            elif works:
                self.list.selection_set(works[0]['work_id'])
            else:
                self.info.set('没有符合筛选条件且音频可用的作品。' if missing else '没有符合筛选条件的作品。')
            if missing:
                self.notice.set(f'已隐藏 {missing} 部音频文件已不存在的作品；登记和播放进度保留，恢复原路径后刷新可重新显示。')
            self.controls()
        self.run('正在加载媒体库……', fetch, render)

    def select(self, event=None):
        selection = self.list.selection()
        if not selection or selection[0] not in self.rows: return
        work = self.current = self.rows[selection[0]]
        self.set_plot('正在读取完整剧情……')
        self.title.set(work['title'] or Path(work['original_name']).stem)
        self.info.set(f"音频时长：{UNKNOWN_DURATION}\n标签：{'、'.join(work['tags']) or '无'}")
        version = self.duration_reader.request(work['work_id'], work['audio_path'])
        self._duration_request = (version, work['work_id'], work['audio_path'])
        def render(text):
            if self.current and self.current['work_id'] == work['work_id']: self.set_plot(text)
        if not self.busy:
            def fetch_plot():
                try: return read_plot(work)
                except (ValueError, OSError, UnicodeError) as error:
                    return '剧情文件无法读取，原文件保留：\n' + str(error)
            self.run('正在读取完整剧情……', fetch_plot, render)
        self.controls()

    def apply_duration(self, result):
        version, work_id, audio, value = result
        if (self.closing or self._duration_request != (version, work_id, audio) or not self.current
                or self.current['work_id'] != work_id or self.current['audio_path'] != audio): return
        self.info.set(re.sub(r'(?m)^音频时长：[^\n]*', '音频时长：' + value, self.info.get(), count=1))

    def set_plot(self, text):
        self.plot.configure(state='normal'); self.plot.delete('1.0', 'end'); self.plot.insert('1.0', text); self.plot.configure(state='disabled')

    def update_plot_font_controls(self):
        self.plot_font_label.set(f'{self.plot_font_size} pt')
        self.plot_font_minus.configure(state='disabled' if self.plot_font_size <= MIN_PLOT_FONT_SIZE else 'normal')
        self.plot_font_plus.configure(state='disabled' if self.plot_font_size >= MAX_PLOT_FONT_SIZE else 'normal')

    def change_plot_font(self, delta):
        self.set_plot_font_size(self.plot_font_size + delta)

    def reset_plot_font(self):
        self.set_plot_font_size(DEFAULT_PLOT_FONT_SIZE)

    def set_plot_font_size(self, size):
        self.plot_font_size = max(MIN_PLOT_FONT_SIZE, min(MAX_PLOT_FONT_SIZE, size))
        self.plot_font.configure(size=self.plot_font_size)
        self.update_plot_font_controls()
        try:
            self.preferences.save_plot_font_size(self.plot_font_size)
        except (OSError, PreferencesError) as error:
            # The display change still works; do not alter any work metadata.
            text = f'剧情字号已应用，但阅读偏好未保存：{error}'
            self.notice.set(text)
            if hasattr(self.app, 'append_log'): self.app.append_log(text + '\n')

    def selected_task(self):
        if self.current is None: raise LibraryError('请选择作品。')
        # File operations call this in their worker thread, not in Tk callbacks.
        return tasks.load_task(self.current['task_directory'], verify_audio=False)

    def save_title(self):
        if not self.current or self.busy: return
        work_id, title = self.current['work_id'], self.title.get().strip()
        self.run('正在保存标题……', lambda: self.library.update(work_id, title=title), lambda value: self.refresh())

    def favorite(self):
        if not self.current or self.busy: return
        work = self.current
        self.run('正在更新喜爱……', lambda: self.library.update(work['work_id'], favorite=0 if work['favorite'] else 1), lambda value: self.refresh())

    def manage_tags(self):
        if self.busy: return
        dialog = tk.Toplevel(self.window); dialog.title('普通标签管理')
        listing = tk.Listbox(dialog, width=28, height=10); listing.pack(fill='both', expand=True, padx=10, pady=10)
        records = []
        def reload():
            records[:] = [tag for tag in self.library.tags() if not tag['system']]
            listing.delete(0, 'end')
            for tag in records: listing.insert('end', tag['name'])
        def save(rename=False):
            selected = listing.curselection()
            if rename and not selected: return
            tag = records[selected[0]] if rename else None
            name = simpledialog.askstring('标签名称', '输入普通标签：', initialvalue=tag['name'] if tag else '', parent=dialog)
            if name is None: return
            try:
                self.library.save_tag(name, tag['id'] if tag else None); reload(); self.refresh()
            except Exception as error: messagebox.showerror('标签未保存', str(error), parent=dialog)
        def delete():
            selected = listing.curselection()
            if not selected: return
            tag = records[selected[0]]
            if messagebox.askyesno('删除标签', f"删除标签“{tag['name']}”？只移除分类关系，不删除作品文件。", parent=dialog, default='no'):
                try: self.library.delete_tag(tag['id']); reload(); self.refresh()
                except Exception as error: messagebox.showerror('未删除', str(error), parent=dialog)
        bar = ttk.Frame(dialog); bar.pack(pady=(0, 10))
        ttk.Button(bar, text='新建', command=save).pack(side='left')
        ttk.Button(bar, text='重命名', command=lambda: save(True)).pack(side='left')
        ttk.Button(bar, text='删除', command=delete).pack(side='left')
        ttk.Label(dialog, text='系统“喜爱”不在此列表，使用作品爱心切换。').pack(padx=10, pady=6)
        try: reload()
        except Exception as error:
            dialog.destroy(); messagebox.showerror('标签暂不可用', str(error), parent=self.window)

    def assign_tags(self):
        if not self.current or self.busy: return
        work = self.current
        try: tags = [row for row in self.library.tags() if not row['system']]
        except Exception as error:
            messagebox.showerror('标签暂不可用', str(error), parent=self.window); return
        dialog = tk.Toplevel(self.window); dialog.title('作品多标签')
        listing = tk.Listbox(dialog, selectmode='multiple', exportselection=False, width=30, height=12); listing.pack(padx=12, pady=12)
        for index, tag in enumerate(tags):
            listing.insert('end', tag['name'])
            if tag['name'] in work['tags']: listing.selection_set(index)
        def save():
            ids = [tags[index]['id'] for index in listing.curselection()]
            dialog.destroy()
            self.run('正在保存作品标签……', lambda: self.library.set_tags(work['work_id'], ids), lambda value: self.refresh())
        ttk.Button(dialog, text='保存', command=save).pack(pady=(0, 10))

    def configure(self):
        if self.busy: return
        root = filedialog.askdirectory(parent=self.window, title='选择媒体库根目录', initialdir=str(self.library.root().parent))
        if not root: return
        if not messagebox.askyesno('设置媒体库目录', f'在 {root} 使用“待处理音频”和“已处理作品”？\n不会移动现有旧作品。已有作品时不能直接换库。', parent=self.window, default='no'): return
        def done(value): self.root_text.set(root); self.refresh()
        self.run('正在设置媒体库……', lambda: self.library.configure(root), done)

    def scan_inbox(self):
        if self.busy: return
        def scan():
            from batch_queue import AUDIO_SUFFIXES
            inbox = self.library.root() / '待处理音频'
            if not inbox.is_dir(): raise LibraryError('请先设置或创建媒体库目录。')
            count = 0; moved = []
            for audio in inbox.iterdir():
                if audio.is_file() and audio.suffix.lower() in AUDIO_SUFFIXES:
                    task = tasks.open_task(audio)
                    self.library.register(task); count += 1
                    from task_store import check_draft_progress
                    if task.result('japanese.txt').is_file():
                        done, total = check_draft_progress(task.result('japanese.txt'), task.result('draft_bilingual.txt'))
                        if total and done == total: moved.append(archive(task, self.library))
            return count, moved
        def done(result):
            count, moved = result
            for task in moved: self.relocated(task)
            self.notice.set(f'已核对 {count} 部音频，归档 {len(moved)} 部完整结果；未启动模型。')
            self.refresh()
        self.run('正在扫描待处理音频，核对身份……', scan, done)

    def open_folder(self):
        if not self.current or self.busy or self.app.exiting: return
        try:
            audio = Path(self.current['audio_path'])
            if not audio.is_absolute() or not audio.is_file() or not audio.parent.is_dir():
                raise LibraryError(f'音频文件或所在目录已不存在，请核对作品路径：\n{audio}')
            os.startfile(str(audio.parent))  # Native Shell directory open, no command string.
        except (OSError, ValueError, TypeError) as error:
            messagebox.showerror('无法打开作品文件夹', str(error), parent=self.window)

    def make_srt(self):
        if not self.current or self.busy or self.app.exiting or getattr(self.app, 'confirming', False): return
        work = dict(self.current)  # Keep the selected identity, never use app.task.
        def preview():
            task = tasks.load_task(work['task_directory'], verify_audio=False)
            if (task.identity['task_id'] != work['task_id']
                    or os.path.normcase(task.identity['path']) != os.path.normcase(work['audio_path'])):
                raise LibraryError('媒体库音频或任务身份不匹配，未生成字幕。')
            return task, prepare_srt(task)
        def ready(result):
            task, plan = result
            if self.app.exiting or self.closing: return
            if getattr(self.app, 'confirming', False):
                self.notice.set('另一个确认窗口仍在使用，请关闭后重新点击生成 SRT；未修改文件。')
                return
            if not self.current or self.current['work_id'] != work['work_id']:
                self.notice.set('作品选择已变化，请重新点击当前作品的生成 SRT；未修改文件。')
                return
            if self.app.task_in_use(task):
                messagebox.showinfo('作品正在使用', '请先结束此作品的播放或汉化，再生成字幕。其他作品可继续播放。', parent=self.window)
                return
            self.busy = True; self.app.confirming = True
            self.app.update_buttons(); self.controls()
            try:
                accepted = confirm_srt(plan, lambda title, text: messagebox.askyesno(title,
                    f"作品：{work['display_title']}\n音频：{work['audio_path']}\n\n{text}", parent=self.window, default='no'))
            finally:
                self.busy = False; self.app.confirming = False
                self.app.update_buttons(); self.controls()
            if not accepted:
                self.notice.set('已取消 SRT 生成；现有字幕和 TXT 保持不变。')
                return
            if not self.current or self.current['work_id'] != work['work_id']:
                self.notice.set('确认期间作品选择已变化，未生成；请重新选择并确认。')
                return
            def done(output):
                label = '精翻' if plan['source'] == 'final' else '粗翻'
                self.notice.set(f'SRT 已生成（{label}）：{output}')
                messagebox.showinfo('SRT 已生成', f"作品：{work['display_title']}\n来源：{label}\n输出：{output}", parent=self.window)
            self.run('正在生成并验证 SRT……', lambda: generate_srt(task, plan), done, task)
        self.run('正在核对当前作品的字幕来源……', preview, ready)

    def play(self):
        if not self.current or self.busy: return
        work = self.current
        def load():
            task = tasks.load_task(work['task_directory'])
            from studio import inspect_task
            return task, inspect_task(task)
        def start(result):
            task, (states, *flags) = result
            self.app.task, self.app.flags = task, flags
            self.app.audio_name.set(work['display_title']); self.app.audio_path.set(task.identity['path'])
            for name, state in states.items(): self.app.file_states[name].set(state)
            self.app.start_player(task)
            self.app.select_page('player')
        self.run('正在核对作品并打开播放器……', load, start)

    def import_final(self):
        if not self.current or self.busy: return
        final = filedialog.askopenfilename(parent=self.window, title='选择 final_bilingual.txt', filetypes=[('精修文本', '*.txt')])
        if not final: return
        plot = None
        if messagebox.askyesno('可选完整剧情', '是否同时选择 full_plot_summary.txt？没有剧情也可以完成精修。', parent=self.window):
            plot = filedialog.askopenfilename(parent=self.window, title='选择 full_plot_summary.txt（可取消）', filetypes=[('剧情文本', '*.txt')]) or None
        self.import_files(final, plot)

    def import_plot(self):
        if not self.current or self.busy: return
        plot = filedialog.askopenfilename(parent=self.window, title='选择 full_plot_summary.txt', filetypes=[('剧情文本', '*.txt')])
        if plot: self.import_files(None, plot)

    def import_files(self, final, plot):
        work = self.current
        try: task = tasks.load_task(work['task_directory'], verify_audio=False)
        except (ValueError, OSError) as error:
            messagebox.showerror('作品无法打开', str(error), parent=self.window); return
        if not messagebox.askyesno('确认精修/剧情归属', f"作品：{work['display_title']}\n音频：{work['audio_path']}\n\n{final or ''}\n{plot or ''}\n\n确认这些文件属于这部作品？精修确认代表你已人工检查，程序只校验格式。", parent=self.window, default='no'): return
        replace = any((task.directory / name).exists() for name in
                      (['final_bilingual.txt', 'output.srt'] if final else []) + (['full_plot_summary.txt'] if plot else []))
        if replace and not messagebox.askyesno('替换现有成果', '已有精修、字幕或剧情。本次替换会保留可恢复旧版本，先完整校验新文件。是否继续？', parent=self.window, default='no'): return
        self.run('正在校验、备份并导入成果……', lambda: import_refinement(work['work_id'], final, plot, confirmed=True, replace=replace, library=self.library),
                 lambda value: self.refresh(), task)

    def retry_archive(self):
        if not self.current or self.busy: return
        work = self.current
        try:
            task = tasks.load_task(work['task_directory'], verify_audio=False)
            in_inbox = self.library.in_inbox(task.identity['path'])
        except (ValueError, OSError) as error:
            messagebox.showerror('无法重试归档', str(error), parent=self.window); return
        if not in_inbox:
            messagebox.showinfo('不自动移动旧作品', '自动归档仅处理待处理音频目录中的作品。旧作品请先查看迁移预览并另行确认。', parent=self.window)
            return
        if not messagebox.askyesno('重试归档', '只复核已有转写/粗翻并归档，不启动模型；冲突或中断需先恢复。', parent=self.window, default='no'): return
        self.run('正在重试归档……', lambda: archive(task, self.library), self.relocated, task)

    def relocated(self, task):
        if task is not None:
            if self.app.task and self.app.task.identity['task_id'] == task.identity['task_id']:
                self.app.task = task; self.app.audio_path.set(task.identity['path'])
            if self.app.batch:
                entry = self.app.batch.get(task.identity['task_id'])
                if entry:
                    entry.update(audio=task.identity['path'], directory=str(task.directory), results={name: str(task.result(name)) for name in tasks.RESULT_NAMES})
                    self.app.save_batch()
        self.refresh()

    def recover(self):
        if self.busy: return
        try: operations = self.library.pending_operations()
        except (ValueError, OSError) as error:
            messagebox.showerror('无法读取恢复清单', str(error), parent=self.window); return
        if not operations:
            messagebox.showinfo('无需恢复', '没有未完成的媒体库文件操作。', parent=self.window); return
        if not messagebox.askyesno('恢复中断操作', f'核对并恢复 {len(operations)} 项中断操作？\n归档只提交已完整校验的副本；未完成导入会恢复旧成果。未知或变化的文件会保留并停止。', parent=self.window, default='no'): return
        def recover_all():
            result = []
            for op in operations:
                result.append(recover_archive(op['op_id'], self.library) if op['kind'] == 'archive' else recover_import(op['op_id'], self.library))
            return result
        def done(result):
            for task in result:
                if isinstance(task, tasks.Task): self.relocated(task)
            if not self.busy: self.refresh()
        self.run('正在核对并恢复中断操作……', recover_all, done)

    def preview(self):
        if self.busy: return
        from library_migration import write_preview
        def done(path):
            messagebox.showinfo('迁移预览已保存', f'{path}\n\n只扫描和准备，没有移动、覆盖或删除旧作品。执行需另行明确确认。', parent=self.window)
        self.run('正在只读扫描旧作品并生成预览……', lambda: write_preview(library=self.library), done)

    def close(self):
        if self.busy:
            messagebox.showinfo('操作仍在运行', '请等待当前媒体库文件/数据库操作结束，避免中断提交。', parent=self.window)
            return
        self.app.select_page('pipeline')  # Fixed pages survive navigation.

    def shutdown(self):
        layout = getattr(self, '_column_layout_id', None)
        if layout is not None:
            self.window.after_cancel(layout); self._column_layout_id = None
        pending = getattr(self, '_column_save_id', None)
        if pending is not None: self.window.after_cancel(pending)
        if pending is not None or getattr(self, '_column_drag', False): self.save_column_widths()
        self._column_drag = False
        self.closing=True
        if getattr(self, 'duration_reader', None): self.duration_reader.close()
        if getattr(self,'_pump_id',None):
            self.window.after_cancel(self._pump_id)
            self._pump_id=None
