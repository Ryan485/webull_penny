# Shared helpers for the Claude<->Codex consensus review hooks.
# PowerShell 5.1 compatible: no ternary, no ??, no -AsHashtable.
# ASCII only (Windows console is cp949).

$script:AUTH_ABSENT      = "ABSENT"
$script:AUTH_VALID       = "VALID"
$script:AUTH_INVALIDATED = "INVALIDATED"
$script:AUTH_CONSUMED    = "CONSUMED"

function Get-ReviewDir {
    param([string]$Root)
    $d = Join-Path $Root ".codex-reviews"
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d | Out-Null }
    return $d
}

function Get-Sha256Hex {
    param([byte[]]$Bytes)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace("-", "").ToLower() }
    finally { $sha.Dispose() }
}

function Get-StringHash {
    param([string]$Text)
    if ($null -eq $Text) { $Text = "" }
    return Get-Sha256Hex ([System.Text.Encoding]::UTF8.GetBytes($Text))
}

function Get-FileTextHash {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return "NONE" }
    return Get-StringHash (Get-Content -LiteralPath $Path -Raw -Encoding UTF8)
}

# Write UTF-8 WITHOUT a BOM. Out-File -Encoding utf8 on PS 5.1 emits a BOM, and a
# BOM fed to codex on stdin (or read back before a regex anchored with ^) breaks
# things silently.
function Write-Utf8NoBom {
    param([string]$Path, [string]$Text)
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

# Read text and strip a leading BOM, so '^' anchored regexes match the first line.
function Read-TextNoBom {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return "" }
    $t = [System.IO.File]::ReadAllText($Path)
    if ($null -eq $t) { return "" }
    return $t.TrimStart([char]0xFEFF)
}

# `codex` on Windows resolves to an extensionless npm shim / codex.ps1, neither of
# which Start-Process can launch ("%1 is not a valid Win32 application").
# Only codex.cmd is a real Application. Resolve it explicitly.
function Resolve-CodexExe {
    foreach ($n in @("codex.cmd", "codex.exe", "codex.bat")) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c -and $c.CommandType -eq "Application") { return $c.Source }
    }
    $c = Get-Command "codex" -ErrorAction SilentlyContinue
    if ($c -and $c.CommandType -eq "Application") { return $c.Source }
    return $null
}

# Run a child process with stdin from a file and a RELIABLE exit code.
# Start-Process -PassThru does NOT populate ExitCode in this environment (verified
# 2026-07-29: HasExited=True but ExitCode was $null even after WaitForExit()),
# and `$null -ne 0` is TRUE, which would fail every review including successes.
# System.Diagnostics.Process reports it correctly.
# Stdout/stderr are drained ASYNCHRONOUSLY before WaitForExit: codex is verbose
# and a full pipe buffer would otherwise deadlock the hook until timeout.
function Invoke-Child {
    param(
        [string]$Exe, [string[]]$Arguments,
        [string]$StdinFile, [string]$StdoutFile, [string]$StderrFile,
        [int]$TimeoutMs
    )
    $quoted = @()
    foreach ($a in $Arguments) {
        if ($a -match '[\s"]') { $quoted += ('"' + ($a -replace '"', '\"') + '"') }
        else { $quoted += $a }
    }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName               = $Exe
    $psi.Arguments              = ($quoted -join ' ')
    $psi.UseShellExecute        = $false
    $psi.CreateNoWindow         = $true
    $psi.RedirectStandardInput  = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true

    $p = [System.Diagnostics.Process]::Start($psi)
    $so = $p.StandardOutput.ReadToEndAsync()
    $se = $p.StandardError.ReadToEndAsync()
    try {
        if ($StdinFile -and (Test-Path -LiteralPath $StdinFile)) {
            $p.StandardInput.Write([System.IO.File]::ReadAllText($StdinFile))
        }
    } catch {}
    try { $p.StandardInput.Close() } catch {}

    $exited = $p.WaitForExit($TimeoutMs)
    if (-not $exited) {
        try { $p.Kill() } catch {}
        return @{ TimedOut = $true; ExitCode = $null }
    }
    try { Write-Utf8NoBom $StdoutFile $so.Result } catch {}
    try { Write-Utf8NoBom $StderrFile $se.Result } catch {}
    return @{ TimedOut = $false; ExitCode = $p.ExitCode }
}

