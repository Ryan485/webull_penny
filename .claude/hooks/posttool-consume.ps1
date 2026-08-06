# PostToolUse: consume the owner authorization only if the commit REALLY landed
# (HEAD moved). A failed or cancelled commit leaves it unconsumed but still
# hash-bound, so it can never authorize a later, changed diff.
#
# Guard mode is ALWAYS ON, so this does NOT reopen the gate: after consumption
# the verdict is cleared, which means the next commit needs a fresh review and a
# fresh authorization.
. (Join-Path $PSScriptRoot "lib-review.ps1")

$raw = ""
try { $raw = [Console]::In.ReadToEnd() } catch { exit 0 }
$inp = $null
try { $inp = $raw | ConvertFrom-Json } catch { exit 0 }

$root = $env:CLAUDE_PROJECT_DIR
if (-not $root -and $inp) { $root = $inp.cwd }
if (-not $root) { exit 0 }
$dir = Join-Path $root ".codex-reviews"
if (-not (Test-Path (Join-Path $dir "state.json"))) { exit 0 }

$cmd = ""
if ($inp.tool_input -and $inp.tool_input.command) { $cmd = [string]$inp.tool_input.command }
# Same BROAD detector the gate uses, so `git -C <path> commit` / `git.exe commit`
# consume the authorization instead of silently leaving it reusable
# (R1-COMMIT-MATCHER).
if (-not (Test-CommitCapable $cmd)) { exit 0 }

try { $state = Get-ReviewState $dir } catch { exit 0 }
if ($state.approval_status -cne $script:AUTH_VALID) { exit 0 }

$head = ([string](& git -C $root rev-parse HEAD 2>$null)).Trim()
if (-not $head -or $head -eq $state.head_at_approval) { exit 0 }   # commit did not land

$state.approval_status   = $script:AUTH_CONSUMED
$state.approved_hash     = ""
$state.approved_tree     = ""
$state.authorized_phrase = ""
$state.verdict           = ""          # next commit needs a fresh review
$state.open_findings     = -1
$state.fingerprint       = ""
$state.in_flight         = $false
try { Save-ReviewState $dir $state } catch { exit 0 }

$a = New-AuditRecord
$a.round = $state.round
$a.approval_status = $script:AUTH_CONSUMED
$a.failure_reason = "commit $head landed; authorization consumed; guard re-armed"
try { Write-AuditRecord $dir $a } catch {}

(@{ systemMessage = "Commit $($head.Substring(0,8)) landed. Authorization CONSUMED and the commit guard is re-armed. Pushing requires separate explicit owner authorization." } | ConvertTo-Json -Depth 3) | Write-Output
exit 0
