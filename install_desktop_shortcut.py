# -*- coding: utf-8 -*-
"""在桌面创建带图标的快捷方式。

用法：
    py install_desktop_shortcut.py           # 创建
    py install_desktop_shortcut.py --remove  # 删除

依赖 pywin32（仅本脚本需要；守护与界面本身不需要它）。
"""
import os
import sys

LNK_NAME = "电池充电上限.lnk"


def desktop_dir():
    try:
        import pythoncom
        from win32com.shell import shell, shellcon
        return shell.SHGetFolderPath(0, shellcon.CSIDL_DESKTOPDIRECTORY, None, 0)
    except Exception:
        return os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")


def pythonw_exe():
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(cand):
        return cand
    return exe


def install():
    try:
        import pythoncom
        from win32com.shell import shell
    except Exception as exc:
        print("需要 pywin32：", exc)
        print("安装：", sys.executable, "-m pip install pywin32")
        return 1

    here = os.path.dirname(os.path.abspath(__file__))
    target = os.path.join(here, "电池充电上限.pyw")
    icon = os.path.join(here, "battery_limit.ico")
    if not os.path.exists(target):
        print("找不到主程序：", target)
        return 1

    lnk = os.path.join(desktop_dir(), LNK_NAME)
    link = pythoncom.CoCreateInstance(
        shell.CLSID_ShellLink, None,
        pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
    link.SetPath(pythonw_exe())
    link.SetArguments('"%s"' % target)
    link.SetWorkingDirectory(here)
    if os.path.exists(icon):
        link.SetIconLocation(icon, 0)
    link.SetDescription("电池充电上限 · 机械革命 蛟龙16QS")
    link.QueryInterface(pythoncom.IID_IPersistFile).Save(lnk, 0)

    print("已创建：", lnk)
    print("  目标：", pythonw_exe(), '"%s"' % target)
    print("  图标：", icon if os.path.exists(icon) else "(默认)")
    return 0


def remove():
    lnk = os.path.join(desktop_dir(), LNK_NAME)
    if os.path.exists(lnk):
        os.remove(lnk)
        print("已删除：", lnk)
    else:
        print("未找到：", lnk)
    return 0


if __name__ == "__main__":
    if "--remove" in sys.argv:
        sys.exit(remove())
    sys.exit(install())
