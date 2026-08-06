# Regression tests for the Claude<->Codex review hooks.
# Run: powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_hooks.ps1
#
# Covers the round-1 Codex findings:
#   R1-COMMIT-INDEX    - approval must bind to the staged index, not worktree bytes
#   R1-COMMIT-MATCHER  - `git -C <path>` / `git.exe` commit forms must be denied
#   R1-COMPOUND-COMMIT - mutation+commit and double-commit must be denied
# Plus the marker/verdict/hash invariants.
#
# Uses throwaway git repos under TEMP. Never touches the real repo's state.
$ErrorActionPreference = "Stop"

$here  = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo  = Split-Path -Parent $here
$hooks = Join-Path $repo ".claude\hooks"
. (Join-Path $hooks "lib-review.ps1")

$script:pass = 0
$script:fail = 0
function Ok {
    param([bool]$Cond, [string]$Desc)
    if ($Cond) { $script:pass++; Write-Output "  PASS  $Desc" }
    else        { $script:fail++; Write-Output "  FAIL  $Desc" }
}

$gitExe = (Get-Command git).Source
# NOTE: the parameter must NOT be called $Args -- that is a PowerShell automatic
# variable, and binding to it silently drops every argument (git then received
# nothing and no repo was created).
function G {
    param([string]$Dir, [string[]]$GitArgs)
    # EAP must be Continue: PS 5.1 turns native stderr into NativeCommandError, so a
    # harmless git warning ("LF will be replaced by CRLF") would otherwise be fatal.
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $gitExe -C $Dir @GitArgs 2>$null | Out-Null }
    finally { $ErrorActionPreference = $old }
}

function New-TempRepo {
    $d = Join-Path $env:TEMP ("hooktest-" + [Guid]::NewGuid().ToString("N").Substring(0, 10))
    New-Item -ItemType Directory -Path $d | Out-Null
    G $d @("init", "-q")
    G $d @("config", "user.email", "t@example.com")
    G $d @("config", "user.name", "t")
    G $d @("config", "commit.gpgsign", "false")
    G $d @("config", "core.autocrlf", "false")   # keep git quiet about line endings
    [System.IO.File]::WriteAllText((Join-Path $d "a.txt"), "v0")
    [System.IO.File]::WriteAllText((Join-Path $d ".gitignore"), ".codex-reviews/`n")
    G $d @("add", "-A")
    G $d @("-c", "core.hooksPath=", "commit", "-q", "-m", "init")
    return $d
}

