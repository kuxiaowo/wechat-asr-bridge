"""Compact native Windows views; service ownership stays in desktop.py."""
from app_paths import resource_dir
from bridge_config import PROVIDERS
from desktop_service import VERSION


def build(app):
    tk, ttk, root = app.tk, app.ttk, app.root
    app.colors = {'bg': '#EEF3F8', 'paper': '#FFFFFF', 'ink': '#213547',
                  'muted': '#687D8E', 'accent': '#0067C0', 'line': '#DFE7EF', 'slot_border': '#8293A5'}
    c = app.colors
    root.title('ASR Bridge · 语音接口')
    root.geometry('940x660')
    root.minsize(820, 580)
    root.configure(bg=c['bg'])
    icon = resource_dir() / 'assets' / 'app.ico'
    if icon.exists():
        root.iconbitmap(str(icon))
    root.option_add('*Font', ('Microsoft YaHei UI', 10))
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('TButton', font=('Microsoft YaHei UI', 9), padding=(12, 7), width=0, relief='flat',
                    background='#E4ECF4', foreground=c['ink'], borderwidth=0)
    style.map('TButton', background=[('active', '#EAF2FC')], foreground=[('disabled', '#8D949E')])
    style.configure('Accent.TButton', background=c['accent'], foreground='white')
    style.map('Accent.TButton', background=[('active', '#0058A4'), ('disabled', '#DDE7F2')])
    style.configure('TEntry', padding=5, fieldbackground='white')
    style.configure('TCombobox', padding=6, borderwidth=1, bordercolor=c['line'], arrowcolor=c['muted'])
    style.map('TCombobox', fieldbackground=[('readonly', 'white')],
              selectbackground=[('readonly', 'white')], selectforeground=[('readonly', c['ink'])])
    style.configure('TProgressbar', background=c['accent'], troughcolor=c['bg'], borderwidth=0,
                    bordercolor=c['bg'], lightcolor=c['bg'], darkcolor=c['bg'], thickness=4)
    style.configure('Vertical.TScrollbar', background='#D7E2EC', troughcolor=c['paper'],
                    bordercolor=c['paper'], lightcolor=c['paper'], darkcolor=c['paper'], arrowsize=12)
    style.configure('Bridge.TNotebook', background=c['bg'], borderwidth=0, tabmargins=0,
                    bordercolor=c['bg'], lightcolor=c['bg'], darkcolor=c['bg'])
    style.configure('Bridge.TNotebook.Tab', padding=(18, 8), background=c['bg'], foreground=c['muted'])
    style.layout('Bridge.TNotebook.Tab', [])
    outer = tk.Frame(root, bg=c['bg'])
    outer.pack(fill='both', expand=True, padx=20, pady=16)
    header = tk.Frame(outer, bg=c['bg'])
    header.pack(fill='x', pady=(0, 12))
    tk.Label(header, text='ASR Bridge', bg=c['bg'], fg=c['ink'],
             font=('Segoe UI', 16, 'bold')).pack(side='left')
    tk.Label(header, text='语音接口', bg=c['bg'], fg=c['muted']).pack(side='left', padx=12)
    tk.Label(header, text=f'v{VERSION}', bg=c['bg'], fg=c['muted'], font=('Segoe UI', 9)).pack(side='right')
    navigation = tk.Frame(outer, bg=c['bg'])
    navigation.pack(fill='x', pady=(0, 14))
    service = tk.Frame(outer, bg=c['bg'])
    service.pack(fill='x', pady=(0, 10))
    app.state_label = tk.Label(service, textvariable=app.status, bg=c['bg'], fg=c['ink'])
    app.state_label.pack(side='left')
    app.settings_button = ttk.Button(service, text='设置', command=app._settings)
    app.settings_button.pack(side='right')
    app.stop_button = ttk.Button(service, text='停止服务', command=app._stop)
    app.stop_button.pack(side='right', padx=8)
    app.start_button = ttk.Button(service, text='启动服务', command=app._start, style='Accent.TButton')
    app.start_button.pack(side='right')
    app.notebook = ttk.Notebook(outer, style='Bridge.TNotebook')
    app.notebook.pack(fill='both', expand=True)
    home = tk.Frame(app.notebook, bg=c['bg'])
    manual = tk.Frame(app.notebook, bg=c['paper'], padx=16, pady=14)
    app.notebook.add(home, text='首页')
    app.notebook.add(manual, text='手动转写')
    nav_items = []
    for index, title in enumerate(('首页', '手动转写')):
        item = tk.Frame(navigation, bg=c['bg'])
        item.pack(side='left', padx=(0, 22))
        button = tk.Button(item, text=title, command=lambda i=index: app.notebook.select(i),
                           bg=c['bg'], activebackground=c['bg'], fg=c['muted'],
                           relief='flat', borderwidth=0, highlightthickness=0,
                           padx=2, pady=5, cursor='hand2', font=('Microsoft YaHei UI', 10))
        button.pack()
        underline = tk.Frame(item, height=2, bg=c['bg'])
        underline.pack(fill='x')
        nav_items.append((button, underline))
    def select_page(event=None):
        selected = app.notebook.index(app.notebook.select())
        for index, (button, underline) in enumerate(nav_items):
            button.configure(fg=c['accent'] if index == selected else c['muted'])
            underline.configure(bg=c['accent'] if index == selected else c['bg'])
        app._queue_layout_check()
    app.notebook.bind('<<NotebookTabChanged>>', select_page)
    select_page()
    pools = tk.Frame(home, bg=c['paper'], padx=14, pady=12)
    pools.pack(fill='x', pady=(0, 12))
    heading = tk.Frame(pools, bg=c['paper'])
    heading.pack(fill='x', pady=(0, 7))
    app._label(heading, '并发池', 10, bold=True).pack(side='left')
    app.pool_summary = tk.StringVar(value='运行 0 · 排队 0')
    app._label(heading, '', 9, variable=app.pool_summary).pack(side='right')
    app.pool_views = {}
    from tkinter.font import Font
    name_width = max(Font(root, family='Microsoft YaHei UI', size=9).measure(p['name']) for p in PROVIDERS.values()) + 20
    for key, provider in PROVIDERS.items():
        row = tk.Frame(pools, bg=c['paper'])
        row.pack(fill='x', pady=4)
        row.columnconfigure(1, weight=1)
        app._label(row, provider['name'], 9).grid(row=0, column=0, sticky='w', padx=(0, 12))
        row.columnconfigure(0, minsize=name_width)
        canvas = tk.Canvas(row, bg=c['paper'], height=24, highlightthickness=0, width=320)
        canvas.grid(row=0, column=1, sticky='ew')
        count = tk.StringVar()
        app._label(row, '', 9, variable=count).grid(row=0, column=2, sticky='e', padx=(12, 0))
        pages = tk.Frame(row, bg=c['paper'])
        pages.grid(row=1, column=1, sticky='e', pady=(3, 0))
        caption = tk.StringVar()
        previous = ttk.Button(pages, text='上一组', command=lambda k=key: page_pool(app, k, -1))
        previous.pack(side='left')
        app._label(pages, '', 8, variable=caption).pack(side='left', padx=6)
        following = ttk.Button(pages, text='下一组', command=lambda k=key: page_pool(app, k, 1))
        following.pack(side='left')
        app.pool_views[key] = dict(canvas=canvas, count=count, capacity=app.concurrency[key], occupied=[],
                                   page=0, pages=pages, caption=caption, previous=previous, following=following)
        pages.grid_remove()
        canvas.bind('<Configure>', lambda _, k=key: draw_pool(app, k))
    legend = tk.Frame(pools, bg=c['paper'])
    legend.pack(anchor='e', pady=(6, 0))
    for color, label in (('#22954E', '占用'), (c['paper'], '空闲')):
        mark = tk.Canvas(legend, width=10, height=10, bg=c['paper'], highlightthickness=0)
        mark.create_rectangle(0, 0, 9, 9, fill=color, outline=c['slot_border'] if label == '空闲' else color)
        mark.pack(side='left', padx=(9, 4))
        app._label(legend, label, 8).pack(side='left')
    info = tk.Frame(home, bg=c['paper'], padx=14, pady=12)
    info.pack(fill='x')
    info.columnconfigure(1, weight=1)
    app._label(info, '接入信息', 10, bold=True).grid(row=0, column=0, sticky='w', pady=(0, 10))
    ttk.Button(info, text='打开接口文档', command=app._open_docs).grid(row=0, column=2, sticky='e', pady=(0, 10))
    app._label(info, 'Base URL', 9).grid(row=1, column=0, sticky='w', padx=(0, 18))
    app.sdk_endpoint = tk.StringVar(value=app.endpoint.get() + '/v1')
    app.endpoint.trace_add('write', lambda *_: app.sdk_endpoint.set(app.endpoint.get() + '/v1'))
    tk.Entry(info, textvariable=app.sdk_endpoint, state='readonly', readonlybackground=c['paper'],
             fg=c['ink'], relief='flat', font=('Consolas', 10)).grid(row=1, column=1, sticky='ew')
    ttk.Button(info, text='复制地址', command=app._copy_endpoint).grid(row=1, column=2, sticky='e')
    tk.Frame(info, bg=c['line'], height=1).grid(row=2, column=0, columnspan=3, sticky='ew', pady=10)
    app._label(info, 'Model', 9).grid(row=3, column=0, sticky='nw', pady=6)
    for number, provider in enumerate(PROVIDERS.values(), 3):
        entry = tk.Frame(info, bg=c['paper'])
        entry.grid(row=number, column=1, columnspan=2, sticky='ew', pady=2)
        entry.columnconfigure(0, weight=1)
        variable = tk.StringVar(value=provider['model'])
        field = tk.Entry(entry, textvariable=variable, state='readonly', readonlybackground=c['paper'],
                         relief='flat', fg=c['ink'], font=('Consolas', 10))
        field.variable = variable
        field.grid(row=0, column=0, sticky='ew')
        app._label(entry, provider['name'], 9).grid(row=0, column=1, padx=12)
        ttk.Button(entry, text='复制', command=lambda m=provider['model']: app._copy(m)).grid(row=0, column=2)
    app._label(manual, '手动转写', 12, bold=True).pack(anchor='w')
    model_bar = tk.Frame(manual, bg=c['paper'])
    model_bar.pack(fill='x', pady=(12, 10))
    app._label(model_bar, '转写模型', 9).pack(side='left', padx=(0, 10))
    app.model_box = ttk.Combobox(model_bar, textvariable=app.model, state='readonly',
                                 values=[p['model'] for p in PROVIDERS.values()], width=24)
    app.model_box.pack(side='left')
    file_area = tk.Frame(manual, bg=c['bg'], padx=12, pady=10)
    file_area.pack(fill='x', pady=(0, 10))
    app._label(file_area, '', 9, variable=app.file_name, bg=c['bg']).pack(anchor='w')
    app._label(file_area, '', 8, variable=app.file_detail, bg=c['bg'], wrap=700).pack(anchor='w', pady=(3, 0))
    tools = tk.Frame(manual, bg=c['paper'])
    tools.pack(fill='x')
    app.choose_button = ttk.Button(tools, text='选择音频', command=app._choose_file)
    app.choose_button.pack(side='left', padx=(0, 8))
    app.sample_button = ttk.Button(tools, text='使用示例音频', command=app._sample)
    app.sample_button.pack(side='left')
    app.recognize_button = ttk.Button(tools, text='开始识别', command=app._recognize, style='Accent.TButton')
    app.recognize_button.pack(side='right')
    app._label(manual, '', 9, variable=app.result_status, wrap=720).pack(anchor='w', pady=(12, 5))
    app.progress = ttk.Progressbar(manual, mode='determinate', value=0)
    app.progress.pack(fill='x', pady=(0, 8))
    result_frame = tk.Frame(manual, bg=c['line'], padx=1, pady=1)
    result_frame.pack(fill='both', expand=True)
    app.result = tk.Text(result_frame, wrap='word', state='disabled', bg=c['paper'], fg=c['ink'],
                         font=('Microsoft YaHei UI', 10), padx=12, pady=10, relief='flat',
                         borderwidth=0, height=5, width=35, undo=False)
    scroll = ttk.Scrollbar(result_frame, command=app.result.yview)
    scroll.pack(side='right', fill='y')
    app.result.configure(yscrollcommand=scroll.set)
    app.result.pack(fill='both', expand=True)
    actions = tk.Frame(manual, bg=c['paper'])
    actions.pack(fill='x', pady=(9, 0))
    ttk.Button(actions, text='复制文字', command=app._copy_result).pack(side='left', padx=(0, 8))
    ttk.Button(actions, text='另存为 TXT', command=app._save_result).pack(side='left')
    footer = tk.Frame(outer, bg=c['bg'])
    footer.pack(fill='x', pady=(9, 0))
    app._label(footer, '', 8, variable=app.detail, wrap=470, bg=c['bg']).pack(side='left')
    ttk.Button(footer, text='运行日志', command=app._open_logs).pack(side='right')
    ttk.Button(footer, text='诊断详情', command=app._show_diagnostics).pack(side='right', padx=(0, 8))
    app.port_box = app.settings_window = None
    for key in PROVIDERS:
        draw_pool(app, key)
    app._update_buttons()


