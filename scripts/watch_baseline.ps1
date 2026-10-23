param(
    [Parameter(Mandatory = $true)][int]$BenchmarkPid,
    [Parameter(Mandatory = $true)][string]$RunDirectory,
    [ValidateSet('bird', 'ambrosia')][string]$BenchmarkName = 'ambrosia',
    [int]$StallSeconds = 600,
    [int]$ConsecutiveErrorLimit = 8
)
$ErrorActionPreference = 'Stop'
$runPath = (Resolve-Path -LiteralPath $RunDirectory).Path
$metadataPath = Join-Path $runPath 'metadata.json'
$checkpointPath = Join-Path $runPath 'query_results.json'
$logPath = Join-Path $runPath "$BenchmarkName.log"
$statePath = Join-Path $runPath 'monitor_state.json'

function Write-State([string]$Status, [string]$Reason, [int]$Completed = 0) {
    $state = [ordered]@{
        status = $Status
        reason = $Reason
        benchmark_pid = $BenchmarkPid
        monitor_pid = $PID
        checked_at = (Get-Date).ToString('o')
        questions_saved = $Completed
        stall_seconds_limit = $StallSeconds
        consecutive_error_limit = $ConsecutiveErrorLimit
    }
    $state | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
    Write-Output "$($state.checked_at) $Status saved=$Completed $Reason"
}

# Validate the command before opening and retaining a handle to this exact process.
$info = Get-CimInstance Win32_Process -Filter "ProcessId = $BenchmarkPid"
if (-not $info -or $info.CommandLine -notmatch 'scripts[\\/]run_openrouter_baseline\.py' -or
    $info.CommandLine -notmatch "--benchmark\s+$BenchmarkName(?:\s|$)") {
    throw 'The target is not the expected benchmark process.'
}
$benchmarkProcess = [System.Diagnostics.Process]::GetProcessById($BenchmarkPid)
$null = $benchmarkProcess.Handle
Write-State 'watching' "Monitoring current $BenchmarkName process"
try {
    while ($true) {
        $benchmarkProcess.Refresh()
        if ($benchmarkProcess.HasExited) {
            $metadata = Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json
            $savedCount = 0
            if ($metadata.status -eq 'completed') { $savedCount = $metadata.questions }
            Write-State 'finished' "Process exited; benchmark status=$($metadata.status)" $savedCount
            break
        }
        try {
            $metadata = Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json
            if ($metadata.status -eq 'completed') {
                Write-State 'completed' 'Benchmark completed successfully' $metadata.questions
                break
            }
            $predictions = @()
            if (Test-Path -LiteralPath $checkpointPath) {
                $checkpoint = Get-Content -LiteralPath $checkpointPath -Raw | ConvertFrom-Json
                $predictions = @($checkpoint.PSObject.Properties | ForEach-Object { $_.Value })
            }
            $consecutive = 0
            for ($i = $predictions.Count - 1; $i -ge 0; $i--) {
                if ($predictions[$i] -ne 'error') { break }
                $consecutive++
            }
            $lastWrite = (Get-Item -LiteralPath $logPath).LastWriteTime
            if (Test-Path -LiteralPath $checkpointPath) {
                $checkpointWrite = (Get-Item -LiteralPath $checkpointPath).LastWriteTime
                if ($checkpointWrite -gt $lastWrite) { $lastWrite = $checkpointWrite }
            }
            $idleSeconds = ((Get-Date) - $lastWrite).TotalSeconds
            $reason = ''
            if ($idleSeconds -ge $StallSeconds) {
                $reason = "No log/checkpoint progress for $([int]$idleSeconds) seconds"
            } elseif ($consecutive -ge $ConsecutiveErrorLimit) {
                $reason = "$consecutive consecutive generation errors"
            }
        } catch {
            # A checkpoint may be in the middle of being written. Retry next poll.
            Write-Output "$(Get-Date -Format o) Snapshot unavailable; retrying next poll"
            Start-Sleep -Seconds 15
            continue
        }
        if ($reason) {
            # The retained process handle prevents killing a recycled PID.
            if (-not $benchmarkProcess.HasExited) {
                $benchmarkProcess.Kill()
                $benchmarkProcess.WaitForExit()
            }
            Write-State 'stopped_by_monitor' $reason $predictions.Count
            # Force-stop skips the Python finally block; mark that explicitly.
            $metadata.status = 'stopped_by_monitor'
            $metadata | Add-Member -NotePropertyName monitor_stop_reason -NotePropertyValue $reason -Force
            $metadata | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $metadataPath -Encoding UTF8
            break
        }
        Write-State 'watching' "phase=$($metadata.phase); db=$($metadata.databases_ready)/$($metadata.databases_total); idle=$([int]$idleSeconds)s" $predictions.Count
        Start-Sleep -Seconds 15
    }
} finally {
    $benchmarkProcess.Dispose()
}
