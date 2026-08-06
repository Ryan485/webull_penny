# Records an EXPLICIT owner commit authorization, bound to the currently
# approved diff hash. Run ONLY after the owner has said, in their own words,
# that the commit is authorized (e.g. "commit it", "approved, create the
# commit", "you may commit these changes").
#
# NOT authorization: silence, plan approval, automation-design approval, a prior
# task's permission, Codex approval, Claude approval, consensus, passing tests,
# or "looks good" without an explicit instruction to commit.
#
# Usage:
#   powershell -NoProfile -File .claude\hooks\authorize-commit.ps1 -Phrase "commit it"
#
# Refuses unless the review reached consensus AND the tree still matches the
# approved hash. The record is single-use: posttool-consume.ps1 marks it CONSUMED
# once a commit actually lands.
param(
    [Parameter(Mandatory = $true)][string]$Phrase,
    [string]$Root = $env:CLAUDE_PROJECT_DIR,
    [string]$SessionId = ""
)

. (Join-Path $PSScriptRoot "lib-review.ps1")

if (-not $Root) { $Root = (Get-Location).Path }
$dir = Join-Path $Root ".codex-reviews"

if (-not (Test-Path (Join-Path $dir "state.json"))) {
    Write-Output "REFUSED: no review state. Nothing has been reviewed, so nothing can be authorized."
    exit 1
}
$state = Get-ReviewState $dir

if ($state.verdict -cne "APPROVED") {
    Write-Output "REFUSED: current Codex verdict is '$($state.verdict)', not APPROVED. Reach consensus first."
    exit 1
}
if ($state.open_findings -ne 0) {
    Write-Output "REFUSED: OPEN_FINDINGS is $($state.open_findings), not 0."
    exit 1
}
if (-not $state.approved_hash) {
    Write-Output "REFUSED: no approved hash on record."
    exit 1
}
if ($state.approval_status -eq $script:AUTH_CONSUMED) {
    Write-Output "REFUSED: this approval was already consumed by a commit. Re-review first."
    exit 1
}

$wt = Get-WorkTreeManifest $Root
if ($wt.Hash -ne $state.approved_hash) {
    $stale = $state.approved_hash
    $state.approval_status = $script:AUTH_INVALIDATED
    $state.approved_hash   = ""
    $state.approved_tree   = ""
    Save-ReviewState $dir $state
    Write-Output "REFUSED: the working tree changed after approval."
    Write-Output "  approved: $stale"
    Write-Output "  current : $($wt.Hash)"
    Write-Output "Approval INVALIDATED. Re-run the review cycle."
    exit 1
}

# The index must still be the exact reviewed tree (R1-COMMIT-INDEX).
$ts = Get-CommitTreeState $Root
if (-not $ts.FullyStaged) {
    Write-Output "REFUSED: the tree is not fully staged, so a commit would not match the review."
    Write-Output ("  unstaged : {0}" -f (($ts.Unstaged -join ', ')))
    Write-Output ("  untracked: {0}" -f (($ts.Untracked -join ', ')))
    exit 1
}
if ($ts.Tree -ne $state.approved_tree) {
    $stale = $state.approved_tree
    $state.approval_status = $script:AUTH_INVALIDATED
    $state.approved_hash   = ""
    $state.approved_tree   = ""
    Save-ReviewState $dir $state
    Write-Output "REFUSED: the staged index does not match the approved tree."
    Write-Output "  approved tree: $stale"
    Write-Output "  current  tree: $($ts.Tree)"
    Write-Output "Approval INVALIDATED. Re-run the review cycle."
    exit 1
}

$state.approval_status    = $script:AUTH_VALID
$state.authorized_phrase  = $Phrase
$state.authorized_at      = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$state.authorized_session = $SessionId
Save-ReviewState $dir $state

$a = New-AuditRecord
$a.round            = $state.round
$a.verdict          = $state.verdict
$a.open_findings    = $state.open_findings
$a.pre_diff_hash    = $state.approved_hash
$a.approval_status  = $script:AUTH_VALID
$a.failure_reason   = "owner authorization recorded: '$Phrase'"
Write-AuditRecord $dir $a

Write-Output "AUTHORIZED for hash $($state.approved_hash)"
Write-Output "Single-use. Consumed once a commit lands. Push still requires separate authorization."
exit 0
