# Read-only EC probe via firmware WMI mailbox (AcpiTest_MULong.GetSetULong).
# Return value is in the property named 'Return' (NOT 'ReturnValue').
# Protocol: Data = (0x0100 << 32) | addr ; Return low byte = value at addr.
# This script NEVER writes any register.

$out = if ($PSScriptRoot) { Join-Path $PSScriptRoot "wmi_result.txt" } else { "wmi_result.txt" }
$lines = @()

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$pr = New-Object Security.Principal.WindowsPrincipal($id)
$lines += "IsAdmin : " + $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$lines += "Time    : " + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
$lines += ""

$instances = @()
try {
    $instances = @(Get-WmiObject -Namespace root/WMI -Class AcpiTest_MULong -ErrorAction Stop)
} catch {
    $lines += "Mailbox FAIL: " + $_.Exception.Message
}
$lines += "AcpiTest_MULong instances: " + $instances.Count

$hi = [uint64]4294967296
$base = [uint64]256 * $hi

function Read-Ec($mb, $addr) {
    $data = [uint64]($base + [uint64]$addr)
    $r = $mb.GetSetULong($data)
    if ($r -is [System.Management.ManagementBaseObject]) { $r = $r.Return }
    if ($r -is [System.Object[]]) { $r = $r[0] }
    if ($r -eq $null) { return [uint64]0 }
    return [uint64]$r
}

if ($instances.Count -gt 0) {
    $mb = $instances[0]
    $lines += ""
    $lines += "=== EC registers via WMI mailbox (instance [0]) ==="
    $targets = @(
        @(0x0740, "PROJECT_ID"),
        @(0x0770, "ROMID[0]"),
        @(0x07C3, "platform"),
        @(0x07A6, "charge_mode"),
        @(0x07B9, "live_limit"),
        @(0x087F, "STORED_LIMIT"),
        @(0x0742, "gate_status")
    )
    $storedVal = -1
    foreach ($t in $targets) {
        $addr = [uint64]$t[0]
        $name = $t[1]
        try {
            $r64 = Read-Ec $mb $addr
            $low = $r64 -band 0xFF
            $extra = ""
            if ($addr -eq 0x087F) { $storedVal = ($low -band 0x7F); $extra = "  -> STORED limit = " + ($low -band 0x7F) + "%" }
            if ($addr -eq 0x07B9) { $extra = "  -> live limit = " + ($low -band 0x7F) + "%" }
            if ($addr -eq 0x0742) { $extra = "  -> gate bit2=" + (($low -shr 2) -band 1) }
            if ($addr -eq 0x07A6) { $extra = "  -> mode bits[5:4]=" + (($low -shr 4) -band 3) }
            $lines += ("{0,-13} 0x{1:X4} -> raw=0x{2:X16} low=0x{3:X2}{4}" -f $name, $addr, $r64, $low, $extra)
        } catch {
            $lines += "{0,-13} read failed: {1}" -f $name, $_.Exception.Message
        }
    }

    $lines += ""
    $lines += "=== VERDICT ==="
    if ($storedVal -ge 1 -and $storedVal -le 100) {
        $lines += "STORED_LIMIT holds a valid value (" + $storedVal + "%)."
        $lines += "=> Firmware storage mechanism WORKS. A one-shot write can be permanent."
    } else {
        $lines += "STORED_LIMIT = " + $storedVal + " (invalid / empty)."
        $lines += "=> Firmware storage is NOT holding the value. One-shot permanent write unavailable."
    }
}

$lines += ""
$lines += "READ-ONLY probe finished. No register was written."

$lines | Set-Content -Path $out -Encoding UTF8
$lines | ForEach-Object { Write-Host $_ }
