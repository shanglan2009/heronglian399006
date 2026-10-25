import sys, threading, ctypes, queue, traceback
from pathlib import Path

ROOT=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(ROOT))
from app.service import Service
from app.api import API


def main():
    base=Path(sys.executable).parent if getattr(sys,'frozen',False) else ROOT
    if getattr(sys,'frozen',False):
        sys.stdout=sys.stderr=open(base/'application.log','a',encoding='utf-8',buffering=1)
    print('Starting native application',flush=True)
    if '--diagnose-data' in sys.argv:
        # Public-source diagnostics only; no account initialization or notifications.
        from app import capital_data,market
        for name,read in [('capital',lambda:capital_data.fetch(capital_data.HUIJIN)),('quotes',market.quotes)]:
            try:print('Data diagnostic',name,'OK',len(read()),flush=True)
            except Exception:traceback.print_exc()
        return
    ctypes.windll.kernel32.CreateMutexW.restype=ctypes.c_void_p
    mutex=ctypes.windll.kernel32.CreateMutexW(None,False,'Local\\HeronglianMonitor-v1')
    if ctypes.windll.kernel32.GetLastError()==183:
        if '--background' not in sys.argv:
            hwnd=ctypes.windll.user32.FindWindowW(None,'和融联 · 创业板投资观察')
            if hwnd:ctypes.windll.user32.ShowWindow(hwnd,9);ctypes.windll.user32.SetForegroundWindow(hwnd)
            else:ctypes.windll.user32.MessageBoxW(None,'应用正在启动，请稍后或从托盘打开。','和融联',0)
        return
    import webview,pystray
    from PIL import Image,ImageDraw
    service=Service(base/'runtime');api=API(service)
    print('Database ready',flush=True)
    window=webview.create_window('和融联 · 创业板投资观察',str(ROOT/'app/ui/index.html'),js_api=api,width=1320,height=900,min_size=(1000,700),hidden='--background' in sys.argv)
    quitting=threading.Event()
    logo=Image.new('RGB',(64,64),'#103e3d');draw=ImageDraw.Draw(logo);draw.line([(12,44),(26,33),(36,39),(52,18)],fill='#a7f0ce',width=6)
    def show(*args):window.show();window.restore()
    def quit_app(*args):
        quitting.set();service.stop.set();service.wake.set();tray.stop();window.destroy()
    tray=pystray.Icon('Heronglian',logo,'和融联 · 创业板观察',menu=pystray.Menu(pystray.MenuItem('打开应用',show,default=True),pystray.MenuItem('刷新行情',lambda:api.refresh()),pystray.MenuItem('退出监控',quit_app)))
    def closing():
        if not quitting.is_set():window.hide();return False
    window.events.closing+=closing
    def workers():
        print('Window initialized; starting monitoring',flush=True)
        threading.Thread(target=service.run,daemon=True).start()
        threading.Thread(target=tray.run,daemon=True).start()
        while not service.stop.is_set():
            try:
                title,body=service.notices.get(timeout=1)
                tray.notify(body[:220],title)
            except queue.Empty:pass
            except Exception:pass
    webview.start(workers,gui='edgechromium')


if __name__=='__main__':
    try:main()
    except Exception:
        base=Path(sys.executable).parent if getattr(sys,'frozen',False) else ROOT
        (base/'startup-error.txt').write_text(traceback.format_exc(),encoding='utf-8')
        ctypes.windll.user32.MessageBoxW(None,'启动失败，请查看 startup-error.txt。需要 Microsoft Edge WebView2 运行环境。','和融联',16)
