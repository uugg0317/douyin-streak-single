# Register or remove a per-user logon task that starts the local console.
# ASCII only on purpose: Windows PowerShell 5.1 misreads UTF-8 files without BOM.
param(
    [ValidateSet('Install', 'Remove', 'Status')]
    [string]$Action = 'Status'
)
$ErrorActionPreference = 'Stop'
$TaskName = 'DouyinStreakAutostart'
$Root = Split-Path -Parent $PSScriptRoot
$Launcher = Get-ChildItem -LiteralPath $Root -Filter '*.bat' |
    Where-Object { (Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8) -match 'startup_check\.py' -and
                   (Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8) -match 'app\.py' } |
    Select-Object -First 1

switch ($Action) {
    'Install' {
        if (-not $Launcher) { throw "Launcher .bat not found in $Root" }
        $command = 'start "" /min "' + $Launcher.FullName + '"'
        $taskAction = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c ' + $command) -WorkingDirectory $Root
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
        $principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
            -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger -Settings $settings `
            -Principal $principal -Description 'Start the local Douyin streak console at logon (127.0.0.1:8000).' -Force | Out-Null
        Write-Output "Installed logon task '$TaskName' -> $($Launcher.FullName)"
    }
    'Remove' {
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Output "Removed logon task '$TaskName'"
        } else {
            Write-Output "Logon task '$TaskName' is not installed"
        }
    }
    'Status' {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($task) { Write-Output "Installed: $($task.State)" } else { Write-Output 'Not installed' }
    }
}
