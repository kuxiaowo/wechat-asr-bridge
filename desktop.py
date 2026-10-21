"""Portable Windows desktop launcher; --serve runs its hidden API child."""
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import traceback
import webbrowser

from app_paths import resource_dir, runtime_dir
from desktop_service import (ServiceController, VERSION, health, saved_port, save_port,
                             saved_api_key, save_settings, transcribe_file)
from desktop_tray import TrayIcon
from bridge_config import validate_concurrency
from desktop_settings import saved_concurrency, saved_auth_enabled


class DesktopApplication:
    def __init__(self, root, port=8765):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.root = tk, ttk, root
        self.runtime = runtime_dir()
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.controller = ServiceController(port)
        self.controllers = {port: self.controller}
        self.events = queue.Queue()
        self.tray = None
        self.tray_ready = False
        self.tray_pending = False
        self.exit_requested = False
        self.closed = False
        self.layout_pending = False
        self.operation = None
        self.polling = False
        self.connected = False
        self.owned = False
        self.audio_path = None
        self.last_health = None
        self.request_diagnostics = ''
        self.started = None
        self.port = tk.StringVar(value=str(port))
        key_error = None
        try:
            key = saved_api_key(self.runtime, default=os.getenv('WECHAT_API_KEY', ''))
            auth_enabled = saved_auth_enabled(self.runtime, default=bool(os.getenv('WECHAT_API_KEY')))
        except ValueError as error:
            key, key_error, auth_enabled = '', str(error), False
        self.api_key = tk.StringVar(value=key)
        self.auth_enabled = tk.BooleanVar(value=auth_enabled)
        self.applied_api_key = key if auth_enabled else ''
        self.status = tk.StringVar(value='正在检查服务…')
        self.detail = tk.StringVar(value='model 指定识别后端。')
        self.file_name = tk.StringVar(value='还没有选择音频')
        self.file_detail = tk.StringVar(value='选择 WAV、MP3 等音频，单文件最大 25 MiB。')
        self.result_status = tk.StringVar(value='选择音频后，点击开始识别。')
        self.endpoint = tk.StringVar(value=f'http://127.0.0.1:{port}')
        self.concurrency = saved_concurrency(self.runtime)
        self.concurrency_vars = {key: tk.StringVar(value=str(value)) for key, value in self.concurrency.items()}
        self.model = tk.StringVar(value='doubao-voice')
        self._build()
        self.root.bind('<Configure>', lambda _: self._queue_layout_check(), add='+')
        self.root.after(100, self._drain_events)
        self.root.after(150, self._poll)
        self.root.protocol('WM_DELETE_WINDOW', self._close)
        if key_error:
            self.root.after(200, lambda: self._show_error(key_error))

    def _build(self):
        from desktop_ui import build
        build(self)

    def _label(self, parent, text, size, *, bold=False, variable=None, wrap=None, mono=False, bg=None):
        label = self.tk.Label(parent, text=text, textvariable=variable,
                              font=('Consolas' if mono else 'Microsoft YaHei UI', size, 'bold' if bold else 'normal'),
                              bg=bg or self.colors['paper'], fg=self.colors['ink'] if bold else self.colors['muted'],
                              justify='left', anchor='w')
        if wrap:
            label.configure(wraplength=wrap)
        return label

    def _work(self, action, success, failure=None):
        def run():
            try:
                result = action()
                self.events.put(lambda value=result: success(value))
            except Exception as error:
                self.events.put(lambda message=str(error): (failure or self._show_error)(message))
        threading.Thread(target=run, daemon=True).start()

    def _drain_events(self):
        try:
            while not self.closed:
                self.events.get_nowait()()
        except queue.Empty:
            pass
        if not self.closed:
            self.root.after(100, self._drain_events)

    def _apply_port(self, event=None):
        if self.operation or self.exit_requested:
            return False
        try:
            value = int(self.port.get())
            if value == self.controller.port:
                return True
            self.controller = ServiceController(value)
            self.controllers[value] = self.controller
            save_port(value, self.runtime)
            self.connected = self.owned = False
            self.endpoint.set(f'http://127.0.0.1:{value}')
            self.status.set('正在检查服务…')
            self._update_buttons()
            return True
        except (ValueError, OSError) as error:
            self.port.set(str(self.controller.port))
            self._show_error(str(error))
            return False

    def _poll(self):
        if self.closed:
            return
        if not self.exit_requested and not self.polling and self.operation not in ('starting', 'stopping'):
            self.polling = True
            controller = self.controller
            def check():
                current = health(controller.port)
                return current, controller.owns_service() if current else False
            self._work(check, lambda result: self._show_health(controller, result), self._poll_error)
        self.root.after(750 if self.connected else 2500, self._poll)

    def _poll_error(self, message):
        self.polling = False

    def _show_health(self, controller, result):
        from desktop_ui import show_health
        show_health(self, controller, result)

    def _update_buttons(self):
        idle = self.operation is None and not self.exit_requested
        for button, enabled in ((self.start_button, idle and not self.connected),
                                 (self.stop_button, idle and self.connected and self.owned),
                                 (self.recognize_button, idle and self.connected and self.audio_path is not None),
                                 (self.settings_button, idle),
                                 (self.choose_button, idle), (self.sample_button, idle)):
            button.configure(state='normal' if enabled else 'disabled')
        if self.port_box is not None and self.port_box.winfo_exists():
            self.port_box.configure(state='normal' if idle else 'disabled')
        self.model_box.configure(state='readonly' if idle else 'disabled')
        self._queue_layout_check()

    def _queue_layout_check(self):
        if not self.closed and not self.layout_pending:
            self.layout_pending = True
            self.root.after_idle(self._fit_window)

    def _fit_window(self):
        self.layout_pending = False
        if self.closed:
            return
        # Font metrics depend on Windows DPI; a fixed 680px minimum can clip
        # the link buttons before the bottom actions. Keep all fixed controls
        # inside the client area and let only unused space/result text grow.
        width = max(820, self.root.winfo_reqwidth())
        height = max(580, self.root.winfo_reqheight())
        if tuple(self.root.minsize()) != (width, height):
            self.root.minsize(width, height)
        current_width, current_height = self.root.winfo_width(), self.root.winfo_height()
        if current_width < width or current_height < height:
            self.root.geometry(f'{max(current_width, width)}x{max(current_height, height)}')

    def _settings(self):
        from desktop_ui import settings
        settings(self)

    def _apply_settings(self):
        if self.operation or self.exit_requested:
            return False
        old = self.controller
        key = self.api_key.get() if self.auth_enabled.get() else ''
        try:
            port = int(self.port.get())
            capacities = validate_concurrency({key: int(value.get()) for key, value in self.concurrency_vars.items()})
            controller = old if port == old.port else ServiceController(port)
            save_settings(port, self.api_key.get(), self.runtime, concurrency=capacities, auth_enabled=self.auth_enabled.get())
        except (ValueError, OSError) as error:
            self._show_error(str(error))
            return False
        changed = port != old.port or key != self.applied_api_key or capacities != self.concurrency
        restart = changed and self.connected and self.owned
        self.applied_api_key = key
        self.concurrency = capacities
        self.controller = controller
        self.controllers[port] = controller
        self.endpoint.set(f'http://127.0.0.1:{port}')
        if port != old.port:
            self.connected = self.owned = False
        if restart:
            self.operation = 'starting'
            self.status.set('正在启动服务…')
            self.detail.set('等待已提交的识别与会话清理完成。')
            def restart_service():
                old.stop()
                return controller.start(api_key=key, concurrency=capacities), controller.owns_service()
            def done(result):
                self.operation = None
                self._show_health(controller, result)
            self._work(restart_service, done, self._operation_error)
        self._update_buttons()
        return True

    def _start(self):
        if not self._apply_port():
            return
        key = self.api_key.get() if self.auth_enabled.get() else ''
        try:
            capacities = validate_concurrency({name: int(value.get()) for name, value in self.concurrency_vars.items()})
            save_settings(self.controller.port, self.api_key.get(), self.runtime, concurrency=capacities, auth_enabled=self.auth_enabled.get())
        except (ValueError, OSError) as error:
            self._show_error(str(error))
            return
        self.applied_api_key = key
        self.concurrency = capacities
        self.operation = 'starting'
        self.status.set('正在启动服务…')
        self.detail.set('首次启动可能需要几秒钟。')
        self._update_buttons()
        controller = self.controller
        def done(result):
            self.operation = None
            self._show_health(controller, result)
        self._work(lambda: (controller.start(api_key=key, concurrency=capacities), controller.owns_service()), done, self._operation_error)

    def _stop(self):
        self.operation = 'stopping'
        self.status.set('正在停止服务…')
        self.detail.set('等待已提交的识别与会话清理完成。')
        self._update_buttons()
        def done(_):
            self.operation = None
            self._show_health(self.controller, (None, False))
        self._work(self.controller.stop, done, self._operation_error)

    def _operation_error(self, message):
        self.operation = None
        self.status.set('操作未完成')
        self.detail.set(message)
        self._update_buttons()
        self._show_error(message)

    def _choose_file(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(title='选择要识别的音频', filetypes=[
            ('音频文件', '*.wav *.mp3 *.flac *.ogg *.m4a *.webm *.aac'), ('所有文件', '*.*')])
        if path:
            self._set_file(Path(path))

    def _set_file(self, path):
        if not path.is_file():
            self._show_error('音频文件不存在。')
            return
        self.audio_path = path
        self.file_name.set(path.name)
        self.file_detail.set(f'{path.stat().st_size / 1024:.1f} KiB · {path}')
        self.result_status.set('音频已选好，点击开始识别。')
        self._update_buttons()

    def _sample(self):
        path = resource_dir() / 'assets' / 'test.wav'
        if not path.exists():
            path = resource_dir() / 'tests' / 'fixtures' / 'test.wav'
        self._set_file(path)

    def _recognize(self):
        port, api_key, path, model = self.controller.port, self.api_key.get() if self.auth_enabled.get() else '', self.audio_path, self.model.get()
        if path is None:
            return
        self.operation = 'recognizing'
        self.started = time.monotonic()
        self.result_status.set(f'正在识别 · {model}')
        self.progress.configure(mode='indeterminate')
        self.progress.start(14)
        self._update_buttons()
        def done(text):
            self.operation = None
            self.progress.stop()
            self.progress.configure(mode='determinate', value=0)
            self.result.configure(state='normal')
            self.result.delete('1.0', 'end')
            self.result.insert('1.0', text)
            self.result.configure(state='disabled')
            warnings = getattr(text, 'warnings', [])
            self.request_diagnostics = '\n'.join(
                f"警告 [{warning.get('code', 'warning')}]：{warning.get('message', '')}" for warning in warnings)
            if getattr(text, 'request_id', None):
                self.request_diagnostics = '请求 ID：' + text.request_id + '\n' + self.request_diagnostics
            suffix = f' · {len(warnings)} 条警告，点击诊断详情' if warnings else ''
            self.result_status.set(f'识别完成 · {time.monotonic() - self.started:.1f} 秒' + suffix)
            self._update_buttons()
        def failed(message):
            self.operation = None
            self.progress.stop()
            self.progress.configure(mode='determinate', value=0)
            self.request_diagnostics = message
            self.result.configure(state='normal')
            self.result.delete('1.0', 'end')
            self.result.insert('1.0', message)
            self.result.configure(state='disabled')
            self.result_status.set('识别失败 · 详细原因见下方，可复制或打开诊断详情')
            self._update_buttons()
        self._work(lambda: transcribe_file(path, port, api_key, model), done, failed)

    def _copy(self, text):
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)

    def _copy_endpoint(self):
        self._copy(self.endpoint.get() + '/v1')

    def _copy_result(self):
        self._copy(self.result.get('1.0', 'end-1c'))

    def _save_result(self):
        from tkinter import filedialog
        text = self.result.get('1.0', 'end-1c')
        if not text:
            return
        path = filedialog.asksaveasfilename(title='保存识别文字', defaultextension='.txt',
                                          filetypes=[('文本文件', '*.txt')])
        if path:
            try:
                Path(path).write_text(text, encoding='utf-8')
                self.result_status.set('文字已保存。')
            except OSError as error:
                self._show_error(str(error))

    def _open_docs(self):
        if self.connected:
            webbrowser.open(self.endpoint.get() + '/docs?port=' + self.port.get())
        else:
            webbrowser.open((resource_dir() / 'docs' / 'api.html').as_uri() + '?port=' + self.port.get())

    def _open_logs(self):
        os.startfile(str(self.runtime))

    def _diagnostic_text(self):
        from diagnostics import format_diagnostics
        text = format_diagnostics(self.last_health)
        if self.request_diagnostics:
            text = '【最近手动请求】\n' + self.request_diagnostics + '\n\n' + text
        return text

    def _show_diagnostics(self):
        window = self.tk.Toplevel(self.root)
        window.title('诊断详情 · 错误与警告')
        window.geometry('780x460')
        window.minsize(480, 280)
        text = self._diagnostic_text()
        self.ttk.Button(window, text='复制完整诊断', command=lambda: self._copy(text)).pack(
            side='bottom', pady=(0, 12))
        panel = self.tk.Frame(window)
        panel.pack(fill='both', expand=True, padx=12, pady=12)
        content = self.tk.Text(panel, wrap='word', font=('Microsoft YaHei UI', 10))
        scroll = self.ttk.Scrollbar(panel, command=content.yview)
        scroll.pack(side='right', fill='y')
        content.configure(yscrollcommand=scroll.set)
        content.pack(fill='both', expand=True)
        content.insert('1.0', text)
        content.configure(state='disabled')

    def _show_error(self, message):
        from tkinter import messagebox
        messagebox.showerror('ASR Bridge', message, parent=self.root)

    def _close(self):
        if self.exit_requested or self.closed:
            return
        if self.tray_ready:
            self._hide_window()
        elif not self.tray_pending:
            self.tray_pending = True
            try:
                self.tray = TrayIcon(lambda: self.events.put(self._restore_window),
                                     lambda: self.events.put(self._request_exit))
                self.tray.start(lambda: self.events.put(self._tray_started),
                                lambda message: self.events.put(lambda: self._tray_failed(message)))
            except Exception as error:
                self._tray_failed(str(error))

    def _tray_started(self):
        self.tray_pending = False
        self.tray_ready = True
        if not self.exit_requested:
            self._hide_window()

    def _tray_failed(self, message):
        self.tray_pending = self.tray_ready = False
        if self.tray:
            self.tray.stop()
            self.tray = None
        self._restore_window()
        self._show_error('无法启动系统托盘，窗口将保持打开：' + message)

    def _hide_window(self):
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.withdraw()
        self.root.withdraw()

    def _restore_window(self):
        if self.closed:
            return
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.deiconify()
            self.settings_window.lift()

    def _request_exit(self):
        if self.exit_requested or self.closed:
            return
        self.exit_requested = True
        self._update_buttons()
        self._begin_exit()

    def _begin_exit(self):
        if self.operation:
            # A launch/recognition worker must finish before deciding which
            # child to stop. Hiding the window does not discard its result.
            self.root.after(100, self._begin_exit)
            return
        self.operation = 'exiting'
        self.status.set('正在停止服务…')
        self.detail.set('等待已提交的识别与会话清理完成。')
        def stop_services():
            for controller in self.controllers.values():
                if controller.owns_service():
                    controller.stop()
        self._work(stop_services, lambda _: self._finish_exit(), self._exit_failed)

    def _exit_failed(self, message):
        self.exit_requested = False
        self.operation = None
        self._restore_window()
        self._operation_error(message)

    def _finish_exit(self):
        if self.tray:
            try:
                self.tray.stop()
            except Exception as error:
                self._exit_failed(str(error))
                return
        self.closed = True
        self.root.destroy()


