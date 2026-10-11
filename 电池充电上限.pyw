# -*- coding: utf-8 -*-
"""电池充电上限 — 机械革命 蛟龙16QS (GM6XG0Q) 适配版

原理（同方/Uniwill EC）：
  1) 写 0x07B9 = <上限%>                     设置实时上限
  2) 临时写 0x07C3 = 0x04，保持 2.5 秒再恢复原值
     —— 让固件门控条件短暂成立，固件会把实时上限抄进存储区 0x087F
  3) 之后由存储值自行维持，重启/关机/拔电源都不丢，无需后台程序
  取消：上限写 0，再走一次同样的存储触发。

本机型差异（与项目实测的无界15X 不同）：
  * 0x0742 bit2 只反映 0x07C3 / 0x0770 是否等于 4/5，
    不反映"存储维持"这条路径 —— 因此本工具不拿它当成功判据。
  * 成功判据 = 实时上限回读 == 目标值，且平台寄存器 0x07C3 已确认恢复原值。

安全约束：
  * 绝不写 0x0770（ROM ID 身份字节）
  * 平台寄存器 0x07C3 必须恢复原值；失败会重试，仍失败则明确报警
  * 若发现 0x07C3 停在 4/5（上次异常残留），按已知出厂原值 0x07 修复
"""
import ctypes
import os
import struct
import subprocess
import sys
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
    """返回应当恢复的平台值，以及是否为残留修复。"""
    cur = ec_read(h, REG_PLATFORM)
    if cur in (STORE_MAGIC, 5):
        return PLATFORM_DEFAULT, True     # 上次异常残留，按出厂原值修复
    return cur, False


def store_trigger(h):
    """让门控条件短暂成立，触发固件 store_limit()，随后恢复平台原值。

    返回 (成功?, 说明)。成功后 0x07C3 一定等于原值。

    每一步都做回读校验：
      写入 0x04 后必须读到 0x04（否则门控没开，固件不会 store_limit()，
      此时若照旧报"成功"，取消限充就会出现 live 清零而 stored 未清的错配）
    """
    original, was_stale = platform_original(h)
    last = "未知原因"
    try:
        for _ in range(3):
            # 1) 开门控前，先确保当前值就是准备恢复的原值
            if ec_read(h, REG_PLATFORM) != original:
                ec_write(h, REG_PLATFORM, original)
                time.sleep(0.2)

            # 2) 开门控，并确认真的写进去了
            ec_write(h, REG_PLATFORM, STORE_MAGIC)
            time.sleep(0.25)
            if ec_read(h, REG_PLATFORM) != STORE_MAGIC:
                last = "平台寄存器写入未生效"
                continue

            # 3) 保持，让固件控制循环执行 store_limit()
            time.sleep(STORE_HOLD_SEC - 0.25)

            # 4) 恢复原值并确认
            ec_write(h, REG_PLATFORM, original)
            time.sleep(0.4)
            if ec_read(h, REG_PLATFORM) == original:
                note = "（并修复了上次残留的平台值）" if was_stale else ""
                return True, "存储触发完成，平台寄存器已恢复 %#04x%s" % (original, note)
            last = "平台寄存器恢复未确认"
    finally:
        # 兜底：无论成功、失败还是抛异常，都不允许平台值停在 4/5
        try:
            if ec_read(h, REG_PLATFORM) in (STORE_MAGIC, 5):
                ec_write(h, REG_PLATFORM, original)
                time.sleep(0.2)
        except OSError:
            pass

    try:
        now = ec_read(h, REG_PLATFORM)
    except OSError:
        now = -1
    return False, ("平台寄存器 0x07C3 三次操作失败（%s），当前 %#04x —— 请手动检查！"
                   % (last, now))


def apply_limit(limit):
    """设置上限（100 表示取消限充）。返回 (成功?, 说明)。"""
    h = open_driver()
    if h is None:
        return False, "无法打开 EC 通道（未找到 UWACPIDriver 设备）"
    try:
        target = 0x00 if limit >= 100 else (limit & 0x7F)
        ec_write(h, REG_LIMIT, target)
        time.sleep(0.2)
        live = ec_read(h, REG_LIMIT) & 0x7F
        if live != target:
            return False, "实时上限回读为 %d%%，与目标 %d%% 不符，已中止" % (live, target)

        ok, msg = store_trigger(h)
        if not ok:
            return False, msg

        time.sleep(1.0)
        live = ec_read(h, REG_LIMIT) & 0x7F
        if live != target:
            return False, "存储触发后上限变为 %d%%，未达到 %d%%" % (live, target)

        if limit >= 100:
            return True, "已取消限充，电池恢复充满（固件存储值已清零）"
        return True, "已设为 %d%%，并写入固件存储区（重启/关机都不失效）" % limit
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
        h = 665
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 3
        self.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self._busy = False
        self._flash = ("", FG)
        self._build()
        self.after(150, self.refresh)

    def _build(self):
        wrap = tk.Frame(self, bg=BG, padx=28, pady=16)
        wrap.pack(fill="both", expand=True)

        tk.Label(wrap, text="电池充电上限", bg=BG, fg=FG,
                 font=(FONT, 17, "bold")).pack(anchor="w")
        tk.Label(wrap, text="机械革命 蛟龙16QS (GM6XG0Q) · 直接写入 EC 固件，跨重启保持",
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

    def refresh(self):
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
                    text="该值已写入固件存储区，重启、关机、拔电源都不会失效，也不需要任何后台程序。")
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
