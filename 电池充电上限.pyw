# -*- coding: utf-8 -*-
"""电池充电上限 — 机械革命 蛟龙16QS (GM6XG0Q) 适配版

原理（同方/Uniwill EC）：
  1) 写 0x07B9 = <上限%>              设置实时上限
  2) 把门控寄存器 0x07C3 常置为 0x04   —— 控制环路才会真正接管充电
  3) 存储区 0x087F 会一并写入目标值，但**本机型固件不采信存储值维持门控**，
     所以 0x07C3 必须一直保持在 0x04。
  取消：0x07B9 写 0，并把 0x07C3 恢复到出厂原值 0x07（释放门控）。

本机型与上游实测机型（无界15X）的关键差异：
  * 无界15X：存储触发一次写入即可永久生效，固件自行维持门控。
  * 蛟龙16QS：存储值**能跨重启保持**（实测 0x087F 重启后仍为 80），
    但固件**不会**用它维持门控 —— 实测电池会一路冲过 80% 直到 100%。
    唯一有效做法是让 0x07C3 保持 0x04（"钉扎"）。
  * 0x07C3 位于普通 EC RAM，**重启即丢**，因此必须由开机守护重放
    （见 pin_guard.py）。这也就是"钉扎易失"这个代价的来源。

正确性判据：
  * 0x0742 bit2 = 1 才是"控制环路已激活"的可靠信号（钉扎成功后 5 秒内变 1，
    同时电池电流归零）。存储写入成功**不代表**限充生效。
  * 成功判据 = 实时上限回读 == 目标值 + 平台寄存器 0x07C3 已确认写入 0x04。

安全约束：
  * 绝不写 0x0770（ROM ID 身份字节）
  * 平台寄存器 0x07C3 的写入会做回读校验；异常路径有兜底恢复
  * 若发现 0x07C3 停在 4/5（上次异常残留），按已知出厂原值 0x07 修复
"""
import ctypes
import json
import os
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk

IOCTL_EC_READ = 0x9C40A488
IOCTL_EC_WRITE = 0x9C40A48C
DEVICE_PATHS = ["\\\\.\\ACPIDriver", "\\\\.\\ACPIH"]
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_RW = 0x3
OPEN_EXISTING = 3

REG_ROMID = 0x0770
REG_PLATFORM = 0x07C3
REG_GATE = 0x0742
REG_LIMIT = 0x07B9

STORE_MAGIC = 0x04        # 临时值：让固件门控条件成立
STORE_HOLD_SEC = 2.5      # 保持时长：给固件控制循环（每秒一次）留足时间
PLATFORM_DEFAULT = 0x07   # 本机出厂原值（已探测确认）

LIMIT_CHOICES = [50, 60, 70, 80, 90, 100]

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.DeviceIoControl.restype = ctypes.c_bool
kernel32.CloseHandle.restype = ctypes.c_bool
# HANDLE 是 64 位，必须显式声明，否则默认 c_int 会截断句柄
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
kernel32.WaitForSingleObject.restype = ctypes.c_uint32
kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def open_driver():
    for path in DEVICE_PATHS:
        h = kernel32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE,
                                 FILE_SHARE_RW, None, OPEN_EXISTING, 0, None)
        if h and h != ctypes.c_void_p(-1).value:
            return h
    return None


def ec_read(h, addr):
    inb = (ctypes.c_ubyte * 4).from_buffer_copy(struct.pack("<I", addr))
    outb = (ctypes.c_ubyte * 4)()
    ret = ctypes.c_ulong(0)
    if not kernel32.DeviceIoControl(h, IOCTL_EC_READ, inb, 4, outb, 4, ctypes.byref(ret), None):
        raise OSError("read failed addr=%#06x" % addr)
    return struct.unpack("<I", bytes(outb))[0] & 0xFF


