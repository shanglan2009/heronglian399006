"""Reversible, current-user startup plus a weekday 09:25 Windows task."""
import winreg,subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

TASK='Heronglian-Market-Monitor'
REGISTRY=r'Software\Microsoft\Windows\CurrentVersion\Run'

def task_owned(exe):
    r=subprocess.run(['schtasks','/Query','/TN',TASK,'/XML'],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode:return False
    raw=r.stdout
    if raw.startswith((b'\xff\xfe',b'\xfe\xff')) or raw[:100].count(b'\0')>5:
        document=raw.decode('utf-16')
    else:
        try:document=raw.decode('utf-8-sig')
        except UnicodeDecodeError:document=raw.decode('mbcs')
    # schtasks can declare UTF-16 while writing redirected output in the active code page.
    root=ET.fromstring(document)
    commands=[e.text for e in root.iter() if e.tag.endswith('}Command') or e.tag=='Command']
    if len(commands)!=1 or Path(commands[0].strip('"')).resolve()!=exe:raise ValueError('同名计划任务指向其他程序，未修改；请人工核对')
    return True

def configure(exe,enabled):
    exe=Path(exe).resolve()
    if not exe.is_file():raise ValueError('应用文件不存在')
    command=subprocess.list2cmdline([str(exe),'--background'])
    owned=task_owned(exe)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,REGISTRY) as key:
        try:
            previous,_=winreg.QueryValueEx(key,'HeronglianMonitor')
            if previous!=command:raise ValueError('同名登录启动项指向其他程序，未修改')
        except FileNotFoundError:pass
        if enabled:winreg.SetValueEx(key,'HeronglianMonitor',0,winreg.REG_SZ,command)
        else:
            try:winreg.DeleteValue(key,'HeronglianMonitor')
            except FileNotFoundError:pass
    args=['schtasks','/Create','/TN',TASK,'/SC','WEEKLY','/D','MON,TUE,WED,THU,FRI','/ST','09:25','/TR',command,'/IT']+(['/F'] if owned else []) if enabled else ['schtasks','/Delete','/TN',TASK,'/F']
    result=subprocess.run(args,capture_output=True,stdin=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW) if enabled or owned else None
    task_ok=bool(enabled and result and result.returncode==0 and task_owned(exe)) if enabled else bool(owned and result and result.returncode!=0)
    if enabled and task_ok:
        # Modify only this verified task; do not change the computer's sleep or power policy.
        script="Set-ScheduledTask -TaskName 'Heronglian-Market-Monitor' -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)) | Out-Null"
        tuned=subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',script],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
        if tuned.returncode:
            return dict(autostart=True,startup_task=True,message='登录及定时启动已登记；任务电池/补启动条件未设置成功，请在任务计划程序核对。')
    return dict(autostart=bool(enabled),startup_task=task_ok,
                message=('登录自启和工作日09:25启动任务均已开启；节假日由应用日历跳过。' if task_ok else '已开启登录自启；工作日定时启动登记未成功，请保持托盘运行。') if enabled else ('已关闭自动启动。' if not result or result.returncode==0 else '已关闭登录自启，但定时任务删除失败，请在任务计划程序中检查 '+TASK))
