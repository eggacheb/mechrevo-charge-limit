# -*- coding: utf-8 -*-
"""电池限充守护 —— 机械革命 蛟龙16QS (GM6XG0Q)

为什么需要它：
  本机固件与无界15X 不同 —— 存储值 0x087F 能持久，但固件**不会**用存储值
  维持限充门控。门控条件（0x07C3 == 4）必须**一直成立**，而 0x07C3 位于
  普通 EC RAM，重启即丢。所以每次开机后必须重新钉扎一次。

本进程做的事（每 INTERVAL 秒一轮）：
  1. 读 %LOCALAPPDATA%\\MechrevoChargeLimit\\setting.json
  2. enabled 为真  -> 确保 0x07C3 = 0x04（钉扎）且 0x07B9 = 目标上限
     enabled 为假  -> 确保 0x07C3 回到出厂原值（解除钉扎）
  3. 写心跳文件，供 GUI 显示守护是否在运行

只写 0x07B9 与 0x07C3 两个寄存器，绝不碰 0x0770（ROM ID 身份字节）。
"""
import ctypes
import json
import os
import struct
import sys
import time

IOCTL_EC_READ = 0x9C40A488
IOCTL_EC_WRITE = 0x9C40A48C
DEVICE_PATHS = ("\\\\.\\ACPIDriver", "\\\\.\\ACPIH")
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_RW = 0x3
OPEN_EXISTING = 3

REG_ROMID = 0x0770
REG_PLATFORM = 0x07C3
REG_GATE = 0x0742
REG_LIMIT = 0x07B9
PIN_VALUE = 0x04
PLATFORM_DEFAULT = 0x07

CFG_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "MechrevoChargeLimit"
)
CFG_PATH = os.path.join(CFG_DIR, "setting.json")
HEARTBEAT_PATH = os.path.join(CFG_DIR, "guard_heartbeat.json")
LOG_PATH = os.path.join(CFG_DIR, "guard.log")

INTERVAL = float(os.environ.get("PIN_GUARD_INTERVAL", "30"))

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.DeviceIoControl.restype = ctypes.c_bool
kernel32.CloseHandle.restype = ctypes.c_bool


def log(msg):
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def open_driver():
    for path in DEVICE_PATHS:
        h = kernel32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_RW,
                                 None, OPEN_EXISTING, 0, None)
        if h and h != ctypes.c_void_p(-1).value:
            return h
    return None


def rd(h, addr):
    if h is None:
        return None
    inb = (ctypes.c_ubyte * 4).from_buffer_copy(struct.pack("<I", addr))
    outb = (ctypes.c_ubyte * 4)()
    ret = ctypes.c_ulong(0)
    if not kernel32.DeviceIoControl(h, IOCTL_EC_READ, inb, 4, outb, 4, ctypes.byref(ret), None):
        return None
    return struct.unpack("<I", bytes(outb))[0] & 0xFF


def wr(h, addr, val):
    if h is None:
        return False
    inb = (ctypes.c_ubyte * 8).from_buffer_copy(struct.pack("<II", addr, val))
    outb = (ctypes.c_ubyte * 4)()
    ret = ctypes.c_ulong(0)
    return bool(kernel32.DeviceIoControl(h, IOCTL_EC_WRITE, inb, 8, outb, 4, ctypes.byref(ret), None))


def read_cfg():
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if isinstance(cfg, dict):
            return cfg
    except Exception:
        pass
    return {}


def write_heartbeat(payload):
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        tmp = HEARTBEAT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, HEARTBEAT_PATH)
    except Exception:
        pass


def main():
    log("guard started (interval=%ss, pid=%s)" % (INTERVAL, os.getpid()))
    last_state = None
    while True:
        cfg = read_cfg()
        enabled = bool(cfg.get("enabled"))
        try:
            limit = int(cfg.get("limit", 80))
        except Exception:
            limit = 80
        try:
            original = int(cfg.get("platform_original", PLATFORM_DEFAULT))
        except Exception:
            original = PLATFORM_DEFAULT
        if limit < 1 or limit > 100:
            limit = 80
        if original in (PIN_VALUE, 5):
            original = PLATFORM_DEFAULT

        h = open_driver()
        action = "no-channel" if h is None else ""
        try:
            if h is not None:
                if enabled:
                    fixed = []
                    cur_lim = rd(h, REG_LIMIT)
                    if cur_lim is None or (cur_lim & 0x7F) != limit:
                        wr(h, REG_LIMIT, limit & 0x7F)
                        fixed.append("limit=%d" % limit)
                    if rd(h, REG_PLATFORM) != PIN_VALUE:
                        wr(h, REG_PLATFORM, PIN_VALUE)
                        fixed.append("gate=0x%02X" % PIN_VALUE)
                    action = "ensure:" + (",".join(fixed) if fixed else "ok")
                else:
                    if rd(h, REG_PLATFORM) != original:
                        wr(h, REG_PLATFORM, original)
                        action = "release:gate=0x%02X" % original
                    else:
                        action = "idle(disabled)"
        except Exception as exc:
            action = "error:%s" % exc
        finally:
            if h is not None:
                kernel32.CloseHandle(h)

        state = (enabled, limit, action)
        if state != last_state:
            log("enabled=%s limit=%s -> %s" % (enabled, limit, action))
            last_state = state

        write_heartbeat({
            "pid": os.getpid(),
            "time": time.time(),
            "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "enabled": enabled,
            "limit": limit,
            "action": action,
            "interval": INTERVAL,
        })
        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