def ec_write(h, addr, val):
    inb = (ctypes.c_ubyte * 8).from_buffer_copy(struct.pack("<II", addr, val))
    outb = (ctypes.c_ubyte * 4)()
    ret = ctypes.c_ulong(0)
    if not kernel32.DeviceIoControl(h, IOCTL_EC_WRITE, inb, 8, outb, 4, ctypes.byref(ret), None):
        raise OSError("write failed addr=%#06x" % addr)
    return True


def power_status():
    sps = SYSTEM_POWER_STATUS()
    if not kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
        return None
    return {
        "pct": None if sps.BatteryLifePercent == 255 else sps.BatteryLifePercent,
        "ac": sps.ACLineStatus == 1,
        "charging": bool(sps.BatteryFlag & 8) and sps.BatteryFlag != 255,
    }


def platform_original(h):
    """返回应当恢复的平台值，以及当前是否处于钉扎状态。

    钉扎状态下 0x07C3 本身就是 0x04，此时要恢复到出厂原值。
    """
    cur = ec_read(h, REG_PLATFORM)
    if cur in (STORE_MAGIC, 5):
        return PLATFORM_DEFAULT, True
    return cur, False


# ---------- 配置：供开机守护 (pin_guard.py) 读取 ----------
CFG_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "MechrevoChargeLimit")
CFG_PATH = os.path.join(CFG_DIR, "setting.json")
HEARTBEAT_PATH = os.path.join(CFG_DIR, "guard_heartbeat.json")


def read_setting():
    """读取守护配置。返回 dict（读不到时返回空 dict）。"""
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_setting(enabled, limit, original_value):
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        data = {
            "enabled": bool(enabled),
            "limit": int(limit),
            "platform_original": int(original_value),
        }
        tmp = CFG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, CFG_PATH)
        return True, None
    except Exception as exc:
        return False, str(exc)


def _pid_alive(pid):
    """判断 PID 是否还在运行。

    返回 True(在跑) / False(已退出) / None(权限不足，无法判定)。
    """
    if not pid:
        return False
    try:
        SYNCHRONIZE = 0x00100000
        h = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not h:
            # 5 = ERROR_ACCESS_DENIED：对方权限更高（例如守护是提权跑的），
            # 无权判定存活，返回 None 交给心跳时间戳兜底，避免误报“未运行”。
            return None if ctypes.get_last_error() == 5 else False
        rc = kernel32.WaitForSingleObject(h, 0)
        kernel32.CloseHandle(h)
        return rc == 0x00000102          # WAIT_TIMEOUT => 仍在运行
    except Exception:
        return None


def guard_status():
    """返回 (守护是否在运行, 上次心跳距今秒数, 心跳内容)。

    只看心跳时间戳会**误报**：守护被外部结束后心跳文件仍留在磁盘上，
    在 90 秒新鲜期内界面照样显示“运行中”。所以这里同时校验 PID 是否真的存活。
    """
    try:
        with open(HEARTBEAT_PATH, "r", encoding="utf-8") as f:
            hb = json.load(f)
    except Exception:
        return False, None, {}
    try:
        age = time.time() - float(hb.get("time", 0) or 0)
    except Exception:
        age = 1e9
    try:
        interval = float(hb.get("interval", 30) or 30)
    except Exception:
        interval = 30.0
    fresh = age < max(90.0, interval * 3)
    pid_ok = _pid_alive(hb.get("pid"))
    alive = (pid_ok is not False) and fresh
    return alive, age, hb


def _pythonw_exe():
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(cand):
        return cand
    return exe


def guard_script_path():
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "pin_guard.py"),
                 os.path.join(here, "tools", "pin_guard.py")):
        if os.path.exists(cand):
            return cand
    return os.path.join(here, "pin_guard.py")


DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def start_guard():
    """以后台方式拉起守护进程。返回 (成功?, 错误)。

    用 DETACHED_PROCESS 脱离控制台；并优先加 CREATE_BREAKAWAY_FROM_JOB
    脱离调用方所在的 Job 对象 —— 否则 GUI 一退出（点「以管理员身份重启」、
    从托盘退出、或被父进程清理），守护会被 Job 连带杀掉。Job 不允许脱离时
    自动降级为不带该标志启动。
    """
    script = guard_script_path()
    if not os.path.exists(script):
        return False, "未找到 pin_guard.py（应与本程序在同一目录）"
    base = DETACHED_PROCESS | CREATE_NO_WINDOW
    last = None
    for extra in (CREATE_BREAKAWAY_FROM_JOB, 0):
        try:
            subprocess.Popen(
                [_pythonw_exe(), script], cwd=os.path.dirname(script),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=base | extra,
            )
            return True, None
        except OSError as exc:
            last = exc
    return False, str(last)


def stop_guard():
    """结束守护并清掉心跳文件。返回 (成功?, 错误)。

    守护可能已经被外部结束（心跳是陈旧的）：这种情况不算失败，直接清掉
    心跳即可，避免界面弹出莫名其妙的“停止失败”。
    """
    _, _, hb = guard_status()
    pid = hb.get("pid") if hb else None
    if pid and _pid_alive(pid) is not False:
        try:
            r = subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, timeout=10,
                               creationflags=CREATE_NO_WINDOW)
            if r.returncode != 0:
                # 进程恰好在此期间退出也会返回非零，只要已不在就算成功
                if _pid_alive(pid) is not False:
                    return False, "taskkill 返回 %d" % r.returncode
        except Exception as exc:
            return False, str(exc)
    try:
        os.remove(HEARTBEAT_PATH)
    except OSError:
        pass
    return True, None


# ---------- 系统托盘 ----------
HERE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_ICON = os.path.join(HERE_DIR, "battery_limit.ico")


def load_tray_image():
    """返回托盘图标（PIL Image）；缺库返回 None，缺图标时现场画一个。"""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    if os.path.exists(APP_ICON):
        try:
            return Image.open(APP_ICON)
        except Exception:
            pass
    img = Image.new("RGBA", (64, 64), (37, 99, 235, 255))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([20, 8, 44, 56], radius=6, outline=(255, 255, 255, 255), width=4)
    d.rounded_rectangle([26, 24, 38, 50], radius=3, fill=(52, 199, 123, 255))
    return img


def make_tray(app):
    """创建托盘图标对象；pystray 不可用时返回 None（此时关窗即退出）。"""
    try:
        import pystray
        image = load_tray_image()
        if image is None:
            return None

        def _toggle(icon, item):
            if getattr(app, "_hidden", False):
                app.after(0, app.show_window)
            else:
                app.after(0, app.hide_window)

        def _quit(icon, item):
            icon.stop()
            app.after(0, app.quit_app)

        menu = pystray.Menu(
            pystray.MenuItem("显示 / 隐藏窗口", _toggle, default=True),
            pystray.MenuItem("退出", _quit),
        )
        return pystray.Icon("mechrevo-charge-limit", image, "电池充电上限", menu)
    except Exception:
        # 托盘建不起来时退回“关窗即退出”，不能因此让程序崩掉
        return None