# Run git and return stdout lines, with a RELIABLE exit code in $script:LastGitExit.
# PowerShell 5.1 wraps a native command's stderr lines in ErrorRecords
# (NativeCommandError). Under $ErrorActionPreference='Stop' that turns an ordinary
# git WARNING -- e.g. "LF will be replaced by CRLF" on this Windows repo -- into a
# terminating error, which would make hashing throw and fail every review. Forcing
# Continue locally keeps warnings non-fatal while still surfacing real exit codes.
function Invoke-Git {
    param([string]$Root, [string[]]$GitArgs)
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & git -C $Root @GitArgs 2>$null
        $script:LastGitExit = $LASTEXITCODE
        return @($out)
    } finally { $ErrorActionPreference = $old }
}

# Deterministic hash of EVERY change that would enter a commit.
# A manifest of raw file bytes -> immune to git's CRLF renormalization, so an
# identical tree always yields an identical hash. Gitignored files are excluded
# by --exclude-standard, so review artifacts cannot affect the hash.
# Throws on git failure so callers can fail closed.
function Get-WorkTreeManifest {
    param([string]$Root)
    # @() at the CALL SITE is mandatory: PowerShell unwraps a single-element array
    # on `return`, so a one-line git output arrives as a scalar STRING. Then
    # `$tracked + $untracked` is string concatenation, not array concatenation, and
    # two real paths silently fuse into one bogus path that hashes as DELETED.
    # (Observed 2026-07-29: {a.txt, new.txt} became "a.txtnew.txt".)
    $tracked = @(Invoke-Git $Root @("diff", "HEAD", "--name-only", "--no-renames"))
    if ($script:LastGitExit -ne 0) { throw "git diff HEAD failed in $Root" }
    $untracked = @(Invoke-Git $Root @("ls-files", "--others", "--exclude-standard"))
    if ($script:LastGitExit -ne 0) { throw "git ls-files failed in $Root" }

    # Outer @() is also mandatory: Sort-Object -Unique returns a SCALAR for a single
    # result, and indexing a string yields a [char] -- so Paths[0] would be 'a'
    # instead of 'a.txt'. Paths must always be an array for callers.
    $paths = @(@($tracked + $untracked) |
               Where-Object { $_ -and $_.Trim().Length -gt 0 } |
               Sort-Object -Unique)
    if ($paths.Count -eq 0) { return @{ Hash = "EMPTY"; Paths = @() } }

    $sb = New-Object System.Text.StringBuilder
    foreach ($p in $paths) {
        $full = Join-Path $Root $p
        if (Test-Path -LiteralPath $full -PathType Leaf) {
            $h = Get-Sha256Hex ([System.IO.File]::ReadAllBytes($full))
        } else {
            $h = "DELETED"
        }
        [void]$sb.Append($p).Append("`t").Append($h).Append("`n")
    }
    return @{ Hash = Get-StringHash $sb.ToString(); Paths = $paths }
}

# The commit gate must bind to what git will ACTUALLY commit: the INDEX.
# Hashing worktree bytes alone is not enough (R1-COMMIT-INDEX): a staged version A
# can be committed while Codex reviewed worktree version B, and `git commit -am`
# on a tree with untracked files commits only tracked modifications while still
# passing a worktree-hash check. So we REQUIRE a fully staged tree (no unstaged
# changes, no untracked non-ignored files) and bind approval to `git write-tree`,
# which is the exact tree a commit would record.
# Note: write-tree adds tree objects to .git/objects. That is inert (dangling
# until gc), does not touch the working tree, and cannot affect the diff hash.
function Get-CommitTreeState {
    param([string]$Root)
    # @() required at the call site -- see the note in Get-WorkTreeManifest.
    $unstaged = @(Invoke-Git $Root @("diff", "--name-only"))        # index vs worktree
    if ($script:LastGitExit -ne 0) { throw "git diff (unstaged) failed in $Root" }
    $untracked = @(Invoke-Git $Root @("ls-files", "--others", "--exclude-standard"))
    if ($script:LastGitExit -ne 0) { throw "git ls-files failed in $Root" }
    $unstaged  = @($unstaged  | Where-Object { $_ -and $_.Trim().Length -gt 0 })
    $untracked = @($untracked | Where-Object { $_ -and $_.Trim().Length -gt 0 })
    $fully = (($unstaged.Count -eq 0) -and ($untracked.Count -eq 0))
    $tree = ""
    if ($fully) {
        $tree = ([string](Invoke-Git $Root @("write-tree"))).Trim()
        if ($script:LastGitExit -ne 0 -or -not $tree) { throw "git write-tree failed in $Root" }
    }
    return @{ Tree = $tree; Unstaged = $unstaged; Untracked = $untracked; FullyStaged = $fully }
}

