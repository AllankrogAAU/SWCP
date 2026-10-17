[CmdletBinding()]
param(
    [string]$SubmissionId,
    [ValidateRange(1, 10000)]
    [int]$Tail = 2000
)

$lines = docker compose logs --no-color --timestamps --tail $Tail core-api c-sandbox-worker llm-worker
if ($LASTEXITCODE -ne 0) {
    throw "docker compose logs failed with exit code $LASTEXITCODE. Run this script from the repository root."
}

$events = foreach ($line in $lines) {
    $jsonStart = $line.IndexOf('{')
    if ($jsonStart -lt 0) {
        continue
    }

    try {
        $outer = $line.Substring($jsonStart) | ConvertFrom-Json
    }
    catch {
        continue
    }

    $state = if ($outer.State) { $outer.State } else { $outer }
    $eventName = $state.event_name
    $id = $state.submission_id
    if (-not $eventName -or -not $id -or -not $eventName.StartsWith('pipeline.')) {
        continue
    }

    $timestamp = if ($outer.Timestamp) { $outer.Timestamp } else { $outer.timestamp }
    if (-not $timestamp) {
        continue
    }

    [pscustomobject]@{
        SubmissionId = [string]$id
        TraceId = [string]$state.trace_id
        EventName = [string]$eventName
        Stage = [string]$state.stage
        Timestamp = [DateTimeOffset]::Parse([string]$timestamp)
        DurationMs = $state.duration_ms
        QueueWaitMs = $state.queue_wait_ms
        QueuePublishMs = $state.queue_publish_ms
        Status = [string]$state.status
        Attempt = $state.attempt
    }
}

if (-not $events) {
    Write-Output 'No structured pipeline events found in the selected Docker log tail.'
    exit 0
}

if ($SubmissionId) {
    $selectedEvents = @($events | Where-Object SubmissionId -eq $SubmissionId)
    if (-not $selectedEvents) {
        Write-Output "No pipeline events found for submission $SubmissionId. Increase -Tail or check the ID."
        exit 0
    }
}
else {
    $latest = $events |
        Where-Object EventName -eq 'pipeline.completed' |
        Sort-Object Timestamp -Descending |
        Select-Object -First 1

    if (-not $latest) {
        $latest = $events | Sort-Object Timestamp -Descending | Select-Object -First 1
        Write-Output 'No completed submission found in the selected log tail; showing the latest submission with events.'
    }

    $SubmissionId = $latest.SubmissionId
    $selectedEvents = @($events | Where-Object SubmissionId -eq $SubmissionId)
}

$rows = foreach ($event in ($selectedEvents | Sort-Object Timestamp)) {
    $label = $null
    $duration = $null

    if ($event.EventName -eq 'pipeline.stage.completed') {
        $label = switch ($event.Stage) {
            'submission_enqueue' { 'API submit/enqueue' }
            'sandbox' { 'Sandbox execution' }
            'llm' { 'LLM execution' }
            default { $null }
        }
        $duration = $event.DurationMs
    }
    elseif ($event.EventName -eq 'pipeline.stage.started' -and $null -ne $event.QueueWaitMs) {
        $label = "$($event.Stage) queue wait"
        $duration = $event.QueueWaitMs
    }
    elseif ($event.EventName -in @('pipeline.stage.retrying', 'pipeline.stage.failed')) {
        $label = "$($event.Stage) $($event.Status) (attempt $($event.Attempt))"
        $duration = $event.DurationMs
    }
    elseif ($event.EventName -eq 'pipeline.completed') {
        $label = 'TOTAL'
        $duration = $event.DurationMs
    }

    if ($label) {
        $formattedDuration = if ($null -ne $duration) {
            $milliseconds = [double]$duration
            '{0:N0} ms ({1:N3} s)' -f $milliseconds, ($milliseconds / 1000)
        }
        else {
            'n/a'
        }

        [pscustomobject]@{
            Time = $event.Timestamp.ToString('HH:mm:ss.fff zzz')
            Stage = $label
            Duration = $formattedDuration
            Status = $event.Status
            Attempt = $event.Attempt
        }

        if ($event.Stage -eq 'submission_enqueue' -and $null -ne $event.QueuePublishMs) {
            $publishMilliseconds = [double]$event.QueuePublishMs
            [pscustomobject]@{
                Time = $event.Timestamp.ToString('HH:mm:ss.fff zzz')
                Stage = 'NATS task publish'
                Duration = '{0:N0} ms ({1:N3} s)' -f $publishMilliseconds, ($publishMilliseconds / 1000)
                Status = $event.Status
                Attempt = $event.Attempt
            }
        }
    }
}

Write-Output "Submission: $SubmissionId"
$traceId = ($selectedEvents | Where-Object TraceId | Select-Object -First 1).TraceId
if ($traceId) {
    Write-Output "Trace:      $traceId"
}
if ($rows) {
    $rows | Format-Table -AutoSize | Out-String -Width 200 | Write-Output
}
else {
    Write-Output 'No timing events were found for this submission.'
}
