"""Native Tk geometry regression checks for the clipped SDK/docs button row."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import desktop
from desktop_service import ServiceController
from desktop_verify import inspect_layout


@unittest.skipUnless(sys.platform == 'win32', 'Native Windows Tk layout')
class DesktopLayoutTests(unittest.TestCase):
    def test_diagnostics_remain_readable_and_copy_button_is_visible(self):
        import tkinter as tk
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            root = tk.Tk()
            try:
                with patch.object(desktop, 'runtime_dir', return_value=runtime), \
                        patch.object(desktop, 'ServiceController', side_effect=lambda port: ServiceController(port, runtime=runtime)), \
                        patch.object(desktop, 'health', return_value=None):
                    app = desktop.DesktopApplication(root)
                message = 'HTTP 503\n原生错误 [native_failure]\nRuntimeError: specific failure\n请求 ID：req_visible'
                app.request_diagnostics = message
                app._show_diagnostics()
                window = next(child for child in root.winfo_children() if isinstance(child, tk.Toplevel))
                button = next(child for child in window.winfo_children() if isinstance(child, app.ttk.Button))
                for size in ('480x280', '780x460'):
                    window.geometry(size)
                    root.update()
                    self.assertTrue(button.winfo_ismapped())
                    self.assertGreaterEqual(button.winfo_height(), button.winfo_reqheight())
                    self.assertLessEqual(button.winfo_y() + button.winfo_height(), window.winfo_height())
                    panel = next(child for child in window.winfo_children() if isinstance(child, tk.Frame))
                    content = next(child for child in panel.winfo_children() if isinstance(child, tk.Text))
                    self.assertIn(message, content.get('1.0', 'end-1c'))
                app.closed = True
            finally:
                for timer in root.tk.call('after', 'info'):
                    root.after_cancel(timer)
                root.destroy()

    def test_controls_fit_at_minimum_size_with_selected_audio_and_scaled_fonts(self):
        import tkinter as tk
        for dpi in (96, 120, 144, 192):
            with self.subTest(dpi=dpi), tempfile.TemporaryDirectory() as directory:
                runtime=Path(directory)
                root=tk.Tk()
                root.tk.call('tk','scaling',dpi/72)
                try:
                    with patch.object(desktop,'runtime_dir',return_value=runtime), \
                            patch.object(desktop,'ServiceController',side_effect=lambda port:ServiceController(port,runtime=runtime)), \
                            patch.object(desktop,'health',return_value=None):
                        app=desktop.DesktopApplication(root)
                    app._sample()
                    providers=('wechat','wetype','doubao')
                    state={'status':'ok','active_requests':12,'queued_requests':1,
                           'pools':[{'provider':key,'max_concurrency':4,'occupied_slots':list(range(4))} for key in providers]}
                    app._show_health(app.controller,(state,True))
                    self.assertIn('运行 12',app.pool_summary.get())
                    self.assertNotIn('/',app.pool_summary.get())
                    for view in app.pool_views.values():
                        canvas=view['canvas']
                        self.assertEqual(sum(canvas.itemcget(item,'fill')=='#22954E' for item in canvas.find_all()),4)
                    app.concurrency['wechat']=108
                    app._show_health(app.controller, (None, False))
                    self.assertEqual(len(app.pool_views['wechat']['canvas'].find_all()),108)
                    self.assertEqual([app.notebook.tab(tab,'text') for tab in app.notebook.tabs()],['首页','手动转写'])
                    with patch.object(desktop.webbrowser,'open') as browser:
                        app._open_docs()
                        self.assertTrue(browser.call_args.args[0].startswith('file:///'))
                        self.assertIn('api.html?port=8765',browser.call_args.args[0])
                        app.connected=True
                        app._open_docs()
                        self.assertEqual(browser.call_args.args[0],'http://127.0.0.1:8765/docs?port=8765')
                        app.connected=False
                    app.detail.set('已连接现有服务，此窗口不会停止它。')
                    app._update_buttons()
                    root.update_idletasks()
                    width,height=root.minsize()
                    for window_width,window_height in ((width,height),(max(width,1080),height+300)):
                        root.geometry(f'{window_width}x{window_height}')
                        root.update_idletasks()
                        inspect_layout(app)
                        app.notebook.select(0)
                        root.update()
                        self.assertFalse(app.result.winfo_ismapped())
                        self.assertTrue(app.pool_views['wechat']['canvas'].winfo_ismapped())
                    app.closed=True
                finally:
                    for timer in root.tk.call('after','info'):
                        root.after_cancel(timer)
                    root.destroy()


if __name__=='__main__':unittest.main(verbosity=2)