def apply_limit(limit):
    """设置 / 取消充电上限。返回 (成功?, 说明)。

    本机型要点：门控 0x07C3 必须**常驻 0x04**，限充才会工作 ——
    固件不会用存储值 0x087F 维持门控（这点与上游实测的无界15X 不同）。
    钉扎值在普通 EC RAM 里，重启即丢，由开机守护重放。

    取消时反向操作：先清 0x07B9，再让门控短暂开一下把存储值也清成 0，
    最后把 0x07C3 释放回出厂原值。
    """
    h = open_driver()
    if h is None:
        return False, "无法打开 EC 通道（未找到 UWACPIDriver 设备）"
    try:
        target = 0x00 if limit >= 100 else (limit & 0x7F)
        original, _ = platform_original(h)

        # 1) 先写实时上限并回读
        ec_write(h, REG_LIMIT, target)
        time.sleep(0.2)
        live = ec_read(h, REG_LIMIT) & 0x7F
        if live != target:
            return False, "实时上限回读为 %d%%，与目标 %d%% 不符，已中止" % (live, target)

        # 2) 开门控：设置时钉住不放，取消时用完即还
        ec_write(h, REG_PLATFORM, STORE_MAGIC)
        time.sleep(0.25)
        if ec_read(h, REG_PLATFORM) != STORE_MAGIC:
            try:
                ec_write(h, REG_PLATFORM, original)
            except OSError:
                pass
            return False, "门控寄存器写入未生效，已回滚"

        time.sleep(STORE_HOLD_SEC - 0.25)   # 顺带让固件把存储值也更新一遍

        if limit >= 100:
            ec_write(h, REG_PLATFORM, original)
            time.sleep(0.6)
            now = ec_read(h, REG_PLATFORM)
            if now != original:
                return False, "平台值恢复失败，当前 %#04x —— 请手动检查！" % now
            if ((ec_read(h, REG_GATE) >> 2) & 1) != 0:
                return False, "上限已清，但门控未回落，请再点一次"
            return True, "已取消限充，门控已释放，电池恢复充满"

        # 3) 校验钉扎确实生效
        live = ec_read(h, REG_LIMIT) & 0x7F
        gate = (ec_read(h, REG_GATE) >> 2) & 1
        romid = ec_read(h, REG_ROMID)
        if live != target:
            return False, "钉扎后上限变为 %d%%，未达到 %d%%" % (live, target)
        if gate != 1:
            return False, "门控未置位，限充不会生效，请重试一次"
        if romid == STORE_MAGIC:
            return False, "身份字节 0x0770 被改动（%#04x），请立即恢复！" % romid
        return True, "已设为 %d%%，门控已钉扎（重启后由守护自动重放）" % limit
    except OSError as exc:
        return False, "EC 操作失败：%s" % exc
    finally:
        kernel32.CloseHandle(h)



def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# 固件 WMI 邮箱读取（只有管理员权限才能取到实例）。
# 这是唯一能读到存储区 0x087F 的通道 —— H2RAM 窗口到不了那里。
_PS_READ = (
    "$ErrorActionPreference='Stop';"
    # AcpiTest_MULong 有 10 个实例，必须只取一个 —— 否则对数组调方法会触发
    # 成员枚举，返回值变成 Object[]，后续 [uint64] 转换就炸。
    "$m=@(Get-WmiObject -Namespace root/WMI -Class AcpiTest_MULong)[0];"
    "$hi=[uint64]256*[uint64]4294967296;"
    "$d=$hi -bor [uint64]{addr};"
    "$r=$m.GetSetULong($d);"
    "if($r -is [System.Object[]]){{$r=$r[0]}};"
    "if($r -is [System.Management.ManagementBaseObject]){{$r=$r.Return}};"
    "[uint64]$r"
)


def _run_ps(ps, timeout=25):
    """执行 PowerShell 片段。返回 (stdout, stderr, 返回码)。

    注意：中文 Windows 上 PowerShell 输出是 GBK，若直接 text=True 按 UTF-8 解码
    会抛 UnicodeDecodeError —— 所以这里自己按字节取，再容错解码。
    """
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", ps],
            capture_output=True, timeout=timeout,
            creationflags=0x08000000,   # CREATE_NO_WINDOW
        )
    except Exception as exc:
        return "", str(exc), -1

    def dec(b):
        if not b:
            return ""
        for enc in ("utf-8", "gbk"):
            try:
                return b.decode(enc)
            except Exception:
                pass
        return b.decode("utf-8", "replace")

    return dec(p.stdout), dec(p.stderr), p.returncode


def wmi_read(addr):
    """经固件邮箱读一个 EC 字节。返回 (值, 错误)。需要管理员。"""
    if not is_admin():
        return None, "需要管理员权限"
    out, err_out, rc = _run_ps(_PS_READ.format(addr=hex(addr)))
    if rc != 0:
        detail = (err_out or out or "powershell 返回非零").strip().replace("\n", " ")
        return None, detail[:160]
    lines = [x for x in out.splitlines() if x.strip()]
    if not lines:
        return None, "无输出"
    try:
        return int(lines[-1].strip()) & 0xFF, None
    except ValueError:
        return None, ("无法解析输出：%s" % lines[-1].strip())[:160]


