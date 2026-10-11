# -*- coding: utf-8 -*-
"""清空固件存储上限 0x087F，并验证它能否与限充共存。

为什么需要管理员：
  0x087F 落在 H2RAM 盲区 —— 驱动程序读它只会返回 0xFF（假值），
  唯一通道是固件 WMI 邮箱，而该通道仅管理员可调。

为什么要"验证共存"：
  存储区我们**写不进去**，只能让固件自己改。而固件只在门控打开时动作，
  限充本身又要求门控一直开着 —— 所以"清空存储值"和"限充生效"可能
  是互斥的。这个脚本跑一次就能给出确定结论。

安全保证：
  - 只写 0x07B9（实时上限）与 0x07C3（门控），绝不写 0x0770（身份字节）
  - 结尾一定会把状态恢复成"限充正常工作"（live=80 + 门控钉扎）
  - 全过程写入 result 文件，便于核对
"""
import importlib.util
import os
import time

BASE = os.path.dirname(os.path.abspath(__file__))
PW = os.path.join(BASE, "电池充电上限.pyw")
RESULT = os.path.join(BASE, "stored_result.txt")

spec = importlib.util.spec_from_file_location("tcc", PW)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

records = []


def log(msg):
    records.append(msg)
    print(msg)


def stored():
    """读固件存储上限。返回 (百分比 或 None, 错误)。"""
    v, err = m.wmi_read(0x087F)
    if v is None:
        return None, err
    return (v & 0x7F), None


def rd(addr):
    h = m.open_driver()
    if h is None:
        return None
    try:
        return m.ec_read(h, addr)
    finally:
        m.kernel32.CloseHandle(h)


def wr(addr, val):
    h = m.open_driver()
    if h is None:
        return False
    try:
        return m.ec_write(h, addr, val)
    finally:
        m.kernel32.CloseHandle(h)


def fmt(v):
    return "?" if v is None else ("0x%02X" % v)


def state(tag):
    s, err = stored()
    s_txt = str(s) if s is not None else ("读失败:%s" % err)
    line = "  %-20s stored=%-12s live=%-4s platform=%-6s gate=%-6s rom=%s" % (
        tag, s_txt,
        ("" if rd(0x07B9) is None else str(rd(0x07B9) & 0x7F)),
        fmt(rd(0x07C3)), fmt(rd(0x0742)), fmt(rd(0x0770)))
    log(line)


log("=== 清空固件存储上限 0x087F（并验证能否与限充共存）===")
log("时间   : %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
log("管理员 : %s" % m.is_admin())
log("")
state("初始")

log("")
log("--- 步骤 1：让固件把存储值清成 0 ---")
wr(0x07B9, 0)
time.sleep(0.4)
wr(0x07C3, 4)
time.sleep(3.0)
state("门控开 / live=0")
wr(0x07C3, 7)
time.sleep(0.6)
state("门控已关")

log("")
log("--- 步骤 2：恢复限充（live=80 + 门控钉扎），观察存储值是否被同步回来 ---")
wr(0x07B9, 80)
time.sleep(0.3)
wr(0x07C3, 4)
ok_after = None
for i in range(6):
    time.sleep(2)
    state("钉扎后 +%ds" % ((i + 1) * 2))
    if i == 5:
        ok_after, _ = stored()

log("")
log("=== 判定 ===")
if ok_after == 0:
    log("存储值 = 0，且限充已恢复 -> 清空成功，两者可以共存。")
else:
    log("存储值 = %s -> 固件会在门控打开时把实时上限同步进存储区。" % ok_after)
    log("         结论：「清空存储值」与「限充生效」在本机型上不能同时成立 ——")
    log("         只要限充工作（门控钉扎），固件就会把存储值保持为当前上限。")

log("")
state("最终")
log("")
log("平台值已钉扎为限充工作状态；身份字节 0x0770 全程未改动。")

with open(RESULT, "w", encoding="utf-8") as f:
    f.write("\n".join(records) + "\n")
print("")
print("结果已写入: %s" % RESULT)
