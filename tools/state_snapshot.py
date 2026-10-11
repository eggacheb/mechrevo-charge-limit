# -*- coding: utf-8 -*-
"""Read-only EC state snapshot (no admin needed).

Prints the registers we care about plus battery percentage / AC state,
so we can compare before vs after a reboot.
"""
import ctypes
import datetime
import struct

IOCTL_EC_READ = 0x9C40A488
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_RW = 0x3
OPEN_EXISTING = 3

REGS = [
    (0x0740, "PROJECT_ID"),
    (0x0770, "ROMID[0]"),
    (0x07C3, "platform"),
    (0x07A6, "charge_mode"),
    (0x07B9, "live_limit"),
    (0x0742, "gate_status"),
]

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
    for path in ("\\\\.\\ACPIDriver", "\\\\.\\ACPIH"):
        h = kernel32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_RW,
                                 None, OPEN_EXISTING, 0, None)
        if h and h != ctypes.c_void_p(-1).value:
            return h
    return None


def rd(h, addr):
    inb = (ctypes.c_ubyte * 4).from_buffer_copy(struct.pack("<I", addr))
    outb = (ctypes.c_ubyte * 4)()
    ret = ctypes.c_ulong(0)
    if not kernel32.DeviceIoControl(h, IOCTL_EC_READ, inb, 4, outb, 4, ctypes.byref(ret), None):
        return None
    return struct.unpack("<I", bytes(outb))[0] & 0xFF


def main():
    print("=== EC state snapshot ===")
    print("time:", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    boot = datetime.datetime.now()
    sps = SYSTEM_POWER_STATUS()
    if kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
        print("battery pct :", sps.BatteryLifePercent if sps.BatteryLifePercent != 255 else "?")
        print("AC online   :", sps.ACLineStatus == 1)
        print("charging    :", bool(sps.BatteryFlag & 8) and sps.BatteryFlag != 255)

    sysup = ctypes.windll.kernel32.GetTickCount64() / 1000.0
    print("uptime      : %.0f s (%.1f min)  <- small value means freshly booted" % (sysup, sysup / 60.0))

    h = open_driver()
    if h is None:
        print("EC channel  : NOT AVAILABLE")
        return 1
    try:
        for addr, name in REGS:
            v = rd(h, addr)
            if v is None:
                print("  %-12s 0x%04X : read failed" % (name, addr))
                continue
            extra = ""
            if addr == 0x07B9:
                extra = " -> live limit %d%%" % (v & 0x7F)
            if addr == 0x0742:
                extra = " -> gate bit2=%d" % ((v >> 2) & 1)
            if addr == 0x07A6:
                extra = " -> mode bits[5:4]=%d" % ((v >> 4) & 3)
            print("  %-12s 0x%04X : 0x%02X%s" % (name, addr, v, extra))
    finally:
        kernel32.CloseHandle(h)
    print()
    print("NOTE: 0x087F (STORED limit) is in the H2RAM blind zone.")
    print("      Read it with the elevated WMI probe (read-ec-wmi.cmd).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
