# -*- coding: utf-8 -*-
"""Battery charge-limit behavior monitor (read-only).

Every 5 s logs: battery %, AC state, charging flag, and EC registers read
through the UWACPIDriver channel (live limit / gate / battery current).

Reads only. NEVER writes any register.
"""
import ctypes
import datetime
import os
import struct
import sys
import time

IOCTL_EC_READ = 0x9C40A488
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_RW = 0x3
OPEN_EXISTING = 3

A_LIMIT = 0x07B9
A_GATE = 0x0742
A_CUR_LO = 0x0434
A_CUR_HI = 0x0435
A_REM_LO = 0x0436
A_REM_HI = 0x0437

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "battery_log.csv")

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


def power():
    sps = SYSTEM_POWER_STATUS()
    if not kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
        return None
    return {
        "pct": None if sps.BatteryLifePercent == 255 else sps.BatteryLifePercent,
        "ac": sps.ACLineStatus == 1,
        "charging": bool(sps.BatteryFlag & 8) and sps.BatteryFlag != 255,
    }


def main():
    h = open_driver()
    if h is None:
        print("ERROR: cannot open EC channel (\\\\.\\ACPIDriver). Is 机械革命电竞控制台 installed?")
        time.sleep(6)
        return 1

    new = not os.path.exists(LOG_PATH)
    log = open(LOG_PATH, "a", encoding="utf-8")
    if new:
        log.write("time,pct,ac,charging,limit_pct,gate_bit2,ec_current,ec_remain\n")
        log.flush()

    print("Battery charge-limit monitor - READ ONLY")
    print("Log file:", LOG_PATH)
    print("Press Ctrl+C to stop.\n")
    print("  time      pct  AC     charging  limit  gate  ec_current  note")
    print("  " + "-" * 72)

    prev_pct = None
    plateau_start = None

    try:
        while True:
            s = power()
            lim = rd(h, A_LIMIT)
            gate = rd(h, A_GATE)
            cur_lo = rd(h, A_CUR_LO)
            cur_hi = rd(h, A_CUR_HI)
            rem_lo = rd(h, A_REM_LO)
            rem_hi = rd(h, A_REM_HI)

            lim_pct = (lim & 0x7F) if lim is not None else -1
            gate_b = ((gate >> 2) & 1) if gate is not None else -1
            cur = None
            if cur_lo is not None and cur_hi is not None:
                cur = cur_lo | (cur_hi << 8)
            rem = None
            if rem_lo is not None and rem_hi is not None:
                rem = rem_lo | (rem_hi << 8)

            note = ""
            if s:
                if s["ac"] and not s["charging"] and lim_pct > 0 and s["pct"] is not None:
                    if s["pct"] > lim_pct:
                        note = "AC on, NOT charging, above limit -> limit likely ACTIVE"
                    else:
                        note = "AC on, NOT charging, below limit"
                if prev_pct is not None and s["ac"] and s["charging"]:
                    note = "charging up..."

            ts = datetime.datetime.now().strftime("%H:%M:%S")
            line = "{:8s}  {:>3}%  {:<5s}  {:<8s}  {:>5}  {:>4}  {:>10}   {}".format(
                ts,
                str(s["pct"]) if s and s["pct"] is not None else "?",
                "yes" if s and s["ac"] else "no",
                "yes" if s and s["charging"] else "no",
                (str(lim_pct) + "%") if lim_pct >= 0 else "?",
                gate_b if gate_b >= 0 else "?",
                str(cur) if cur is not None else "?",
                note,
            )
            print(line, flush=True)
            log.write("{},{},{},{},{},{},{},{}\n".format(
                datetime.datetime.now().isoformat(timespec="seconds"),
                s["pct"] if s else "", 1 if s and s["ac"] else 0,
                1 if s and s["charging"] else 0, lim_pct, gate_b,
                cur if cur is not None else "", rem if rem is not None else ""))
            log.flush()

            prev_pct = s["pct"] if s else None
            time.sleep(5)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        kernel32.CloseHandle(h)
        log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
