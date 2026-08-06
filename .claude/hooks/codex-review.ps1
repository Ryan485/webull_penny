# Stop hook: drives the Claude<->Codex consensus review cycle.
# No-ops on ordinary turns. Once the EXACT marker is seen it FAILS CLOSED:
# every error path blocks the stop and reports visibly.
. (Join-Path $PSScriptRoot "lib-review.ps1")

$script:root     = $null
$script:dir      = $null
$script:audit    = New-AuditRecord
$script:state    = $null
$script:snapshot = $null
$script:lock     = $null

function Release-Resources {
    if ($script:snapshot) { Remove-ReviewSnapshot $script:root $script:snapshot; $script:snapshot = $null }
    if ($script:lock -and (Test-Path $script:lock)) { Remove-Item $script:lock -Force }
}

function Fail-Closed {
    param([string]$Short, [string]$Guidance)
    Release-Resources
    $script:audit.failure_reason  = $Short
    $script:audit.approval_status = $script:AUTH_ABSENT
    if ($script:state) {
        # A failed review must never create or retain a valid approval.
        $script:state.approved_hash     = ""
        $script:state.approved_tree     = ""
        $script:state.approval_status   = $script:AUTH_ABSENT
        $script:state.authorized_phrase = ""
        $script:state.last_error        = $Short
        $script:state.in_flight         = $true
        try { Save-ReviewState $script:dir $script:state } catch {}
    }
    if ($script:dir) {
        try {
            Write-AuditRecord $script:dir $script:audit
            Write-ReviewSummary $script:dir $script:state $script:audit `
                "REVIEW FAILED - NOT APPROVED. No commit is authorized." $Guidance
        } catch {}
    }
    Deny-Stop @"
[REVIEW FAILED - NOT APPROVED] $Short

$Guidance

This is NOT approval and NOT consensus. Do not commit.
Machine record: .codex-reviews/REVIEW-SUMMARY.md and .codex-reviews/audit.jsonl
"@
}

# --- read hook input --------------------------------------------------------
$raw = ""
try { $raw = [Console]::In.ReadToEnd() } catch { $raw = "" }
$inp = $null
try { $inp = $raw | ConvertFrom-Json } catch { $inp = $null }

$script:root = $env:CLAUDE_PROJECT_DIR
if (-not $script:root -and $inp) { $script:root = $inp.cwd }
if (-not $script:root) { $script:root = (Get-Location).Path }

# Malformed/missing input: block ONLY if a cycle is in flight (we cannot verify
# the marker, so we must not silently pass). An ordinary turn exits safely.
if (-not $inp -or -not $inp.transcript_path) {
    $maybe = Join-Path $script:root ".codex-reviews"
    if (Test-Path (Join-Path $maybe "state.json")) {
        try { $s = Get-ReviewState $maybe } catch { $s = $null }
        if ($s -and ($s.in_flight -or $s.verdict -eq "CHANGES_REQUESTED")) {
            $script:dir = Get-ReviewDir $script:root
            $script:state = $s
            Fail-Closed "hook input missing or malformed while a review cycle is in flight" `
                "The transcript could not be read, so the review marker cannot be verified. Re-run the turn; if this repeats, inspect the Stop hook wiring."
        }
    }
    exit 0
}

$tx = @{ Text = ""; Parsed = 0; Failed = 0 }
try { $tx = Get-LastAssistantText $inp.transcript_path }
catch { $tx = @{ Text = ""; Parsed = 0; Failed = -1 } }
$markerSeen = Test-ReviewMarker $tx.Text

try { $script:dir = Get-ReviewDir $script:root } catch { exit 0 }
try { $script:state = Get-ReviewState $script:dir }
catch {
    if ($markerSeen) {
        $script:state = $null
        Fail-Closed "review state unreadable: $($_.Exception.Message)" "Inspect .codex-reviews/state.json."
    }
    exit 0
}

