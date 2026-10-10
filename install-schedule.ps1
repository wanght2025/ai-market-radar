$ErrorActionPreference = 'Stop'
$radarRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$radarPython = Join-Path $radarRoot '.venv\Scripts\pythonw.exe'
$radarScheduler = Join-Path $radarRoot 'scheduler.py'
if (!(Test-Path -LiteralPath $radarPython) -or !(Test-Path -LiteralPath $radarScheduler)) {
    throw 'Run run.bat once to prepare the local runtime.'
}
$radarHash = [System.Security.Cryptography.SHA256]::Create()
$radarId = [BitConverter]::ToString($radarHash.ComputeHash([Text.Encoding]::UTF8.GetBytes($radarRoot))).Replace('-', '').Substring(0, 12)
$radarTaskName = 'AI-Market-Radar-' + $radarId
$radarAction = New-ScheduledTaskAction -Execute $radarPython -Argument ('"' + $radarScheduler + '" --once') -WorkingDirectory $radarRoot
$radarTrigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
$radarSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$radarUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$radarPrincipal = New-ScheduledTaskPrincipal -UserId $radarUser -LogonType Interactive -RunLevel Limited
$radarTask = New-ScheduledTask -Action $radarAction -Trigger $radarTrigger -Settings $radarSettings -Principal $radarPrincipal -Description ('Local market research scheduler for ' + $radarRoot)
Register-ScheduledTask -TaskName $radarTaskName -InputObject $radarTask -Force | Out-Null
$radarInstallRecord = @{ task_name = $radarTaskName; installed_at = (Get-Date).ToString('o'); interval = '5 minutes'; pre_market_beijing = '08:30'; requires_logged_in = $true } | ConvertTo-Json
[System.IO.File]::WriteAllText((Join-Path $radarRoot 'data\scheduler-install.json'), $radarInstallRecord, (New-Object System.Text.UTF8Encoding($false)))
Write-Output ('Installed local scheduled task: ' + $radarTaskName)