# Commit-capable detection. Deliberately BROAD (R1-COMMIT-MATCHER): the old
# pattern missed `git -C <path> commit`, `git.exe commit`, `--git-dir=...`, etc.
# Over-blocking is the safe direction for a capital-risk guard.
function Test-CommitCapable {
    param([string]$Command)
    if (-not $Command) { return $false }
    return [bool]([regex]::IsMatch($Command, '(?i)\bgit(\.exe)?\b[^\r\n]*?\bcommit\b'))
}

# Segments of a compound command, for R1-COMPOUND-COMMIT.
function Split-CommandSegments {
    param([string]$Command)
    if (-not $Command) { return @() }
    return @([regex]::Split($Command, '(&&|\|\||;|\||\r?\n)') |
             Where-Object { $_ -and ($_ -notmatch '^(&&|\|\||;|\|)$') -and $_.Trim().Length -gt 0 })
}

function Test-MutatingSegment {
    param([string]$Segment)
    if (-not $Segment) { return $false }
    return [bool]([regex]::IsMatch($Segment,
        '(?i)(\bgit(\.exe)?\b[^\r\n]*?\b(add|apply|checkout|reset|restore|stash|rm|mv|clean|update-index|write-tree)\b' +
        '|\bSet-Content\b|\bAdd-Content\b|\bOut-File\b|\bRemove-Item\b|\bMove-Item\b|\bCopy-Item\b' +
        '|\bNew-Item\b|\btee\b|\bsed\s+-i\b|\brm\s|\bmv\s|\bcp\s|>>|>)'))
}

# TRUE only when the trimmed final non-empty line is EXACTLY the marker.
# The marker inside a code block, quotation, or earlier paragraph does not fire.
function Test-ReviewMarker {
    param([string]$Text)
    if (-not $Text) { return $false }
    $lines = $Text -split "`r?`n"
    for ($i = $lines.Count - 1; $i -ge 0; $i--) {
        $t = $lines[$i].Trim()
        if ($t.Length -eq 0) { continue }
        return ($t -ceq '[CODEX_REVIEW_READY]')   # case-sensitive, exact
    }
    return $false
}

# Returns @{ Text = <last assistant text>; Parsed = <lines parsed>; Failed = <lines that would not parse> }
# so the caller can tell "read fine, no marker" apart from "could not read the
# transcript at all". A silently empty result must never be read as "no marker".
function Get-LastAssistantText {
    param([string]$TranscriptPath)
    $res = @{ Text = ""; Parsed = 0; Failed = 0 }
    if (-not $TranscriptPath -or -not (Test-Path -LiteralPath $TranscriptPath)) { return $res }
    # @() is load-bearing: Get-Content on a SINGLE-line file returns a scalar
    # string, and indexing a string yields a [char], which then blows up on
    # .TrimStart() and silently blanks the marker check. Single-line transcripts
    # happen at the start of a session.
    $lines = @(Get-Content -LiteralPath $TranscriptPath -Encoding UTF8)
    for ($i = $lines.Count - 1; $i -ge 0; $i--) {
        # PS 5.1 Get-Content leaves a UTF-8 BOM on the first line; ConvertFrom-Json
        # chokes on it, which would silently blank the marker check.
        $line = [string]$lines[$i]
        if (-not $line) { continue }
        $line = $line.TrimStart([char]0xFEFF).Trim()
        if ($line.Length -eq 0) { continue }
        $o = $null
        try { $o = $line | ConvertFrom-Json } catch { $res.Failed++; continue }
        $res.Parsed++
        if ($o.type -ne "assistant") { continue }
        $out = ""
        foreach ($c in @($o.message.content)) {
            # text only: the marker must never be matched inside thinking/tool_use
            if ($c.type -eq "text") { $out += $c.text + "`n" }
        }
        if ($out.Trim().Length -gt 0) { $res.Text = $out; return $res }
    }
    return $res
}

function New-ReviewState {
    return [pscustomobject]@{
        round = 0; diff_hash = ""; handoff_hash = ""; override_hash = ""
        verdict = ""; open_findings = -1; fingerprint = ""
        approved_hash = ""; approved_tree = ""; approval_status = $script:AUTH_ABSENT
        authorized_phrase = ""; authorized_at = ""; authorized_session = ""
        last_error = ""; in_flight = $false; head_at_approval = ""
    }
}