def self_test(output):
    import tkinter as tk
    import frida
    import imageio_ffmpeg
    import websocket
    import server
    server.configure_backend('all')
    report = {'version': VERSION, 'frozen': bool(getattr(sys, 'frozen', False)),
              'assets': (resource_dir() / 'agent.js').is_file(), 'frida': frida.__version__,
              'websocket': websocket.__version__, 'models': list(server.MODELS),
              'wetype_assets': (resource_dir() / 'wetype_agent.js').is_file(),
              'documentation': (resource_dir() / 'docs' / 'api.html').is_file()}
    root = tk.Tk()
    root.withdraw()
    report['tk'] = root.tk.call('info', 'patchlevel')
    app = DesktopApplication(root)
    app._sample()
    report['gui'] = app.start_button.winfo_exists() and app.result.winfo_exists()
    report['sample_audio'] = app.audio_path is not None and app.audio_path.is_file()
    import pystray
    from desktop_settings import _crypt
    report['tray_backend'] = pystray.Icon.__module__ == 'pystray._win32'
    test_key = b'"packaged-self-test"'
    report['key_encryption'] = _crypt(_crypt(test_key, protect=True), protect=False) == test_key
    root.destroy()
    result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-version'], capture_output=True,
                            timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    report['ffmpeg'] = result.returncode == 0
    report['passed'] = all(report[k] for k in ('assets', 'wetype_assets', 'documentation', 'ffmpeg', 'gui', 'sample_audio', 'tray_backend', 'key_encryption'))
    Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report['passed'] else 1