# Invoke the real PreToolUse guard and return its decision ("deny" / "allow").
function Invoke-Guard {
    param([string]$Root, [string]$Command, [string]$ToolName = "Bash")
    $payload = @{ tool_name = $ToolName; cwd = $Root
                  tool_input = @{ command = $Command } } | ConvertTo-Json -Compress -Depth 5
    $old = $env:CLAUDE_PROJECT_DIR
    $env:CLAUDE_PROJECT_DIR = $Root
    try {
        $out = $payload | & powershell -NoProfile -ExecutionPolicy Bypass `
                            -File (Join-Path $hooks "pretool-guard.ps1") 2>&1
        $txt = ($out | Out-String)
        if ($txt -match '"permissionDecision"\s*:\s*"deny"') { return "deny" }
        return "allow"
    } finally { $env:CLAUDE_PROJECT_DIR = $old }
}

function Set-State {
    param([string]$Root, [hashtable]$Fields)
    $dir = Get-ReviewDir $Root
    $s = New-ReviewState
    foreach ($k in $Fields.Keys) { $s.$k = $Fields[$k] }
    Save-ReviewState $dir $s
    return $s
}

Write-Output "=== 1. Commit-capable detection (R1-COMMIT-MATCHER) ==="
$mustMatch = @(
    'git commit -m "x"',
    'git -C "C:\repo" commit -m x',
    'git.exe commit -m x',
    'git --git-dir=.git --work-tree=. commit -m x',
    'git -c user.name=x commit -m x',
    'GIT_AUTHOR_NAME=x git commit --amend',
    'git commit -F -'
)
foreach ($c in $mustMatch) { Ok (Test-CommitCapable $c) "detects: $c" }
$mustNotMatch = @('git status', 'git log --oneline', 'py -3.12 -m pytest tests -q', 'git diff HEAD')
foreach ($c in $mustNotMatch) { Ok (-not (Test-CommitCapable $c)) "ignores: $c" }

Write-Output "=== 2. Compound-command analysis (R1-COMPOUND-COMMIT) ==="
$segs = Split-CommandSegments 'Set-Content main.py "x"; git add main.py; git commit -m y'
Ok ($segs.Count -eq 3) "splits 3 segments (got $($segs.Count))"
Ok (@($segs | Where-Object { Test-CommitCapable $_ }).Count -eq 1) "one commit segment found"
Ok (@($segs | Where-Object { (-not (Test-CommitCapable $_)) -and (Test-MutatingSegment $_) }).Count -ge 1) `
   "detects a non-commit mutating segment"
Ok (Test-MutatingSegment 'git add main.py') "git add is mutating"
Ok (Test-MutatingSegment 'Set-Content x.py "junk"') "Set-Content is mutating"
Ok (-not (Test-MutatingSegment 'git status')) "git status is not mutating"

Write-Output "=== 3. Index/worktree divergence (R1-COMMIT-INDEX) ==="
$r = New-TempRepo
try {
    # Stage version A, leave version B in the worktree, and add an untracked file.
    [System.IO.File]::WriteAllText((Join-Path $r "a.txt"), "A")
    G $r @("add", "a.txt")
    [System.IO.File]::WriteAllText((Join-Path $r "a.txt"), "B")
    [System.IO.File]::WriteAllText((Join-Path $r "new.txt"), "untracked")

    $ts = Get-CommitTreeState $r
    Ok (-not $ts.FullyStaged) "divergent index is NOT fully staged"
    Ok ($ts.Unstaged.Count -ge 1) "unstaged change detected (a.txt)"
    Ok ($ts.Untracked.Count -ge 1) "untracked file detected (new.txt)"
    Ok ($ts.Tree -eq "") "no tree hash offered while divergent"

    # Approval recorded against the divergent worktree must not permit a commit.
    $wt = Get-WorkTreeManifest $r
    Set-State $r @{ verdict = "APPROVED"; open_findings = 0; approved_hash = $wt.Hash
                    approved_tree = "deadbeef"; approval_status = "VALID" } | Out-Null
    Ok ((Invoke-Guard $r 'git commit -m x') -eq "deny")        "plain commit denied while divergent"
    Ok ((Invoke-Guard $r 'git commit -a -m x') -eq "deny")     "commit -a denied while divergent"
    # Parentheses matter: without them PowerShell binds only the first literal to
    # Invoke-Guard and concatenates afterwards, so the guard never sees "commit".
    $gcForm = 'git -C "' + $r + '" commit -m x'
    Ok ((Invoke-Guard $r $gcForm) -eq "deny") "git -C form denied"
    Ok ((Invoke-Guard $r 'git.exe commit -m x') -eq "deny")    "git.exe form denied"

    # Stage everything: now a tree exists, and the manifest hash is UNCHANGED
    # (staging must not alter what was reviewed).
    G $r @("add", "-A")
    $ts2 = Get-CommitTreeState $r
    $wt2 = Get-WorkTreeManifest $r
    Ok ($ts2.FullyStaged) "fully staged after git add -A"
    Ok ($ts2.Tree -ne "") "tree hash available when fully staged"
    Ok ($wt2.Hash -eq $wt.Hash) "staging does NOT change the manifest hash"

    # Correct tree + VALID authorization -> allowed.
    Set-State $r @{ verdict = "APPROVED"; open_findings = 0; approved_hash = $wt2.Hash
                    approved_tree = $ts2.Tree; approval_status = "VALID" } | Out-Null
    Ok ((Invoke-Guard $r 'git commit -m x') -eq "allow") "isolated commit of the reviewed tree allowed"

    # Compound forms must still be denied even with a valid authorization.
    Ok ((Invoke-Guard $r 'Set-Content a.txt "evil"; git commit -m x') -eq "deny") `
       "mutation + commit denied"
    Ok ((Invoke-Guard $r 'git commit -m one && git commit -m two') -eq "deny") `
       "two commits in one call denied"
    Ok ((Invoke-Guard $r 'git add evil.txt; git commit -m x') -eq "deny") `
       "git add + commit denied"

    # Wrong tree / wrong hash / bad authorization states must all deny.
    Set-State $r @{ verdict = "APPROVED"; open_findings = 0; approved_hash = $wt2.Hash
                    approved_tree = "0000000"; approval_status = "VALID" } | Out-Null
    Ok ((Invoke-Guard $r 'git commit -m x') -eq "deny") "stale approved_tree denied"

    Set-State $r @{ verdict = "APPROVED"; open_findings = 0; approved_hash = "deadbeef"
                    approved_tree = $ts2.Tree; approval_status = "VALID" } | Out-Null
    Ok ((Invoke-Guard $r 'git commit -m x') -eq "deny") "stale approved_hash denied"

    foreach ($st in @("ABSENT", "INVALIDATED", "CONSUMED")) {
        Set-State $r @{ verdict = "APPROVED"; open_findings = 0; approved_hash = $wt2.Hash
                        approved_tree = $ts2.Tree; approval_status = $st } | Out-Null
        Ok ((Invoke-Guard $r 'git commit -m x') -eq "deny") "authorization '$st' denied"
    }

    Set-State $r @{ verdict = "CHANGES_REQUESTED"; open_findings = 3
                    approved_hash = $wt2.Hash; approved_tree = $ts2.Tree
                    approval_status = "VALID" } | Out-Null
    Ok ((Invoke-Guard $r 'git commit -m x') -eq "deny") "open findings deny the commit"

    # Edit lock blocks mutations during review.
    Set-State $r @{ verdict = "APPROVED"; open_findings = 0; approved_hash = $wt2.Hash
                    approved_tree = $ts2.Tree; approval_status = "VALID" } | Out-Null
    $lock = Join-Path (Join-Path $r ".codex-reviews") "review.lock"
    [System.IO.File]::WriteAllText($lock, "locked")
    Ok ((Invoke-Guard $r 'git commit -m x') -eq "deny") "commit denied while review.lock exists"
    Ok ((Invoke-Guard $r 'x' "Edit") -eq "deny")        "Edit tool denied while review.lock exists"
    Ok ((Invoke-Guard $r 'git status') -eq "allow")     "read-only command allowed under lock"
    [System.IO.File]::Delete($lock)

    # No review state at all -> deny (always-on gate).
    [System.IO.File]::Delete((Join-Path (Join-Path $r ".codex-reviews") "state.json"))
    Ok ((Invoke-Guard $r 'git commit -m x') -eq "deny") "no review state denies the commit"
    Ok ((Invoke-Guard $r 'py -3.12 -m pytest tests -q') -eq "allow") "non-commit command allowed"
}
finally {
    if (Test-Path $r) { Remove-Item $r -Recurse -Force -ErrorAction SilentlyContinue }
}

Write-Output "=== 3b. Manifest must not fuse paths (single-line git output) ==="
$r2 = New-TempRepo
try {
    # Exactly ONE tracked change and ONE untracked file: the case where PowerShell
    # unwraps a single-element array and string-concatenates the two path lists.
    [System.IO.File]::WriteAllText((Join-Path $r2 "a.txt"), "changed")
    [System.IO.File]::WriteAllText((Join-Path $r2 "solo.txt"), "untracked")
    $m = Get-WorkTreeManifest $r2
    Ok ($m.Paths.Count -eq 2) "two paths reported, not fused (got $($m.Paths.Count): $($m.Paths -join '|'))"
    Ok ($m.Paths -contains "a.txt")    "a.txt present"
    Ok ($m.Paths -contains "solo.txt") "solo.txt present"
    foreach ($p in $m.Paths) { Ok (Test-Path (Join-Path $r2 $p)) "manifest path exists on disk: $p" }

    # Staging must not change the manifest hash: same paths, same bytes.
    $h1 = $m.Hash
    G $r2 @("add", "-A")
    Ok ((Get-WorkTreeManifest $r2).Hash -eq $h1) "staging does NOT change the manifest hash"

    # A single tracked change with NO untracked files must also survive.
    $r3 = New-TempRepo
    try {
        [System.IO.File]::WriteAllText((Join-Path $r3 "a.txt"), "only-one")
        $m3 = Get-WorkTreeManifest $r3
        Ok ($m3.Paths.Count -eq 1 -and $m3.Paths[0] -eq "a.txt") "lone tracked change handled"
    } finally { if (Test-Path $r3) { Remove-Item $r3 -Recurse -Force -ErrorAction SilentlyContinue } }
}
finally { if (Test-Path $r2) { Remove-Item $r2 -Recurse -Force -ErrorAction SilentlyContinue } }

Write-Output "=== 4. Marker exactness ==="
Ok (Test-ReviewMarker "done`n`n[CODEX_REVIEW_READY]")            "bare marker on final line"
Ok (Test-ReviewMarker "[CODEX_REVIEW_READY]`n`n   `n")           "trailing blank lines tolerated"
Ok (-not (Test-ReviewMarker "a`n[CODEX_REVIEW_READY]`nmore"))    "not final line -> no trigger"
Ok (-not (Test-ReviewMarker '```'+"`n[CODEX_REVIEW_READY]`n"+'```')) "inside code fence -> no trigger"
Ok (-not (Test-ReviewMarker "[codex_review_ready]"))             "lowercase -> no trigger"
Ok (-not (Test-ReviewMarker "[CODEX_REVIEW_READY] now"))         "trailing text -> no trigger"
Ok (-not (Test-ReviewMarker ""))                                 "empty -> no trigger"

Write-Output "=== 5. Verdict parsing ==="
Ok ((Parse-CodexVerdict "VERDICT: APPROVED`nOPEN_FINDINGS: 0").Valid)              "valid approval"
Ok ((Parse-CodexVerdict "VERDICT: CHANGES_REQUESTED`nOPEN_FINDINGS: 4").Valid)     "valid changes"
Ok (-not (Parse-CodexVerdict "VERDICT: APPROVED`nOPEN_FINDINGS: 2").Valid)         "approved+n>0 rejected"
Ok (-not (Parse-CodexVerdict "VERDICT: CHANGES_REQUESTED`nOPEN_FINDINGS: 0").Valid) "changes+0 rejected"
Ok (-not (Parse-CodexVerdict "VERDICT: APPROVED`nOPEN_FINDINGS: 0`nVERDICT: APPROVED`nOPEN_FINDINGS: 0").Valid) "duplicate verdicts rejected"
Ok (-not (Parse-CodexVerdict "Looks good").Valid)                                  "prose rejected"
Ok (-not (Parse-CodexVerdict "").Valid)                                            "empty rejected"
Ok (-not (Parse-CodexVerdict "VERDICT: approved`nOPEN_FINDINGS: 0").Valid)          "lowercase verdict rejected"
Ok (-not (Parse-CodexVerdict (Get-Content (Join-Path $repo "CODEX_REVIEW.md") -Raw)).Valid) `
   "the protocol file itself is not a verdict"

Write-Output "=== 6. Codex CLI resolution ==="
$exe = Resolve-CodexExe
Ok ($null -ne $exe) "codex resolved to a launchable Application ($exe)"
Ok ($exe -notmatch '\.ps1$') "does not resolve to the unlaunchable .ps1 shim"

Write-Output "=== 7. Child-process exit codes ==="
$tmpo = Join-Path $env:TEMP ("hk-" + [Guid]::NewGuid().ToString("N").Substring(0,8))
foreach ($code in @(0, 1, 3)) {
    $rc = Invoke-Child -Exe "cmd.exe" -Arguments @("/c", "exit $code") -StdinFile "" `
            -StdoutFile "$tmpo.out" -StderrFile "$tmpo.err" -TimeoutMs 10000
    Ok ($rc.ExitCode -eq $code) "exit code $code reported (not null)"
}
$rc = Invoke-Child -Exe "cmd.exe" -Arguments @("/c", "ping -n 6 127.0.0.1 >nul") -StdinFile "" `
        -StdoutFile "$tmpo.out" -StderrFile "$tmpo.err" -TimeoutMs 1200
Ok ($rc.TimedOut) "timeout reported"
foreach ($f in @("$tmpo.out", "$tmpo.err")) { if (Test-Path $f) { Remove-Item $f -Force } }

Write-Output "=== 8. Review-artifact snapshot copying (Codex NEW-SNAPSHOT-HISTORY-OMITTED) ==="
$revDir = Join-Path $env:TEMP ("rev-" + [Guid]::NewGuid().ToString("N").Substring(0,8))
$snapDir = Join-Path $env:TEMP ("snap-" + [Guid]::NewGuid().ToString("N").Substring(0,8))
New-Item -ItemType Directory -Path $revDir -Force | Out-Null
New-Item -ItemType Directory -Path $snapDir -Force | Out-Null
try {
    $handoffSrc = Join-Path $revDir "handoff.md"
    "# handoff referencing round-1.md" | Out-File $handoffSrc -Encoding utf8
    "VERDICT: CHANGES_REQUESTED`nOPEN_FINDINGS: 1`nsome prior finding text" |
        Out-File (Join-Path $revDir "round-1.md") -Encoding utf8
    "VERDICT: CHANGES_REQUESTED`nOPEN_FINDINGS: 1`nsome round-2 finding text" |
        Out-File (Join-Path $revDir "round-2.md") -Encoding utf8
    # A raw transcript log must NOT be copied -- it is noise, nothing references it.
    "raw codex stdout, hundreds of KB in real runs" |
        Out-File (Join-Path $revDir "round-2.log") -Encoding utf8
    '{"decision":"owner ruling"}' | Out-File (Join-Path $revDir "override.json") -Encoding utf8

    Copy-ReviewArtifactsToSnapshot -ReviewDir $revDir -Snapshot $snapDir -Handoff $handoffSrc

    $snapRev = Join-Path $snapDir ".codex-reviews"
    Ok (Test-Path (Join-Path $snapRev "handoff.md"))    "handoff.md copied into snapshot"
    Ok (Test-Path (Join-Path $snapRev "round-1.md"))    "round-1.md copied into snapshot (the referenced history)"
    Ok (Test-Path (Join-Path $snapRev "round-2.md"))    "round-2.md copied into snapshot"
    Ok (Test-Path (Join-Path $snapRev "override.json")) "override.json (owner ruling) copied into snapshot"
    Ok (-not (Test-Path (Join-Path $snapRev "round-2.log"))) "raw .log transcript NOT copied (noise, unreferenced)"
    $copiedText = Get-Content (Join-Path $snapRev "round-1.md") -Raw
    Ok ($copiedText -match "some prior finding text") "copied round-1.md has the real prior finding content"
} finally {
    Remove-Item -Recurse -Force $revDir -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $snapDir -ErrorAction SilentlyContinue
}

Write-Output "=== 9. Legacy state.json schema migration ==="
$stateDir = Join-Path $env:TEMP ("state-" + [Guid]::NewGuid().ToString("N").Substring(0,8))
New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
try {
    # A state.json written before `approved_tree` was added to the schema
    # (R1-COMMIT-INDEX). Loading it and then SETTING that property used to throw
    # (PSCustomObject cannot gain a property via plain assignment), which crashed
    # every review attempt and was misreported as "hook input missing or
    # malformed" (found 2026-08-04 -- this state shape is the exact one that
    # blocked the automation for a week).
    '{"round":0,"diff_hash":"","handoff_hash":"","override_hash":"","verdict":"","open_findings":-1,"fingerprint":"","approved_hash":"","approval_status":"ABSENT","authorized_phrase":"","authorized_at":"","authorized_session":"","last_error":"","in_flight":true,"head_at_approval":""}' |
        Out-File (Join-Path $stateDir "state.json") -Encoding utf8
    $legacy = Get-ReviewState $stateDir
    $threw = $false
    try { $legacy.approved_tree = "TEST_TREE" } catch { $threw = $true }
    Ok (-not $threw) "setting approved_tree on a migrated legacy state does not throw"
    Ok ($legacy.approved_tree -eq "TEST_TREE") "backfilled property actually holds the assigned value"
    Ok ($legacy.round -eq 0) "pre-existing fields are preserved, not clobbered, by the backfill"
} finally {
    Remove-Item -Recurse -Force $stateDir -ErrorAction SilentlyContinue
}

Write-Output ""
Write-Output "================================"
Write-Output "  PASS: $script:pass   FAIL: $script:fail"
Write-Output "================================"
if ($script:fail -gt 0) { exit 1 }
exit 0