# --- no marker: ordinary turn ----------------------------------------------
if (-not $markerSeen) {
    # If the transcript could not be read at all, "no marker" is unproven. That
    # is only safe on an ordinary turn; while a cycle is in flight, fail closed.
    if (($tx.Parsed -eq 0 -or $tx.Failed -ne 0) -and
        ($script:state.in_flight -or $script:state.verdict -eq "CHANGES_REQUESTED")) {
        Fail-Closed "transcript unreadable (parsed=$($tx.Parsed) failed=$($tx.Failed)) while a review cycle is in flight" `
            "The review marker could not be verified either way. Inspect the transcript path and the Stop hook wiring before continuing."
    }
    if ($inp.stop_hook_active -and $script:state.verdict -eq "CHANGES_REQUESTED") {
        Allow-Stop "WARNING: Codex round $($script:state.round) still has $($script:state.open_findings) OPEN finding(s). No approval and no commit authorization exist. See .codex-reviews/REVIEW-SUMMARY.md"
    }
    exit 0
}

# ===========================================================================
# MARKER SEEN -> fail closed from here on.
# ===========================================================================
try {
    # Desync: platform says a hook resumed us, our state records no cycle.
    if ($inp.stop_hook_active -and -not $script:state.in_flight -and $script:state.round -eq 0) {
        Fail-Closed "state desync: stop_hook_active=true but no review cycle recorded" `
            "The review state machine and the platform loop state disagree. Inspect .codex-reviews/state.json before continuing."
    }

    # Invalidate any prior approval BEFORE starting a new round.
    $script:state.approved_hash     = ""
    $script:state.approved_tree     = ""
    $script:state.approval_status   = $script:AUTH_INVALIDATED
    $script:state.authorized_phrase = ""
    $script:state.in_flight         = $true
    Save-ReviewState $script:dir $script:state

    try { $pre = Get-WorkTreeManifest $script:root }
    catch { Fail-Closed "hash generation failed: $($_.Exception.Message)" "Verify the repo is healthy (git status) before re-submitting." }

    $script:audit.pre_diff_hash = $pre.Hash
    if ($pre.Hash -eq "EMPTY") {
        Fail-Closed "marker emitted with no uncommitted changes" `
            "Either the work is already committed (do not re-review) or nothing was implemented. Do not emit the marker without changed files."
    }

    # R1-COMMIT-INDEX: approval must bind to the tree git would actually commit.
    try { $tstate = Get-CommitTreeState $script:root }
    catch { Fail-Closed "index state check failed: $($_.Exception.Message)" "Verify the repo is healthy (git status)." }

    if (-not $tstate.FullyStaged) {
        $u = ($tstate.Unstaged  -join ", "); if (-not $u) { $u = "none" }
        $n = ($tstate.Untracked -join ", "); if (-not $n) { $n = "none" }
        Fail-Closed "working tree is not fully staged, so approval cannot be bound to the commit" @"
`git commit` records the INDEX, not the working tree. While staged and unstaged
content differ -- or untracked files exist -- the tree Codex reviews is not
necessarily the tree that would be committed.

  unstaged changes : $u
  untracked files  : $n

Run `git add -A` (review artifacts are gitignored and will not be staged), then
re-emit the marker. Staging does not change the review hash.
"@
    }
    $script:audit.index_tree = $tstate.Tree

    $handoff = Join-Path $script:dir "handoff.md"
    if (-not (Test-Path $handoff)) {
        Fail-Closed "handoff file missing (.codex-reviews/handoff.md)" @"
Create it before emitting the marker. Required contents: round id, diff hash,
previous Codex verdict, every finding, your classification of each (ACCEPTED /
PARTIALLY_ACCEPTED / DISPUTED), fixes applied with files changed, regression
tests added, test commands run and their results, evidence for disputed
findings, and anything you believe is still unresolved.
No credentials, tokens, account IDs, or raw broker responses.
"@
    }
    $script:audit.handoff_hash  = Get-FileTextHash $handoff
    $script:audit.override_hash = Get-FileTextHash (Join-Path $script:dir "override.json")

    # --- no-progress detection ---------------------------------------------
    $fp = Get-StringHash ("{0}|{1}|{2}|{3}|{4}" -f `
        $pre.Hash, $script:audit.handoff_hash, $script:audit.override_hash, `
        $script:state.verdict, $script:state.open_findings)

    if ($fp -eq $script:state.fingerprint -and $script:state.verdict -eq "CHANGES_REQUESTED") {
        $corroborated = "no"
        if ($inp.stop_hook_active) { $corroborated = "yes (stop_hook_active)" }
        $script:audit.round          = $script:state.round
        $script:audit.verdict        = $script:state.verdict
        $script:audit.open_findings  = $script:state.open_findings
        $script:audit.post_diff_hash = $pre.Hash
        $script:audit.failure_reason = "no progress - paused for user decision"
        Write-AuditRecord $script:dir $script:audit
        Write-ReviewSummary $script:dir $script:state $script:audit `
            "REVIEW PAUSED - NO PROGRESS. Not consensus. Owner decision required." `
            "Loop corroborated by platform state: $corroborated"
        Release-Resources
        Deny-Stop @"
[REVIEW PAUSED - NO PROGRESS] The diff, handoff, override and last verdict are
byte-identical to round $($script:state.round). Re-running Codex would repeat a
paid review with no new information. Loop corroborated: $corroborated

This is NOT consensus and NOT approval.

Stop the automatic cycle. Present to the owner: the unresolved finding(s),
Codex's position, your position, the evidence each side holds, and the specific
information needed to break the deadlock. Ask for a decision.

Resume only after the implementation, the evidence, or the owner's decision
changes the state. Record an owner ruling in .codex-reviews/override.json so it
counts as progress.
"@
    }

    $round = [int]$script:state.round + 1
    $script:audit.round = $round
    $out = Join-Path $script:dir ("round-{0}.md" -f $round)
    $log = Join-Path $script:dir ("round-{0}.log" -f $round)

    # --- immutable snapshot ------------------------------------------------
    $script:lock = Join-Path $script:dir "review.lock"
    "round=$round pid=$PID started=$((Get-Date).ToUniversalTime().ToString('o'))" |
        Out-File $script:lock -Encoding utf8 -Force

    try { $script:snapshot = New-ReviewSnapshot $script:root $pre.Paths }
    catch { Fail-Closed "snapshot creation failed: $($_.Exception.Message)" "Check 'git worktree list' for stale entries, then run 'git worktree prune'." }

    try { $snapHash = (Get-WorkTreeManifest $script:snapshot).Hash }
    catch { Fail-Closed "snapshot hash failed: $($_.Exception.Message)" "The review snapshot could not be verified." }

    if ($snapHash -ne $pre.Hash) {
        Fail-Closed "snapshot does not match the hashed tree ($snapHash vs $($pre.Hash))" `
            "The snapshot is not a faithful copy of the changes under review. Refusing to review an unverified tree."
    }
    $script:audit.snapshot_verified = $true

    # Handoff and prior round reports are gitignored, so none of them are in the
    # snapshot by default; copy them in. Without this the handoff can reference
    # ".codex-reviews/round-N.md" (e.g. for prior finding text) and the reviewer
    # has no way to verify that history exists or says what Claude claims
    # (Codex NEW-SNAPSHOT-HISTORY-OMITTED, round 1, 2026-08-04).
    Copy-ReviewArtifactsToSnapshot -ReviewDir $script:dir -Snapshot $script:snapshot -Handoff $handoff

    # --- run Codex ---------------------------------------------------------
    $prompt = @"
Round $round of an independent, READ-ONLY review of a snapshot containing
UNCOMMITTED changes to an automated trading system.

Read AGENTS.md and then read CODEX_REVIEW.md in full. Follow CODEX_REVIEW.md
exactly, including its required output format and output-integrity rules.

Steps:
1. Read .codex-reviews/handoff.md for previous findings and Claude's responses.
2. Independently inspect the real diff:
     git --no-pager diff HEAD
     git status --porcelain
     git ls-files --others --exclude-standard
   Read surrounding code, not just changed lines. The CODE is authoritative over
   the handoff; report any discrepancy as a finding.
3. Classify EVERY previous finding as RESOLVED, WITHDRAWN, or STILL_OPEN.
   Withdraw only on concrete evidence, never on assertion.
4. Look for NEW defects introduced by this round's corrections.
5. Emit the verdict block exactly as CODEX_REVIEW.md specifies. Emit exactly one
   VERDICT line and one OPEN_FINDINGS line; do not reproduce the format template.

Do not modify any file.
"@
    # Prompt goes in on STDIN ("-"), not as an argument: codex.cmd is a batch
    # wrapper, and a multi-line prompt as an argv would be a quoting minefield.
    $pf = Join-Path $script:dir "prompt.txt"
    Write-Utf8NoBom $pf $prompt

    $codexExe = Resolve-CodexExe
    if (-not $codexExe) {
        Fail-Closed "codex CLI not found (looked for codex.cmd / codex.exe / codex.bat on PATH)" `
            "Install or repair the Codex CLI, then re-emit the marker."
    }

    $cargs = @("exec", "-s", "read-only", "-C", $script:snapshot, "-o", $out, "-")
    $run = $null
    try {
        $run = Invoke-Child -Exe $codexExe -Arguments $cargs -StdinFile $pf `
                   -StdoutFile $log -StderrFile "$log.err" -TimeoutMs 900000
    } catch {
        Fail-Closed "codex failed to launch ($codexExe): $($_.Exception.Message)" "Verify the codex CLI is executable."
    }

    if ($run.TimedOut) {
        $script:audit.codex_exit_code = "TIMEOUT"
        Fail-Closed "codex timed out after 15 min (round $round)" "Inspect .codex-reviews/round-$round.log, then re-emit the marker to retry."
    }
    if ($null -eq $run.ExitCode) {
        $script:audit.codex_exit_code = "UNKNOWN"
        Fail-Closed "codex exit code unavailable" "The child process exit status could not be determined, so the review cannot be trusted."
    }
    $script:audit.codex_exit_code = [string]$run.ExitCode

    # BOM-stripped: the verdict is the FIRST line, and a BOM would defeat '^'.
    $body = Read-TextNoBom $out
    $parsed = Parse-CodexVerdict $body

    # NOTE: do NOT scan stderr for auth keywords on the success path. stderr holds
    # the transcript of every file Codex read, INCLUDING this hook's own source,
    # so an auth pattern matches its own literal. Observed 2026-07-29: a fully
    # successful review (valid CHANGES_REQUESTED verdict) was discarded because the
    # only 4 matches in 206KB of stderr were this very regex on line 262 of this
    # file. A valid verdict proves Codex ran and authenticated. Auth is now only a
    # diagnostic HINT on the already-failing non-zero-exit path.
    $errTxt = Read-TextNoBom "$log.err"
    $script:audit.verdict       = $parsed.Verdict
    $script:audit.open_findings = $parsed.OpenFindings

    if ($run.ExitCode -ne 0) {
        $hint = ""
        # Anchored to codex's own error shape so source echoes cannot match.
        if ($errTxt -match "(?im)^\s*(error|ERROR).*(not logged in|codex login|401 Unauthorized)") {
            $hint = " (looks like auth: run 'codex login status')"
        }
        Fail-Closed "codex exited $($run.ExitCode)$hint" "See .codex-reviews/round-$round.log"
    }
    if (-not $parsed.Valid) {
        Fail-Closed "missing/malformed/contradictory verdict: $($parsed.Reason)" `
            "Per the verdict-parsing rules this counts as CHANGES_REQUESTED. Raw output: .codex-reviews/round-$round.md"
    }

    # --- post-review hash: reject a tree that moved ------------------------
    try { $post = Get-WorkTreeManifest $script:root }
    catch { Fail-Closed "post-review hash generation failed: $($_.Exception.Message)" "The reviewed tree could not be verified." }
    $script:audit.post_diff_hash = $post.Hash

    if ($post.Hash -ne $pre.Hash) {
        Fail-Closed "working tree changed during review (pre $($pre.Hash.Substring(0,12)) -> post $($post.Hash.Substring(0,12)))" `
            "An external process modified files while Codex was reviewing. The review is void and NO approval is stored. Re-submit for a fresh review."
    }

    # The index must also be unchanged, or approval would bind to a stale tree.
    try { $tpost = Get-CommitTreeState $script:root }
    catch { Fail-Closed "post-review index check failed: $($_.Exception.Message)" "The reviewed index could not be verified." }
    if (-not $tpost.FullyStaged -or $tpost.Tree -ne $tstate.Tree) {
        Fail-Closed "index changed during review (tree $($tstate.Tree) -> $($tpost.Tree))" `
            "The staged tree moved while Codex was reviewing. The review is void and NO approval is stored."
    }

    Release-Resources

    $script:state.round         = $round
    $script:state.diff_hash     = $pre.Hash
    $script:state.handoff_hash  = $script:audit.handoff_hash
    $script:state.override_hash = $script:audit.override_hash
    $script:state.verdict       = $parsed.Verdict
    $script:state.open_findings = $parsed.OpenFindings
    $script:state.fingerprint   = $fp
    $script:state.last_error    = ""

    if ($parsed.Verdict -ceq "APPROVED" -and $parsed.OpenFindings -eq 0) {
        $script:state.approved_hash   = $pre.Hash
        $script:state.approved_tree   = $tstate.Tree
        # Approved, but NOT authorized. Only the owner can authorize a commit.
        $script:state.approval_status = $script:AUTH_ABSENT
        $script:state.in_flight       = $false
        Save-ReviewState $script:dir $script:state
        $pre.Hash | Out-File (Join-Path $script:dir "approved.hash") -Encoding utf8 -Force
        $script:audit.approval_status = "APPROVED_AWAITING_USER"
        Write-AuditRecord $script:dir $script:audit
        Write-ReviewSummary $script:dir $script:state $script:audit `
            "CONSENSUS REACHED. Commit authorization is ABSENT - the owner must authorize explicitly." $body
        Allow-Stop "Codex APPROVED round $round, 0 open findings. Approved hash $($pre.Hash.Substring(0,12)). Consensus does NOT authorize a commit; present the full summary and wait for explicit owner authorization."
    }

    $script:state.approved_hash   = ""
    $script:state.approved_tree   = ""
    $script:state.approval_status = $script:AUTH_ABSENT
    Save-ReviewState $script:dir $script:state
    Write-AuditRecord $script:dir $script:audit
    Write-ReviewSummary $script:dir $script:state $script:audit `
        "CHANGES REQUESTED - $($parsed.OpenFindings) open finding(s)." $body

    Deny-Stop @"
[CODEX REVIEW - ROUND $round] VERDICT: $($parsed.Verdict)  OPEN: $($parsed.OpenFindings)

$body

--- YOUR REQUIRED RESPONSE ---
Address EVERY open finding individually as ACCEPTED, PARTIALLY_ACCEPTED, or DISPUTED.
- ACCEPTED / PARTIALLY_ACCEPTED: explain why it is valid, apply the SMALLEST safe
  correction, add a regression test that FAILS without the fix, rerun tests, and
  record the result. Leave everything uncommitted.
- DISPUTED: give concrete evidence - exact code path, invariant, test output,
  runtime behavior, broker API behavior, or documentation. Opinion is not
  evidence. You may not unilaterally close a finding.
Never weaken a risk limit or edit a test to hide a defect. No unrelated refactors.
Update .codex-reviews/handoff.md, then end your message with the marker alone on
its final line.
"@
}
catch {
    Fail-Closed "unexpected exception in review path: $($_.Exception.Message)" `
        "The review did not complete. Script line: $($_.InvocationInfo.ScriptLineNumber)"
}
