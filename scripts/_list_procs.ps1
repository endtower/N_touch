$procs = Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'node|SnowLuma|cmd' }
foreach ($p in $procs) {
    $cmd = $p.CommandLine
    if ($cmd -and $cmd.Length -gt 150) { $cmd = $cmd.Substring(0, 150) }
    "{0}`t{1}`t{2}" -f $p.ProcessId, $p.ParentProcessId, $cmd
}
