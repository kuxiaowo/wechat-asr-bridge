"""Opt-in frozen-app lifecycle verification; no WeChat recognition or OS input."""
import json
import os
from pathlib import Path
import socket
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid


def inspect_layout(app):
    """Check real allocated rectangles, including every containing frame."""
    app.root.update()
    app.root.update_idletasks()
    wanted = {'启动服务', '停止服务', '复制地址', '打开接口文档',
              '设置', '运行日志', '诊断详情', '选择音频', '使用示例音频',
              '开始识别', '复制文字', '另存为 TXT'}
    results = []
    def walk(widget):
        for child in widget.winfo_children():
            if isinstance(child, (app.ttk.Button, app.tk.Button)) and child.winfo_ismapped():
                assert child.winfo_width() >= child.winfo_reqwidth(), ('width clipped', child.cget('text'))
                assert child.winfo_height() >= child.winfo_reqheight(), ('height clipped', child.cget('text'))
                current = child
                while current is not app.root:
                    parent = current.master
                    assert current.winfo_x() >= 0 and current.winfo_y() >= 0
                    assert current.winfo_x() + current.winfo_width() <= parent.winfo_width(), ('parent width clipped', child.cget('text'),str(current),current.winfo_x(),current.winfo_width(),parent.winfo_width())
                    assert current.winfo_y() + current.winfo_height() <= parent.winfo_height(), ('parent height clipped', child.cget('text'),str(current),current.winfo_y(),current.winfo_height(),parent.winfo_height())
                    current = parent
                if child.cget('text') in wanted:
                    results.append(dict(text=child.cget('text'),width=child.winfo_width(),height=child.winfo_height()))
            walk(child)
    selected = app.notebook.select()
    for tab in app.notebook.tabs():
        app.notebook.select(tab)
        app.root.update()
        app.root.update_idletasks()
        walk(app.root)
        if app.result.winfo_ismapped():
            assert app.result.winfo_height() >= app.result.winfo_reqheight(), 'Result area clipped'
    app.notebook.select(selected)
    assert {item['text'] for item in results} == wanted
    return dict(passed=True,window=[app.root.winfo_width(),app.root.winfo_height()],minimum=list(app.root.minsize()),controls=results)


def lifecycle_self_test(output):
    from desktop import DesktopApplication
    from desktop_service import health, port_in_use, process_info
    from desktop_settings import saved_api_key
    import tkinter as tk
    report = {'passed': False, 'frozen': bool(getattr(sys, 'frozen', False)),
              'scope': 'Real Tk window, Win32 tray and packaged service; invoke registered menu callbacks, not physical mouse clicks. No recognition requested.',
              'checks': []}
    target = Path(output).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    previous_data = os.environ.get('LOCALAPPDATA')
    previous_key = os.environ.get('WECHAT_API_KEY')
    root = app = None
    callback_errors = []
    try:
        if not report['frozen']:
            raise RuntimeError('Use the built Windows executable for this verification.')
        # This test never reads/writes the user's ordinary desktop settings.
        case = target.parent / ('lifecycle-' + uuid.uuid4().hex)
        case.mkdir()
        os.environ['LOCALAPPDATA'] = str(case)
        os.environ.pop('WECHAT_API_KEY', None)
        report['isolated_data'] = str(case)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        root = tk.Tk()
        root.withdraw()
        root.report_callback_exception = lambda *error: callback_errors.append(error)
        app = DesktopApplication(root, port)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def pump(condition, timeout=35):
            deadline = time.monotonic() + timeout
            while not condition():
                if callback_errors:
                    raise RuntimeError('Tk callback failed: ' + str(callback_errors[0][1]))
                if time.monotonic() > deadline:
                    raise TimeoutError('Desktop verification timed out.')
                if not app.closed:
                    root.update()
                time.sleep(.03)

        def response(key):
            request = urllib.request.Request(f'http://127.0.0.1:{port}/v1/models',
                                             headers={'Authorization': 'Bearer ' + key})
            try:
                with opener.open(request, timeout=3) as result:
                    return result.status
            except urllib.error.HTTPError as error:
                return error.code

        first_key, second_key = 'lifecycle-test-key-one', 'lifecycle-test-key-two'
        app.api_key.set('unused-configured-key')
        assert not app.auth_enabled.get()
        app._start()
        pump(lambda: app.operation is None and app.connected and app.owned)
        assert response('any-client-key') == 200 and response('') == 200
        unauth_pid = app.controller._metadata()['pid']
        app.api_key.set('another-unused-key')
        assert app._apply_settings()
        assert app.operation is None and app.controller._metadata()['pid'] == unauth_pid
        report['checks'].append('disabled_auth_ignores_keys_and_unused_key_changes_do_not_restart')
        app.auth_enabled.set(True)
        app.api_key.set(first_key)
        assert app._apply_settings()
        pump(lambda: app.operation is None and app.connected and app.owned)
        assert response(first_key) == 200 and response('wrong-test-key') == 401
        before_pid = app.controller._metadata()['pid']
        report['checks'].append('packaged_service_authentication')

        app._close()
        pump(lambda: app.tray_ready and root.state() == 'withdrawn')
        assert app.tray.icon.visible and app.tray.thread.is_alive()
        assert health(port) and response(first_key) == 200
        assert process_info(before_pid) is not None
        report['checks'].append('close_hides_window_keeps_real_tray_and_service')

        app.tray.icon.menu.items[0](app.tray.icon)
        pump(lambda: root.state() == 'normal')
        report['checks'].append('registered_show_menu_callback_restores_window')
        app._sample()
        root.update_idletasks()
        report['layout'] = inspect_layout(app)
        report['checks'].append('selected_audio_layout_controls_and_result_fit')

        app.api_key.set(second_key)
        assert app._apply_settings()
        pump(lambda: app.operation is None and app.connected and app.owned)
        assert response(second_key) == 200 and response(first_key) == 401
        assert process_info(before_pid) is None
        child_pid = app.controller._metadata()['pid']
        assert saved_api_key(app.runtime) == second_key
        assert second_key not in (app.runtime / 'desktop-settings.json').read_text(encoding='utf-8')
        report['checks'].append('key_change_restarts_owned_service_and_persists_encrypted')

        app._close()
        assert root.state() == 'withdrawn'
        tray = app.tray
        app.tray.icon.menu.items[1](app.tray.icon)
        pump(lambda: app.closed)
        assert not tray.icon.visible and not tray.thread.is_alive()
        assert not port_in_use(port) and health(port) is None
        assert process_info(child_pid) is None
        report['checks'].append('registered_exit_menu_callback_removes_tray_and_stops_child')

        # Recreate the application from the saved file; no new service needed.
        reopened = tk.Tk()
        reopened.withdraw()
        again = DesktopApplication(reopened, port)
        assert again.api_key.get() == second_key and again.auth_enabled.get()
        again.closed = True
        reopened.destroy()
        report['checks'].append('reopened_application_restores_saved_key')
        report['passed'] = True
    except Exception:
        report['error'] = traceback.format_exc()
    finally:
        if app is not None and not app.closed:
            for controller in app.controllers.values():
                if controller.owns_service():
                    controller.stop()
            if app.tray:
                app.tray.stop()
            app.closed = True
        if root is not None and (app is None or not report['passed']):
            try:
                root.destroy()
            except tk.TclError:
                pass
        for name, value in (('LOCALAPPDATA', previous_data), ('WECHAT_API_KEY', previous_key)):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report['passed'] else 1