def main():
    args = sys.argv[1:]
    if args and args[0] == '--serve':
        # Windowed frozen apps have no standard streams. Uvicorn requires real
        # streams even when the process is launched invisibly by the desktop.
        runtime = runtime_dir()
        runtime.mkdir(parents=True, exist_ok=True)
        port = args[args.index('--port') + 1] if '--port' in args else '8765'
        with (runtime / f'desktop-service-{int(port)}.log').open('a', encoding='utf-8', buffering=1) as log:
            sys.stdout = sys.stderr = log
            from server import main as serve
            serve(args[1:])
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int)
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--self-test-lifecycle', action='store_true')
    parser.add_argument('--output', type=Path)
    options = parser.parse_args(args)
    if options.self_test or options.self_test_lifecycle:
        if not options.output:
            parser.error('--self-test requires --output')
        if options.self_test_lifecycle:
            from desktop_verify import lifecycle_self_test
            return lifecycle_self_test(options.output)
        return self_test(options.output)
    if sys.platform == 'win32':
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    import tkinter as tk
    root = tk.Tk()
    DesktopApplication(root, options.port if options.port is not None else saved_port())
    root.mainloop()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        runtime = runtime_dir()
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / 'desktop-error.log').write_text(traceback.format_exc(), encoding='utf-8')
        testing = '--self-test' in sys.argv or '--self-test-lifecycle' in sys.argv
        if testing and '--output' in sys.argv:
            Path(sys.argv[sys.argv.index('--output') + 1]).write_text(
                json.dumps({'passed': False, 'error': traceback.format_exc()}, ensure_ascii=False, indent=2), encoding='utf-8')
        if '--serve' not in sys.argv and not testing:
            import tkinter.messagebox
            try:
                tkinter.messagebox.showerror('应用启动失败', f'请查看错误日志：\n{runtime / "desktop-error.log"}')
            except Exception:
                pass
        raise SystemExit(1)