function Get-ReviewState {
    param([string]$ReviewDir)
    $f = Join-Path $ReviewDir "state.json"
    if (-not (Test-Path $f)) { return New-ReviewState }
    $loaded = $null
    try { $loaded = (Get-Content $f -Raw -Encoding UTF8 | ConvertFrom-Json) }
    catch { throw "state.json is unreadable or corrupt: $($_.Exception.Message)" }
    # Backfill: a state.json written before a schema field was added (e.g.
    # approved_tree, added for R1-COMMIT-INDEX) deserializes as a PSCustomObject
    # missing that NoteProperty. `$state.approved_tree = ...` then THROWS
    # (PSCustomObject, unlike a hashtable, cannot create a property via plain
    # assignment) -- discovered 2026-08-04 testing against a state.json that
    # predated this field, which crashed every review attempt with a misleading
    # "hook input missing or malformed" message from the outer catch.
    $fresh = New-ReviewState
    foreach ($prop in $fresh.PSObject.Properties.Name) {
        if (-not (Get-Member -InputObject $loaded -Name $prop -MemberType NoteProperty)) {
            Add-Member -InputObject $loaded -MemberType NoteProperty -Name $prop -Value $fresh.$prop
        }
    }
    return $loaded
}

function Save-ReviewState {
    param([string]$ReviewDir, $State)
    $target = Join-Path $ReviewDir "state.json"
    $tmp = Join-Path $ReviewDir "state.json.tmp"
    ($State | ConvertTo-Json -Depth 6) | Out-File $tmp -Encoding utf8 -Force
    if (Test-Path $target) { Remove-Item $target -Force }
    Move-Item $tmp $target -Force
}

# Append-only audit trail. Written on EVERY attempt, success or failure.
function New-AuditRecord {
    return [pscustomobject]@{
        timestamp = ""; round = 0; pre_diff_hash = ""; handoff_hash = ""
        override_hash = ""; codex_exit_code = ""; verdict = ""
        open_findings = -1; post_diff_hash = ""; index_tree = ""
        approval_status = $script:AUTH_ABSENT
        failure_reason = ""; snapshot_verified = $false
    }
}

function Write-AuditRecord {
    param([string]$ReviewDir, $Rec)
    $Rec.timestamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $line = ($Rec | ConvertTo-Json -Depth 6 -Compress)
    Add-Content -Path (Join-Path $ReviewDir "audit.jsonl") -Value $line -Encoding utf8
}

# --- Immutable review snapshot -----------------------------------------------
# A clean HEAD worktree plus a byte-exact copy of the hashed manifest. Built
# OUTSIDE the repo so it can never perturb the diff hash. Gitignored files
# (.env, did.bin, logs/) are absent by construction -- Codex never sees them.
function New-ReviewSnapshot {
    param([string]$Root, [string[]]$Paths)
    $dest = Join-Path $env:TEMP ("codex-rev-" + [Guid]::NewGuid().ToString("N").Substring(0, 12))
    & git -C $Root worktree add --detach $dest HEAD 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "git worktree add failed (exit $LASTEXITCODE)" }
    foreach ($p in $Paths) {
        $src = Join-Path $Root $p
        $dst = Join-Path $dest $p
        if (Test-Path -LiteralPath $src -PathType Leaf) {
            $d = Split-Path $dst -Parent
            if ($d -and -not (Test-Path -LiteralPath $d)) {
                New-Item -ItemType Directory -Path $d -Force | Out-Null
            }
            [System.IO.File]::WriteAllBytes($dst, [System.IO.File]::ReadAllBytes($src))
        } elseif (Test-Path -LiteralPath $dst) {
            Remove-Item -LiteralPath $dst -Force
        }
    }
    return $dest
}

function Copy-ReviewArtifactsToSnapshot {
    # Copies the gitignored review history into the snapshot so Codex can
    # independently audit what a handoff references, instead of trusting
    # Claude's paraphrase of it (Codex NEW-SNAPSHOT-HISTORY-OMITTED, round 1,
    # 2026-08-04: a handoff pointed at ".codex-reviews/round-1.md" for prior
    # finding text, but the snapshot builder only ever copied handoff.md, so
    # that file did not exist inside the sandbox the reviewer actually saw).
    # Only the parsed verdict reports (round-*.md) and any recorded owner
    # ruling are copied -- round-*.log/.log.err are raw multi-hundred-KB Codex
    # transcripts that nothing references and would only add noise.
    param([string]$ReviewDir, [string]$Snapshot, [string]$Handoff)
    $snapRev = Join-Path $Snapshot ".codex-reviews"
    if (-not (Test-Path $snapRev)) { New-Item -ItemType Directory -Path $snapRev -Force | Out-Null }
    Copy-Item $Handoff (Join-Path $snapRev "handoff.md") -Force
    Get-ChildItem -LiteralPath $ReviewDir -Filter "round-*.md" -File -ErrorAction SilentlyContinue |
        ForEach-Object { Copy-Item $_.FullName (Join-Path $snapRev $_.Name) -Force }
    $overrideFile = Join-Path $ReviewDir "override.json"
    if (Test-Path $overrideFile) { Copy-Item $overrideFile (Join-Path $snapRev "override.json") -Force }
}