def draw_pool(app, key):
    view = app.pool_views[key]
    canvas, capacity = view['canvas'], view['capacity']
    # Draw a window of slots, not millions of Tk widgets for a large capacity.
    page_size = 128
    view['page'] = min(view['page'], (capacity - 1) // page_size)
    start, end = view['page'] * page_size, min((view['page'] + 1) * page_size, capacity)
    factor = float(app.root.tk.call('tk', 'scaling')) / (96 / 72)
    size, gap = max(12, round(12 * factor)), max(3, round(4 * factor))
    width = canvas.winfo_width()
    if width < 32:
        width = int(canvas.cget('width'))
    columns = max(1, width // (size + gap))
    rows = (end - start + columns - 1) // columns
    height = rows * (size + gap) + 2
    if int(canvas.cget('height')) != height:
        canvas.configure(height=height)
    canvas.delete('all')
    occupied = set(view['occupied'])
    for index in range(start, end):
        x = (index - start) % columns * (size + gap)
        y = (index - start) // columns * (size + gap) + 1
        busy = index in occupied
        canvas.create_rectangle(x, y, x + size - 1, y + size - 1,
                                fill='#22954E' if busy else app.colors['paper'],
                                outline='#22954E' if busy else app.colors['slot_border'])
    view['count'].set(f'{len(occupied)} / {capacity} 占用')
    if capacity > page_size:
        view['pages'].grid()
        view['caption'].set(f'{start + 1}–{end} / {capacity}')
        view['previous'].configure(state='normal' if start else 'disabled')
        view['following'].configure(state='normal' if end < capacity else 'disabled')
    else:
        view['pages'].grid_remove()


def page_pool(app, key, step):
    view = app.pool_views[key]
    view['page'] = max(0, min((view['capacity'] - 1) // 128, view['page'] + step))
    draw_pool(app, key)
    app._queue_layout_check()


def show_health(app, controller, result):
    app.polling = False
    if controller is not app.controller or app.operation in ('starting', 'stopping'):
        return
    current, app.owned = result
    app.last_health = current
    app.connected = current is not None
    failed = bool(current and (current.get('last_error') or current.get('status') != 'ok'))
    warnings = current.get('warnings', []) if current else []
    app.status.set('最近识别失败 · 诊断详情可查看原因' if failed else
                   '服务运行中 · 有兼容性警告' if warnings else '本机服务运行中' if current else '服务未启动')
    app.state_label.configure(fg='#B33232' if failed else '#956100' if warnings else app.colors['ink'])
    states = {s['provider']: s for s in current.get('pools', [])} if current else {}
    for key, view in app.pool_views.items():
        data = states.get(key, {})
        view['capacity'] = max(1, int(data.get('max_concurrency', app.concurrency[key])))
        view['occupied'] = data.get('occupied_slots', list(range(int(data.get('active_requests', 0)))))
        draw_pool(app, key)
    if current:
        active, queued = current.get('active_requests', 0), current.get('queued_requests', 0)
        app.pool_summary.set(f'运行 {active} · 排队 {queued}')
        error_detail = current.get('last_error_detail') or {}
        message = error_detail.get('message') or current.get('last_error') or '服务状态异常，请打开诊断详情。'
        app.detail.set((str(message).splitlines()[0][:80] + ' · 诊断详情') if failed else
                       f'{len(warnings)} 条兼容性警告 · 点击诊断详情查看完整内容' if warnings else
                       '服务运行中 · model 指定后端' if app.owned else '已连接现有服务，此窗口不会停止它。')
    else:
        app.pool_summary.set('运行 0 · 排队 0')
        app.detail.set('选择模型后调用对应后端；微信和输入法需保持客户端可用。')
    app._update_buttons()


def settings(app):
    tk, ttk = app.tk, app.ttk
    if app.settings_window is not None and app.settings_window.winfo_exists():
        app.settings_window.lift()
        return
    window = tk.Toplevel(app.root)
    app.settings_window = window
    window.title('服务设置')
    window.transient(app.root)
    window.resizable(False, False)
    panel = ttk.Frame(window, padding=20)
    panel.pack(fill='both', expand=True)
    ttk.Label(panel, text='本机接口端口').pack(anchor='w')
    app.port_box = ttk.Entry(panel, textvariable=app.port, width=34)
    app.port_box.pack(fill='x', pady=(5, 12))
    ttk.Checkbutton(panel, text='启用 API Key 鉴权', variable=app.auth_enabled).pack(anchor='w')
    ttk.Entry(panel, textvariable=app.api_key, show='•', width=34).pack(fill='x', pady=(5, 12))
    for key, provider in PROVIDERS.items():
        line = ttk.Frame(panel)
        line.pack(fill='x', pady=4)
        ttk.Label(line, text=provider['name'] + '并发数').pack(side='left')
        ttk.Entry(line, textvariable=app.concurrency_vars[key], width=12).pack(side='right')
    ttk.Label(panel, text='每个模型默认 4 路，可分别设置并发数，超出本池排队。').pack(anchor='w', pady=(9, 4))
    ttk.Label(panel, text='关闭鉴权时，客户端 API Key 可任意填写或留空。').pack(anchor='w', pady=(0, 6))
    ttk.Label(panel, text='修改后会重启此窗口启动的服务，等待已有请求完成。').pack(anchor='w', pady=(0, 12))
    def apply():
        if app._apply_settings():
            window.destroy()
    ttk.Button(panel, text='保存设置', style='Accent.TButton', command=apply).pack(fill='x')
