<#
Runs main.py, showing output live while appending it to the run log.

Why this file exists instead of a one-liner inside Run Noperi.bat:

PowerShell 5.1's `Tee-Object -FilePath` has no -Encoding parameter and writes
UTF-16LE, while cmd.exe's `>> echo` writes ANSI. Appending to one file from
both left logs/noperi_hidden.log roughly half NUL bytes and unreadable in any
single encoding. A StreamWriter pins the encoding to UTF-8 (no BOM) for every
line, including the start/exit markers, so the log is one consistent encoding.

AutoFlush means output is on disk as it happens, so a crashed or killed run
still leaves a complete log — buffering the whole run in a variable would not.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [Parameter(Mandatory = $true)][string]$Script,
    [Parameter(Mandatory = $true)][string]$LogFile
)

$ErrorActionPreference = 'Stop'

# Emoji in the agent output must survive both the console and the file.
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom

$logDir = Split-Path -Parent $LogFile
if ($logDir -and -not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

function Get-Stamp { Get-Date -Format 'yyyy-MM-dd HH:mm:ss' }

$writer = New-Object System.IO.StreamWriter($LogFile, $true, $utf8NoBom)
$writer.AutoFlush = $true

$exitCode = 1
try {
    # NOTE: build the marker text into a variable first. Writing
    # $writer.WriteLine("{0}..{1}" -f $a, $b) makes PowerShell treat the comma
    # as a METHOD argument separator, so -f receives only one value and throws
    # "Index (zero based) must be ... less than the size of the argument list".
    $startLine = "[$(Get-Stamp)] Starting Noperi with $PythonExe"
    $writer.WriteLine('')
    $writer.WriteLine($startLine)

    # 2>&1 folds stderr into the same ordered stream as stdout, so the log
    # keeps warnings and tracebacks in the order they actually happened.
    #
    # 'Continue' is required here: under 'Stop', ANY stderr line from a native
    # command is wrapped in an ErrorRecord and raised as a terminating error,
    # so a single harmless warning would abort the launcher and truncate the
    # log. Python's real result is $LASTEXITCODE, which is checked below.
    $ErrorActionPreference = 'Continue'
    & $PythonExe $Script 2>&1 | ForEach-Object {
        $line = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { [string]$_ }
        Write-Host $line
        $writer.WriteLine($line)
    }
    $exitCode = $LASTEXITCODE
    if ($null -eq $exitCode) { $exitCode = 0 }

    $writer.WriteLine("[$(Get-Stamp)] Noperi exited with code $exitCode")
}
catch {
    $message = $_ | Out-String
    $writer.WriteLine("[$(Get-Stamp)] Launcher error: $($message.Trim())")
    Write-Host "Launcher error: $($message.Trim())"
    $exitCode = 1
}
finally {
    $writer.Close()
}

exit $exitCode