function Remove-ReviewSnapshot {
    param([string]$Root, [string]$Dest)
    if (-not $Dest) { return }
    try { & git -C $Root worktree remove --force $Dest 2>&1 | Out-Null } catch {}
    if (Test-Path -LiteralPath $Dest) {
        try { Remove-Item -LiteralPath $Dest -Recurse -Force } catch {}
    }
    try { & git -C $Root worktree prune 2>&1 | Out-Null } catch {}
}

# --- Strict verdict parsing (any ambiguity => CHANGES_REQUESTED) -------------
function Parse-CodexVerdict {
    param([string]$Text)
    $res = [pscustomobject]@{
        Verdict = "CHANGES_REQUESTED"; OpenFindings = -1; Valid = $false; Reason = ""
    }
    if (-not $Text -or $Text.Trim().Length -eq 0) {
        $res.Reason = "empty Codex output"; return $res
    }
    $mV = [regex]::Matches($Text, '(?m)^\s*VERDICT:\s*(\S+)\s*$')
    $mO = [regex]::Matches($Text, '(?m)^\s*OPEN_FINDINGS:\s*(\S+)\s*$')
    if ($mV.Count -eq 0) { $res.Reason = "no 'VERDICT:' line"; return $res }
    if ($mO.Count -eq 0) { $res.Reason = "no 'OPEN_FINDINGS:' line"; return $res }
    if ($mV.Count -gt 1) { $res.Reason = "multiple VERDICT lines ($($mV.Count))"; return $res }
    if ($mO.Count -gt 1) { $res.Reason = "multiple OPEN_FINDINGS lines ($($mO.Count))"; return $res }

    $v = $mV[0].Groups[1].Value
    $o = $mO[0].Groups[1].Value
    if ($v -cne "APPROVED" -and $v -cne "CHANGES_REQUESTED") {
        $res.Reason = "unrecognized verdict token '$v'"; return $res
    }
    if ($o -notmatch '^\d+$') { $res.Reason = "OPEN_FINDINGS not an integer: '$o'"; return $res }
    $n = [int]$o
    $res.OpenFindings = $n
    if ($v -ceq "APPROVED" -and $n -ne 0) {
        $res.Reason = "contradictory: APPROVED with OPEN_FINDINGS: $n"; return $res
    }
    if ($v -ceq "CHANGES_REQUESTED" -and $n -eq 0) {
        $res.Reason = "contradictory: CHANGES_REQUESTED with OPEN_FINDINGS: 0"; return $res
    }
    $res.Verdict = $v
    $res.Valid = $true
    return $res
}

# --- Durable user-facing summary (not a transient systemMessage) -------------
function Write-ReviewSummary {
    param([string]$ReviewDir, $State, $Audit, [string]$Headline, [string]$Body)
    $approved = ""
    $status = $script:AUTH_ABSENT
    if ($State) { $approved = $State.approved_hash; $status = $State.approval_status }
    $t = @"
# Codex review summary

$Headline

| field | value |
|---|---|
| round | $($Audit.round) |
| verdict | $($Audit.verdict) |
| open findings | $($Audit.open_findings) |
| pre-review diff hash | $($Audit.pre_diff_hash) |
| post-review diff hash | $($Audit.post_diff_hash) |
| handoff hash | $($Audit.handoff_hash) |
| snapshot verified | $($Audit.snapshot_verified) |
| codex exit code | $($Audit.codex_exit_code) |
| approval status | $status |
| approved hash | $approved |
| failure reason | $($Audit.failure_reason) |

$Body

Audit trail: .codex-reviews/audit.jsonl
"@
    $t | Out-File (Join-Path $ReviewDir "REVIEW-SUMMARY.md") -Encoding utf8 -Force
}

function Deny-Stop {
    param([string]$Reason)
    (@{ decision = "block"; reason = $Reason } | ConvertTo-Json -Depth 3) | Write-Output
    exit 0
}

function Allow-Stop {
    param([string]$Note)
    if ($Note) { (@{ systemMessage = $Note } | ConvertTo-Json -Depth 3) | Write-Output }
    exit 0
}
