# PreToolUse guard. Three jobs:
#   1) Edit lock  - refuse repo mutations while a Codex review is running.
#   2) Commit gate - ALWAYS ON. Every commit-capable command requires a completed
#      Codex review (VERDICT: APPROVED, OPEN_FINDINGS: 0), an explicit unconsumed
#      owner authorization bound to the approved hash AND the approved index tree,
#      and a tree that still matches both.
#   3) Isolation - an authorized commit must be the ONLY operation in the command.
#
# This is a WORKFLOW SAFEGUARD, NOT a sandbox. It matches command text and only
# governs tool calls made through Claude Code. The owner can always commit from
# their own terminal; that is the intended escape hatch for an emergency.
. (Join-Path $PSScriptRoot "lib-review.ps1")

$raw = ""
try { $raw = [Console]::In.ReadToEnd() } catch { exit 0 }
$inp = $null
try { $inp = $raw | ConvertFrom-Json } catch { exit 0 }

$root = $env:CLAUDE_PROJECT_DIR
if (-not $root -and $inp) { $root = $inp.cwd }
if (-not $root) { exit 0 }
$dir = Join-Path $root ".codex-reviews"

function Block-Tool {
    param([string]$Reason)
    (@{ hookSpecificOutput = @{
            hookEventName = "PreToolUse"
            permissionDecision = "deny"
            permissionDecisionReason = $Reason } } | ConvertTo-Json -Depth 5) | Write-Output
    exit 0
}

$cmd = ""
if ($inp.tool_input -and $inp.tool_input.command) { $cmd = [string]$inp.tool_input.command }

# --- 1) Edit lock -----------------------------------------------------------
if (Test-Path (Join-Path $dir "review.lock")) {
    $mutating = $false
    if (@("Edit", "Write", "NotebookEdit") -contains $inp.tool_name) { $mutating = $true }
    if ((Test-CommitCapable $cmd) -or (Test-MutatingSegment $cmd)) { $mutating = $true }
    if ($mutating) {
        Block-Tool "BLOCKED: a Codex review is in progress (.codex-reviews/review.lock exists). Editing the repository now would void the reviewed snapshot. Wait for the review to finish."
    }
}

# --- 2) Commit gate (always on) --------------------------------------------
if (-not (Test-CommitCapable $cmd)) { exit 0 }

# 3) Isolation: an authorized commit may not share a command with anything else.
# Without this, `Set-Content x; git add x; git commit` would pass the hash check
# BEFORE the mutation ran, then commit unreviewed bytes (R1-COMPOUND-COMMIT).
$segments = Split-CommandSegments $cmd
$commitSegs = @($segments | Where-Object { Test-CommitCapable $_ })
$otherMutating = @($segments | Where-Object { (-not (Test-CommitCapable $_)) -and (Test-MutatingSegment $_) })

if ($commitSegs.Count -gt 1) {
    Block-Tool "COMMIT BLOCKED: the command contains $($commitSegs.Count) commit operations. One authorization permits exactly ONE commit. Issue them as separate tool calls, each with its own review and authorization."
}
if ($otherMutating.Count -gt 0) {
    Block-Tool @"
COMMIT BLOCKED: a commit may not be combined with other repository-mutating
operations in one command. The gate validates the tree BEFORE the command runs,
so a mutation in the same command would be committed unreviewed.

  offending segment(s): $($otherMutating -join ' | ')

Run the mutation separately, re-run the review, then commit on its own.
"@
}

if (-not (Test-Path (Join-Path $dir "state.json"))) {
    Block-Tool "COMMIT BLOCKED: no Codex review has ever run in this repo. Every commit requires a completed review plus explicit owner authorization. Implement, test, stage everything, write .codex-reviews/handoff.md, then end your message with the review marker."
}

try { $state = Get-ReviewState $dir }
catch { Block-Tool "COMMIT BLOCKED: review state is unreadable ($($_.Exception.Message)). Failing closed." }

if ($state.verdict -eq "CHANGES_REQUESTED") {
    Block-Tool "COMMIT BLOCKED: Codex round $($state.round) left $($state.open_findings) open finding(s). No commit is permitted while a consequential objection is unresolved."
}
if ($state.verdict -cne "APPROVED") {
    Block-Tool "COMMIT BLOCKED: no APPROVED Codex verdict on record (current verdict: '$($state.verdict)'). Run the review cycle to consensus first."
}
if ($state.approval_status -eq $script:AUTH_CONSUMED) {
    Block-Tool "COMMIT BLOCKED: the owner authorization was already consumed by a successful commit. A new review and a new explicit authorization are required."
}
if ($state.approval_status -eq $script:AUTH_INVALIDATED) {
    Block-Tool "COMMIT BLOCKED: the approval was INVALIDATED (the tree changed or a new review round began). Re-run the review cycle."
}
if ($state.approval_status -cne $script:AUTH_VALID) {
    Block-Tool "COMMIT BLOCKED: commit authorization status is '$($state.approval_status)'. Explicit owner authorization for the current approved hash is required. Codex approval, Claude approval, consensus and passing tests are all insufficient on their own."
}
if (-not $state.approved_hash -or -not $state.approved_tree) {
    Block-Tool "COMMIT BLOCKED: authorization is marked VALID but the approved hash or approved index tree is missing. Failing closed."
}

# Worktree bytes must match what was reviewed...
try { $wt = Get-WorkTreeManifest $root }
catch { Block-Tool "COMMIT BLOCKED: cannot compute the current diff hash ($($_.Exception.Message)). Failing closed." }

if ($state.approved_hash -ne $wt.Hash) {
    $stale = $state.approved_hash
    $state.approval_status   = $script:AUTH_INVALIDATED
    $state.approved_hash     = ""
    $state.approved_tree     = ""
    $state.authorized_phrase = ""
    try { Save-ReviewState $dir $state } catch {}
    Block-Tool @"
COMMIT BLOCKED: the working tree changed after approval.
  approved: $stale
  current : $($wt.Hash)
The approval and the authorization are now INVALIDATED. Re-run the review cycle.
"@
}

# ...and the INDEX must be exactly the reviewed tree, since that is what a commit
# records (R1-COMMIT-INDEX).
try { $ts = Get-CommitTreeState $root }
catch { Block-Tool "COMMIT BLOCKED: cannot inspect the git index ($($_.Exception.Message)). Failing closed." }

if (-not $ts.FullyStaged) {
    $u = ($ts.Unstaged -join ", "); if (-not $u) { $u = "none" }
    $n = ($ts.Untracked -join ", "); if (-not $n) { $n = "none" }
    Block-Tool @"
COMMIT BLOCKED: the working tree is not fully staged, so the commit would not
match the reviewed tree.
  unstaged : $u
  untracked: $n
"@
}
if ($ts.Tree -ne $state.approved_tree) {
    $stale = $state.approved_tree
    $state.approval_status = $script:AUTH_INVALIDATED
    $state.approved_hash   = ""
    $state.approved_tree   = ""
    try { Save-ReviewState $dir $state } catch {}
    Block-Tool @"
COMMIT BLOCKED: the staged index does not match the approved tree.
  approved tree: $stale
  current  tree: $($ts.Tree)
The approval is INVALIDATED. Re-run the review cycle.
"@
}

# Record HEAD so PostToolUse can prove whether the commit actually landed.
$state.head_at_approval = ([string](& git -C $root rev-parse HEAD 2>$null)).Trim()
try { Save-ReviewState $dir $state } catch {}
exit 0