def read_stored_limit():
    """读固件存储上限 (0x087F)。返回 (百分比 或 None, 错误)。"""
    v, err = wmi_read(0x087F)
    if err:
        return None, err
    return (v & 0x7F), None


def relaunch_as_admin():
    """以管理员身份重启本程序。"""
    try:
        exe = sys.executable
        params = '"%s"' % os.path.abspath(__file__)
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        return rc > 32, rc
    except Exception as exc:
        return False, str(exc)


def read_state():
    st = {"error": None, "pct": None, "ac": None, "charging": None,
          "limit": None, "platform": None, "romid": None}
    ps = power_status()
    if ps:
        st.update(ps)
    h = open_driver()
    if h is None:
        st["error"] = "无法打开 EC 通道（未找到 UWACPIDriver 设备）"
        return st
    try:
        st["limit"] = ec_read(h, REG_LIMIT) & 0x7F
        st["platform"] = ec_read(h, REG_PLATFORM)
        st["romid"] = ec_read(h, REG_ROMID)
    except OSError as exc:
        st["error"] = "EC 读取失败：%s" % exc
    finally:
        kernel32.CloseHandle(h)
    return st


BG = "#f5f6f8"
FG = "#333333"
MUTED = "#777777"
OK = "#1a7f37"
WARN = "#b45309"
BAD = "#b91c1c"
CARD = "#ffffff"
BORDER = "#dcdde1"
ACCENT = "#2f6feb"
FONT = "Microsoft YaHei UI"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("电池充电上限 · 蛟龙16QS")
        self.configure(bg=BG)
        self.resizable(False, False)
        w = min(880, int(self.winfo_screenwidth() * 0.9))
        h = 700
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 3
        self.geometry("%dx%d+%d+%d" % (w, h, x, y))
        try:
            self.iconbitmap(APP_ICON)
        except Exception:
            pass
        self._busy = False
        self._flash = ("", FG)
        self._hidden = False
        self._tray = None
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()
        self.after(150, self.refresh)
        self.after(400, self._start_tray)
        self.after(900, self._ensure_guard_once)

    def _build(self):
        wrap = tk.Frame(self, bg=BG, padx=28, pady=16)
        wrap.pack(fill="both", expand=True)

        tk.Label(wrap, text="电池充电上限", bg=BG, fg=FG,
                 font=(FONT, 17, "bold")).pack(anchor="w")
        tk.Label(wrap, text="机械革命 蛟龙16QS (GM6XG0Q) · 本机型固件不采信存储值，需守护常驻钉扎（重启后自动重放）",
                 bg=BG, fg=MUTED, font=(FONT, 10)).pack(anchor="w", pady=(2, 8))

        perm = tk.Frame(wrap, bg=BG)
        perm.pack(fill="x", pady=(0, 12))
        self.admin = is_admin()
        if self.admin:
            tk.Label(perm, text="权限：管理员 · 可直接读取固件存储区做确定性验证",
                     bg=BG, fg=OK, font=(FONT, 9)).pack(side="left")
        else:
            tk.Label(perm, text="权限：普通 · 写入免提权；直读固件存储区需管理员",
                     bg=BG, fg=MUTED, font=(FONT, 9)).pack(side="left")
            tk.Button(perm, text="以管理员身份重启", font=(FONT, 9), relief="flat", bd=0,
                      cursor="hand2", bg="#e8e8ea", fg=FG, activebackground="#d8d8da",
                      command=self.on_elevate).pack(side="right", ipady=2, padx=(8, 0))

        guard_row = tk.Frame(wrap, bg=BG)
        guard_row.pack(fill="x", pady=(0, 10))
        self.guard_lbl = tk.Label(guard_row, text="", bg=BG, fg=MUTED,
                                  font=(FONT, 9), anchor="w")
        self.guard_lbl.pack(side="left")
        self.guard_btn = tk.Button(guard_row, text="启动守护", font=(FONT, 9), relief="flat",
                                   bd=0, cursor="hand2", bg="#e8e8ea", fg=FG,
                                   activebackground="#d8d8da", command=self.on_guard_toggle)
        self.guard_btn.pack(side="right", ipady=2)

        top = tk.Frame(wrap, bg=CARD, highlightbackground=BORDER,
                       highlightthickness=1, padx=20, pady=14)
        top.pack(fill="x")
        for c in (0, 2):
            top.grid_columnconfigure(c, minsize=92)
        for c in (1, 3):
            top.grid_columnconfigure(c, weight=1)
        self.vals = {}
        for r, (l1, k1, l2, k2) in enumerate([
                ("当前电量", "pct", "充电状态", "charge"),
                ("设定上限", "limit", "限充状态", "state")]):
            tk.Label(top, text=l1, bg=CARD, fg=MUTED, font=(FONT, 10)).grid(
                row=r, column=0, sticky="w", pady=4, padx=(0, 8))
            v1 = tk.Label(top, text="—", bg=CARD, fg=FG, font=(FONT, 13, "bold"))
            v1.grid(row=r, column=1, sticky="w", pady=4)
            tk.Label(top, text=l2, bg=CARD, fg=MUTED, font=(FONT, 10)).grid(
                row=r, column=2, sticky="w", pady=4, padx=(28, 8))
            v2 = tk.Label(top, text="—", bg=CARD, fg=FG, font=(FONT, 13, "bold"))
            v2.grid(row=r, column=3, sticky="w", pady=4)
            self.vals[k1], self.vals[k2] = v1, v2

        tk.Label(wrap, text="选择充电上限", bg=BG, fg=MUTED,
                 font=(FONT, 10)).pack(anchor="w", pady=(16, 7))
        row = tk.Frame(wrap, bg=BG)
        row.pack(fill="x")
        self.btns = {}
        for pct in LIMIT_CHOICES:
            b = tk.Button(row, text=("%d%%" % pct), width=7, font=(FONT, 10, "bold"),
                          relief="flat", bd=0, cursor="hand2",
                          command=lambda p=pct: self.on_apply(p))
            b.pack(side="left", padx=(0, 8), ipady=5)
            self.btns[pct] = b
        tk.Label(wrap, text="100% = 取消限充（恢复充满）",
                 bg=BG, fg=MUTED, font=(FONT, 9)).pack(anchor="w", pady=(5, 0))

        mid = tk.Frame(wrap, bg=BG, pady=14)
        mid.pack(fill="x")
        self.verdict = tk.Label(mid, text="", bg=BG, fg=FG,
                                font=(FONT, 14, "bold"), anchor="w", justify="left")
        self.verdict.pack(fill="x")
        self.detail = tk.Label(mid, text="", bg=BG, fg=MUTED, font=(FONT, 10),
                               anchor="w", justify="left", wraplength=790)
        self.detail.pack(fill="x", pady=(5, 0))

        bottom = tk.Frame(wrap, bg=BG)
        bottom.pack(side="bottom", fill="x")
        tk.Button(bottom, text="取消限充（恢复充满）", width=20, font=(FONT, 10),
                  relief="flat", bd=0, cursor="hand2", bg="#e8e8ea", fg=FG,
                  activebackground="#d8d8da", command=self.on_cancel).pack(side="left", ipady=4)
        if self.admin:
            tk.Button(bottom, text="复核固件存储值", width=16, font=(FONT, 10),
                      relief="flat", bd=0, cursor="hand2", bg="#e8e8ea", fg=FG,
                      activebackground="#d8d8da", command=self.on_verify).pack(side="left", ipady=4, padx=(8, 0))
        tk.Button(bottom, text="刷新", width=10, font=(FONT, 10), relief="flat", bd=0,
                  cursor="hand2", bg="#e8e8ea", fg=FG, activebackground="#d8d8da",
                  command=self.refresh).pack(side="right", ipady=4)

    def _run(self, limit):
        if self._busy:
            return
        self._busy = True
        try:
            self.verdict.config(text="正在写入固件，请稍候…", fg=MUTED)
            self.detail.config(text="写入过程中请不要关闭窗口。")
            self.update_idletasks()
            ok, msg = apply_limit(limit)

            if ok:
                # 写配置，供开机守护重放（这张“意图”必须留下来）
                saved, err = write_setting(limit < 100,
                                           0 if limit >= 100 else limit,
                                           PLATFORM_DEFAULT)
                if not saved:
                    msg += " · 配置写入失败（%s），重启后需手动重应用" % err

            if ok and self.admin:
                self.verdict.config(text="正在经固件邮箱复核存储区…", fg=MUTED)
                self.update_idletasks()
                val, err = read_stored_limit()
                want = 0 if limit >= 100 else limit
                if err:
                    msg += " · 存储区复核不可用（%s）" % err
                elif val == want:
                    msg += " · 存储区复核：%d%% ✔" % val
                else:
                    ok = False
                    msg = "写入后固件存储区为 %d%%，与目标 %d%% 不符，请重试" % (val, want)

            self._flash = (msg, OK if ok else BAD)
        finally:
            self._busy = False
            self.refresh()

    def on_apply(self, limit):
        self._run(limit)

    def on_cancel(self):
        self._run(100)

    def on_elevate(self):
        ok, info = relaunch_as_admin()
        if ok:
            self.destroy()
        else:
            self.verdict.config(text="⚠ 提权被拒绝或失败", fg=BAD)
            self.detail.config(text="ShellExecuteW 返回：%s" % info)

    def on_verify(self):
        if self._busy:
            return
        self._busy = True
        try:
            self.verdict.config(text="正在经固件邮箱读取存储区…", fg=MUTED)
            self.detail.config(text="需要管理员权限，可能一两秒。")
            self.update_idletasks()
            val, err = read_stored_limit()
            if err:
                self._flash = ("读取固件存储区失败：%s" % err, BAD)
            elif 1 <= val <= 100:
                self._flash = ("固件存储值 = %d%%，存储写入已确认" % val, OK)
            else:
                self._flash = ("固件存储值 = %d，当前无有效限充" % val, WARN)
        finally:
            self._busy = False
            self.refresh()

    def _start_tray(self):
        """托盘要跑在后台线程，否则会阻塞 Tk 主循环。"""
        self._tray = make_tray(self)
        if self._tray is not None:
            threading.Thread(target=self._tray.run, daemon=True).start()

    def hide_window(self):
        self._hidden = True
        self.withdraw()

    def show_window(self):
        self._hidden = False
        self.deiconify()
        self.lift()
        try:
            self.focus_force()
        except Exception:
            pass

    def on_close(self):
        """关闭窗口 = 缩到托盘；没有托盘时才真的退出。"""
        if self._tray is not None:
            self.hide_window()
        else:
            self.quit_app()

    def quit_app(self):
        try:
            if self._tray is not None:
                self._tray.stop()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass

    def _ensure_guard_once(self):
        """打开本工具时，若限充处于启用状态但守护没在跑，自动补上。

        守护可能因为开机自启被拦、被安全软件结束、或父进程被清理而消失；
        限充本身靠钉扎维持，但重启后需要守护重放，所以这里做一次自愈。
        注意：只在 setting.json 里 enabled=True 时才补 —— 「取消限充」会写
        enabled=False，此时不会强行拉起，尊重用户的关闭意图。
        """
        try:
            if not bool(read_setting().get("enabled")):
                return
            alive, _, _ = guard_status()
            if alive:
                return
            ok, _ = start_guard()
            if ok:
                self._flash = ("已自动启动限充守护（后台运行）", OK)
                self.after(1200, self.refresh)
        except Exception:
            pass

    def on_guard_toggle(self):
        if self._busy:
            return
        self._busy = True
        alive, _, _ = guard_status()
        if alive:
            ok, err = stop_guard()
            msg = "守护已停止" if ok else ("停止失败：%s" % err)
        else:
            ok, err = start_guard()
            msg = "守护已启动（后台运行）" if ok else ("启动失败：%s" % err)
        self._flash = (msg, OK if ok else BAD)
        self._busy = False
        self.verdict.config(text=("✔ " if ok else "⚠ ") + msg, fg=OK if ok else BAD)
        self.detail.config(text="守护在后台每 30 秒自检一次；开机时由启动项自动拉起。")
        self.after(1500, self.refresh)

    def refresh(self):
        alive, age, hb = guard_status()
        self.guard_btn.config(text="停止守护" if alive else "启动守护")
        if alive:
            self.guard_lbl.config(
                text="限充守护：运行中 · 目标 %s%% · 心跳 %s（重启后自动重放）"
                     % (hb.get("limit", "?"), hb.get("time_str", "?")), fg=OK)
        else:
            age_txt = "" if age is None else "（心跳 %.0f 秒前）" % age
            self.guard_lbl.config(
                text="限充守护：未运行%s —— 重启后钉扎会丢，点右侧按钮启动" % age_txt, fg=WARN)

        st = read_state()
        if st["error"]:
            self.verdict.config(text="⚠ 读取失败", fg=BAD)
            self.detail.config(text=st["error"])
            return

        pct = st["pct"]
        limit = st["limit"] or 0
        active = 0 < limit <= 100
        at_limit = active and pct is not None and pct >= limit

        for sel, b in self.btns.items():
            on = (not active) if sel >= 100 else (active and limit == sel)
            b.config(bg=ACCENT if on else "#e8e8ea",
                     fg="white" if on else FG,
                     activebackground=ACCENT if on else "#d8d8da",
                     activeforeground="white" if on else FG)

        self.vals["pct"].config(text=("%d%%" % pct) if pct is not None else "—")
        if active:
            self.vals["limit"].config(text="%d%%" % limit)
            self.vals["state"].config(text="已启用", fg=OK)
        else:
            self.vals["limit"].config(text="未限制")
            self.vals["state"].config(text="未启用", fg=WARN)

        if st["ac"] is False:
            charge_txt = "使用电池中"
        elif at_limit:
            charge_txt = "已停止（到上限）"
        elif st["charging"]:
            charge_txt = "充电中"
        else:
            charge_txt = "未充电"
        self.vals["charge"].config(text=charge_txt, fg=FG)

        flash, color = self._flash
        self._flash = ("", FG)
        if flash:
            self.verdict.config(text=("✔ " if color == OK else "⚠ ") + flash, fg=color)
            if color == OK:
                self.detail.config(
                    text="门控已钉扎、限充正在工作。钉扎值重启会丢，由开机守护自动重放（见上方守护状态）。")
            elif color == WARN:
                self.detail.config(
                    text="存储值不在 1–100 范围内，门控不会常开，电池会正常充满。")
            else:
                self.detail.config(
                    text="可点「复核固件存储值」，或用管理员模式重试写入。")
        elif not active:
            self.verdict.config(text="⚠ 当前未限制充电", fg=WARN)
            self.detail.config(text="电池会一直充到 100%。点上方的百分比即可启用限充。")
        elif at_limit:
            self.verdict.config(text="✔ 限充已生效（%d%%）" % limit, fg=OK)
            if st["ac"] is False:
                self.detail.config(
                    text="当前 %d%% 已在上限之上，正在使用电池。插上电源后不会充回 100%%，"
                         "可据此验证限充。" % pct)
            else:
                self.detail.config(
                    text="当前 %d%% 已到上限，插着电源也不会继续充。可直接拔电插电验证。" % pct)
        else:
            self.verdict.config(text="✔ 限充已设置（%d%%）" % limit, fg=OK)
            self.detail.config(
                text="当前 %d%% 低于上限，充电中；到 %d%% 会自动停。" % (pct, limit))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        s = read_state()
        print({k: s[k] for k in ("pct", "ac", "charging", "limit", "platform", "romid", "error")})
        sys.exit(0)
    App().mainloop()
