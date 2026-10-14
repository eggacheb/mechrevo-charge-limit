# -*- coding: utf-8 -*-
"""安装 / 卸载「开机启动守护」计划任务（SYSTEM 身份，登录前执行）。

为什么用计划任务，而不是启动文件夹：
  - 启动文件夹里的项是**用户登录之后**由资源管理器依次拉起的，顺序不可控；
    计划任务的「启动时」触发器在用户登录**之前**就会执行 —— 这是最早的时机。
  - 守护是无界面程序，以 SYSTEM 运行不需要用户会话。
  - 可以「使用最高权限运行」，且完全不受 UAC 影响。

为什么守护必须能脱离用户环境变量：
  以 SYSTEM 运行时没有 LOCALAPPDATA。pin_guard.py 因此改为优先用**自身所在目录**
  定位配置，找不到配置时也绝不动寄存器（见其 _resolve_cfg_dir 与 _tick）。

用法：
    python install_boot_task.py            # 安装（自动请求提权）
    python install_boot_task.py --remove   # 卸载
    python install_boot_task.py --status   # 查看
"""
import ctypes
import os
import subprocess
import sys

TASK_NAME = "MechrevoChargeLimitGuard"
BASE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(BASE, "pin_guard.py")
CREATE_NO_WINDOW = 0x08000000


def pythonw_exe():
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_ps(script):
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", script],
        capture_output=True, timeout=180, creationflags=CREATE_NO_WINDOW,
    )
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace")
    return r.returncode, (out + err).strip()


PS_INSTALL = r"""
$ErrorActionPreference = 'Stop'
$py  = '{py}'
$gd  = '{gd}'
$wd  = '{wd}'
$act = New-ScheduledTaskAction -Execute $py -Argument ('"' + $gd + '"') -WorkingDirectory $wd
$trg = New-ScheduledTaskTrigger -AtStartup
$pri = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
       -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
       -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
       -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName '{name}' -Action $act -Trigger $trg -Principal $pri `
       -Settings $set -Description 'EC charge-limit pin guard: re-pins the charge gate at boot, before user logon.' `
       -Force | Out-Null
$t = Get-ScheduledTask -TaskName '{name}'
$i = Get-ScheduledTaskInfo -TaskName '{name}'
'TASKNAME=' + $t.TaskName
'STATE=' + $t.State
'TRIGGER=' + ($t.Triggers | ForEach-Object {{ $_.CimClass.CimClassName }})
'USER=' + $t.Principal.UserId + ' / ' + $t.Principal.RunLevel
'ACTION=' + $t.Actions[0].Execute + ' ' + $t.Actions[0].Arguments
'LAST_RESULT=' + $i.LastTaskResult
"""

PS_REMOVE = r"""
$ErrorActionPreference = 'Stop'
$t = Get-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue
if ($t) {{ Unregister-ScheduledTask -TaskName '{name}' -Confirm:$false; 'REMOVED' }} else {{ 'NOT_PRESENT' }}
"""

PS_STATUS = r"""
$t = Get-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue
if (-not $t) {{ 'NOT_PRESENT'; exit 0 }}
$i = Get-ScheduledTaskInfo -TaskName '{name}'
'TASKNAME=' + $t.TaskName
'STATE=' + $t.State
'USER=' + $t.Principal.UserId + ' / ' + $t.Principal.RunLevel
'LAST_RUN=' + $i.LastRunTime
'LAST_RESULT=' + $i.LastTaskResult
'NEXT_RUN=' + $i.NextRunTime
"""


def relaunch_admin():
    """以管理员身份重跑本脚本。"""
    params = '"%s"' % os.path.abspath(__file__)
    for a in sys.argv[1:]:
        params += " " + a
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    return rc > 32


def main():
    args = [a.lower() for a in sys.argv[1:]]

    # --status 是只读的，先处理，免得为了看一眼状态还弹 UAC
    if "--status" in args:
        rc, out = run_ps(PS_STATUS.format(name=TASK_NAME))
        print(out)
        return 0 if rc == 0 else 1

    if not is_admin():
        print("需要管理员权限，正在请求提权…（会弹一次 UAC）")
        if relaunch_admin():
            return 0
        print("提权被拒绝。请右键以管理员身份运行，或在「任务计划程序」里手动创建。")
        return 1

    if "--remove" in args:
        rc, out = run_ps(PS_REMOVE.format(name=TASK_NAME))
        print(out)
        return 0 if rc == 0 else 1

    if not os.path.exists(GUARD):
        print("找不到 pin_guard.py：%s" % GUARD)
        return 1

    rc, out = run_ps(PS_INSTALL.format(
        py=pythonw_exe().replace("'", "''"),
        gd=GUARD.replace("'", "''"),
        wd=BASE.replace("'", "''"),
        name=TASK_NAME,
    ))
    print(out)
    if rc != 0:
        print("安装失败。")
        return 1

    print()
    print("已安装。该任务会在**开机时、用户登录之前**以 SYSTEM 身份运行守护，")
    print("并带「最高权限」；电池供电时也会启动，异常退出会自动重试 3 次。")
    print()
    print("提示：这个守护由 SYSTEM 运行，普通权限的界面无法结束它 ——")
    print("      要停就在「任务计划程序」里禁用此任务，或用界面上的「取消限充」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
