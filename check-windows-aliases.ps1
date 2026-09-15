# Windows App Execution Alias preflight for Codex sandbox delegation.
# This script is read-only and compatible with Windows PowerShell 5.1.
#
# Optional: -TemplateDir <path> (or $env:HARNESS_TEMPLATE_DIR) points at the
# tagteam template checkout; harness files in THIS project are then
# hash-compared against it and every divergence is listed as DRIFT (INFO —
# drift can be deliberate, so it never fails the preflight; it only makes
# the hand-sync debt visible, which is the Plugins re-evaluation signal).
param(
    [string]$TemplateDir = $env:HARNESS_TEMPLATE_DIR,
    [switch]$Launchers
)

$warningCount = 0
$windowsAppsPattern = '\\Microsoft\\WindowsApps\\'

function Test-WindowsAppsPath {
    param([string]$Path)

    if ([string]::IsNullOrEmpty($Path)) {
        return $false
    }

    return ($Path -match $windowsAppsPattern)
}

# <SystemRoot>\System32\bash.exe is the WSL launcher: a real file outside
# WindowsApps, present whenever WSL is enabled, and ahead of Git on the
# default system PATH. It cannot run Windows-path scripts either
# (execvpe(/bin/bash) failed, observed 2026-09-10); stop_gate.py skips it.
function Test-WslLauncherPath {
    param([string]$Path)

    if ([string]::IsNullOrEmpty($Path)) {
        return $false
    }

    return ($Path -match '\\Windows\\(System32|Sysnative)\\bash\.exe$')
}

function Test-NonPosixBashPath {
    param([string]$Path)

    return ((Test-WindowsAppsPath $Path) -or (Test-WslLauncherPath $Path))
}

function Get-ExecutableLocations {
    param([string]$Name)

    $locations = @(where.exe $Name 2>$null)
    return @($locations | Where-Object { $_ -and ($_ -match '\.exe$') })
}

function Show-PythonAliasCheck {
    param(
        [string]$Name,
        [switch]$RequiredForHooks
    )

    $locations = @(Get-ExecutableLocations $Name)
    if ($locations.Count -eq 0) {
        if ($RequiredForHooks) {
            $script:warningCount++
            Write-Host "WARN: $Name was not found on PATH."
            Write-Host "      The PreToolUse guard hook is invoked as '$Name'; without it every"
            Write-Host "      harness prohibition (bypass flags, force push, subagent commits) is"
            Write-Host "      silently inert while this check would otherwise look green."
            Write-Host "      Fix: install Python so '$Name' resolves on PATH, or change the hook"
            Write-Host "      command in .claude/settings.json to an interpreter that exists."
        }
        else {
            Write-Host "INFO: $Name was not found on PATH (expected on Windows; use 'python' or 'py')."
        }
        return
    }
    $stubLocations = @($locations | Where-Object { Test-WindowsAppsPath $_ })
    $realLocations = @($locations | Where-Object { -not (Test-WindowsAppsPath $_) })

    if ($stubLocations.Count -eq 0) {
        Write-Host "OK: $Name has no Microsoft WindowsApps stub."
    }
    elseif ($realLocations.Count -eq 0) {
        $script:warningCount++
        Write-Host "WARN: $Name resolves only to Microsoft WindowsApps alias(es): $($stubLocations -join ', ')"
        Write-Host "      Invoking it opens Microsoft Store and cannot execute under the Codex sandbox token."
        Write-Host "      Fix: disable its App Execution Alias: Settings > Apps > Advanced app settings > App execution aliases."
        Write-Host "      On Windows, use 'python' or 'py', never 'python3'."
    }
    elseif (Test-WindowsAppsPath $locations[0]) {
        $script:warningCount++
        Write-Host "WARN: $Name resolves to a Microsoft WindowsApps alias before a real install: $($locations -join ', ')"
        Write-Host "      Disable its App Execution Alias: Settings > Apps > Advanced app settings > App execution aliases."
        Write-Host "      On Windows, use 'python' or 'py', never 'python3'."
    }
    else {
        Write-Host "INFO: $Name resolves to a real install first; Microsoft WindowsApps stub also present."
    }
}

# Check pwsh first because Codex sandbox delegation requires PowerShell 7.
$pwshLocations = @(Get-ExecutableLocations 'pwsh')
if ($pwshLocations.Count -eq 0) {
    $warningCount++
    Write-Host "WARN: pwsh was not found. This harness needs PowerShell 7."
}
elseif (Test-WindowsAppsPath $pwshLocations[0]) {
    $warningCount++
    Write-Host "WARN: pwsh first resolves to Microsoft WindowsApps: $($pwshLocations[0])"
    Write-Host "      1. Install a GitHub release: winget install Microsoft.PowerShell --scope machine (admin), or extract the release ZIP to %LOCALAPPDATA%\Programs\pwsh7 and prepend it to user Path."
    Write-Host "      2. Remove the Store package: Get-AppxPackage Microsoft.PowerShell | Remove-AppxPackage"
    Write-Host "      3. Delete the dangling alias: %LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe"
}
else {
    Write-Host "OK: pwsh first resolves outside Microsoft WindowsApps: $($pwshLocations[0])"
}

Show-PythonAliasCheck 'python' -RequiredForHooks
Show-PythonAliasCheck 'python3'

# bash: the Stop-hook gate (stop_gate.py) and the codex launcher run
# verify scripts through bash. The WindowsApps bash.exe is the WSL
# launcher stub — it strips backslashes from Windows paths and exits 127.
# stop_gate.py skips that stub itself, so a real bash elsewhere is enough;
# no real bash at all leaves the gate failing closed on every turn end.
$bashLocations = @(Get-ExecutableLocations 'bash')
$realBash = @($bashLocations | Where-Object { -not (Test-NonPosixBashPath $_) })
if ($realBash.Count -eq 0) {
    # Same standard install paths stop_gate.py's find_bash() falls back to.
    $fallbackBash = @(
        "$env:ProgramFiles\Git\bin\bash.exe",
        "$env:ProgramFiles\Git\usr\bin\bash.exe",
        "${env:ProgramFiles(x86)}\Git\bin\bash.exe"
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    if ($fallbackBash) {
        Write-Host "INFO: no usable bash on PATH; the harness resolves $fallbackBash instead (stop_gate.py find_bash fallback)."
        Write-Host "      For scripts that call bare 'bash' from a Windows shell, use the route's 'shell' path or put Git\bin earlier on PATH."
    }
    else {
        $warningCount++
        Write-Host "WARN: no usable bash was found on PATH or in the standard Git-for-Windows install paths."
        Write-Host "      Install Git for Windows; the Stop gate cannot run verify scripts without it."
    }
}
elseif (Test-NonPosixBashPath $bashLocations[0]) {
    Write-Host "INFO: bash resolves to the WSL stub/launcher before a real install: $($bashLocations -join ', ')"
    Write-Host "      stop_gate.py/find_bash skip it (2026-09-02 stub, 2026-09-10 System32 launcher); scripts that call bare 'bash' from a Windows shell still hit it — prefer Git Bash or put Git\bin earlier on the SYSTEM PATH."
}
else {
    Write-Host "OK: bash first resolves to a real install: $($bashLocations[0])"
}

$windowsAppsDirectory = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps'
$zeroByteAliases = @(Get-ChildItem -LiteralPath $windowsAppsDirectory -File -Filter '*.exe' -ErrorAction SilentlyContinue | Where-Object { $_.Length -eq 0 } | ForEach-Object { $_.Name })
if ($zeroByteAliases.Count -eq 0) {
    Write-Host "INFO: 0-byte WindowsApps EXE aliases: <none>"
}
else {
    Write-Host "INFO: 0-byte WindowsApps EXE aliases: $($zeroByteAliases -join ', ')"
}

# Hook liveness. A green alias check proves nothing if a hook never runs,
# so require every hook's self-test to report success.
$hookNames = @('deny_dangerous.py', 'stop_gate.py')
foreach ($hookName in $hookNames) {
    $hookPath = Join-Path $PSScriptRoot (Join-Path '.claude/hooks' $hookName)
    if (-not (Test-Path -LiteralPath $hookPath)) {
        $warningCount++
        Write-Host "WARN: Hook $hookName not found at $hookPath."
        continue
    }

    # A SyntaxWarning today is a SyntaxError in the next Python, and a hook
    # that fails to compile is fail-open on both hosts (for example a
    # `\;` inside a docstring). `-W error` finds it now.
    $compileOk = $false
    try {
        & python -W error -m py_compile $hookPath 2>&1 | Out-Null
        $compileOk = ($LASTEXITCODE -eq 0)
    }
    catch { $compileOk = $false }
    if (-not $compileOk) {
        $warningCount++
        Write-Host "WARN: Hook $hookName does not compile under 'python -W error' -- a warning that will become a SyntaxError and silently disable the guard."
        Write-Host "      Run: python -W error -m py_compile $hookPath"
        continue
    }

    $selfTest = ''
    $selfTestOk = $false
    try {
        $selfTest = (& python $hookPath --self-test 2>&1) -join ' '
        $selfTestOk = ($LASTEXITCODE -eq 0) -and ($selfTest -match 'SELFTEST_OK')
    }
    catch {
        $selfTest = $_.Exception.Message
    }
    if ($selfTestOk) {
        Write-Host "OK: Hook $hookName passed its self-test."
    }
    else {
        $warningCount++
        Write-Host "WARN: Hook $hookName did NOT pass its self-test."
        Write-Host "      Output: $selfTest"
    }
}

# norm: markers -- CLAUDE.md and AGENTS.md carry the same normative items,
# paired by `<!-- norm:<name> -->` markers. Each marker must appear exactly
# once in BOTH files, or the two hosts' rules have drifted. The templates
# are checked where present, the rendered files otherwise (a consumer
# project has only those).
foreach ($pair in @(@('CLAUDE.md.template', 'AGENTS.md.template'), @('CLAUDE.md', 'AGENTS.md'))) {
    $pairPaths = $pair | ForEach-Object { Join-Path $PSScriptRoot $_ }
    if (-not ((Test-Path -LiteralPath $pairPaths[0]) -and (Test-Path -LiteralPath $pairPaths[1]))) { continue }
    $markerRegex = [regex]'<!-- norm:([a-z-]+) -->'
    $texts = $pairPaths | ForEach-Object { [System.IO.File]::ReadAllText($_) }
    $markers = @($texts | ForEach-Object { $markerRegex.Matches($_) | ForEach-Object { $_.Groups[1].Value } } | Sort-Object -Unique)
    if ($markers.Count -eq 0) {
        Write-Host "INFO: no norm: markers in $($pair[0]) / $($pair[1]) (files predate the pairing); skipped."
        break
    }
    $drift = @()
    foreach ($m in $markers) {
        for ($k = 0; $k -lt 2; $k++) {
            $n = ([regex]::Matches($texts[$k], [regex]::Escape("<!-- norm:$m -->"))).Count
            if ($n -ne 1) { $drift += "$($pair[$k]):$m=$n" }
        }
    }
    if ($drift.Count -eq 0) {
        Write-Host "OK: norm: marker names agree (not semantic equivalence) between $($pair[0]) and $($pair[1]) ($($markers.Count) markers, each exactly once in both)."
    }
    else {
        $warningCount++
        Write-Host "WARN: norm: markers drift between $($pair[0]) and $($pair[1]) (expected exactly 1 per file): $($drift -join ' ')"
    }
    break
}

# Behavioral host-role checks are separate from matching norm marker names.
$hostRouteTests = Join-Path $PSScriptRoot '.claude/scripts/test_host_routes.py'
if (Test-Path -LiteralPath $hostRouteTests) {
    & python $hostRouteTests
    if ($LASTEXITCODE -eq 0) {
        Write-Host 'OK: host routing and lifecycle scenarios passed.'
    }
    else {
        $warningCount++
        Write-Host 'WARN: host routing/lifecycle scenarios failed; check entry instructions and bindings.'
    }
}
else {
    $warningCount++
    Write-Host 'WARN: missing .claude/scripts/test_host_routes.py (host parity not checked).'
}

# Codex hook trust: the mirrored guards run only once trusted.
# Codex records trust in ~/.codex/config.toml under
#   [hooks.state.'<ABSOLUTE hooks.json path>:<event>:<entry>:<handler>']
# keyed by absolute path, one entry PER EVENT. Read events from the installed
# definition so new hooks are not silently omitted. This checks registration
# presence only, not enabled state, the current definition hash, or execution.
$codexHooks = Join-Path $PSScriptRoot '.codex/hooks.json'
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$codexConfig = Join-Path $codexHome 'config.toml'
if (-not (Test-Path -LiteralPath $codexHooks)) {
    Write-Host "INFO: no .codex/hooks.json in this checkout (Codex host guards not installed)."
}
elseif (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    Write-Host "INFO: .codex/hooks.json present but codex is not on PATH; trust check skipped."
}
elseif (-not (Test-Path -LiteralPath $codexConfig)) {
    $warningCount++
    Write-Host "WARN: Codex hook trust not registered: $codexConfig does not exist."
    Write-Host "      Run codex in $PSScriptRoot once and trust the configured hooks with /hooks (manual: install section, Codex host)."
}
else {
    try {
        $definition = Get-Content -LiteralPath $codexHooks -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        if ($definition.hooks -isnot [System.Management.Automation.PSCustomObject]) { throw 'hooks must be an object' }
        $eventNames = @($definition.hooks.PSObject.Properties | ForEach-Object { $_.Name })
        if ($eventNames.Count -eq 0) { throw 'hooks must not be empty' }
        $events = @(foreach ($name in $eventNames) {
            if ($name -cnotmatch '^[A-Z][A-Za-z0-9]*$') { throw 'invalid hook event name' }
            $name = $name -creplace '([A-Z]+)([A-Z][a-z])', '$1_$2'
            ($name -creplace '([a-z0-9])([A-Z])', '$1_$2').ToLowerInvariant()
        })
        $hooksKey = ((Resolve-Path -LiteralPath $codexHooks).Path -replace '\\', '/').ToLowerInvariant()
        $configText = ((Get-Content -LiteralPath $codexConfig -Raw -ErrorAction Stop) -replace '\\', '/').ToLowerInvariant()
        $missing = @()
        foreach ($event in $events) {
            if ($configText -notmatch [regex]::Escape("$hooksKey" + ":$event" + ":")) {
                $missing += $event
            }
        }
        if ($missing.Count -gt 0) {
            $warningCount++
            Write-Host "WARN: Codex hook trust missing for: $($missing -join ' ') (registration entries absent)."
            Write-Host "      Run codex in $PSScriptRoot once and trust the configured hooks with /hooks; re-trust after every edit of .codex/hooks.json."
        }
        else {
            Write-Host "OK: Codex hook registration entries found for: $($eventNames -join ', ')."
            Write-Host 'INFO: Registration presence only; enabled state, current hash and actual hook execution are not verified.'
        }
    }
    catch {
        $warningCount++
        Write-Host "WARN: Cannot check Codex hook registrations; check $codexHooks and $codexConfig."
    }
}

# Launcher liveness: the launchers carry the strongest
# gates (grant, full-access, control plane, busy guard) and are bash, so
# their regression suite runs through the first REAL bash (the WindowsApps
# stub cannot run it). Stub CLIs only — no quota, no network.
# Opt-in (-Launchers): consumers may omit the maintainer suite. Missing
# tests are a warning only when requested; hook and host checks still run.
$launcherTest = Join-Path $PSScriptRoot '.claude/scripts/test_launchers.sh'
if (-not (Test-Path -LiteralPath $launcherTest)) {
    if ($Launchers) {
        $warningCount++
        Write-Host "WARN: requested launcher regression suite not found at $launcherTest."
    }
    else {
        Write-Host 'INFO: optional launcher regression suite not installed; see the template maintenance guide.'
    }
}
elseif (-not $Launchers) {
    Write-Host "INFO: launcher regression suite present but not run (add -Launchers for all groups; or: bash .claude/scripts/test_launchers.sh)."
}
elseif ($realBash.Count -eq 0) {
    Write-Host "INFO: no real bash on PATH — launcher regression suite skipped (run it from Git Bash: bash .claude/scripts/test_launchers.sh)."
}
else {
    $launcherOut = ''
    $launcherOk = $false
    try {
        $launcherOut = (& $realBash[0] $launcherTest 2>&1) -join "`n"
        $launcherOk = ($LASTEXITCODE -eq 0) -and ($launcherOut -match 'ALL PASS')
    }
    catch {
        $launcherOut = $_.Exception.Message
    }
    if ($launcherOk) {
        Write-Host "OK: launcher regression suite passed ($(($launcherOut -split "`n" | Select-String 'ALL PASS' | Select-Object -Last 1)))."
    }
    else {
        $warningCount++
        Write-Host "WARN: launcher regression suite did NOT pass."
        Write-Host "      Tail: $((($launcherOut -split "`n") | Select-Object -Last 6) -join ' | ')"
    }
}

# Delegation CLIs (INFO only). A missing binary is the documented
# CODEX_UNAVAILABLE / AGY_UNAVAILABLE fallback, not a broken harness, so it
# never counts toward the exit code. Get-Command (not where.exe) because
# these may resolve to .cmd/.ps1 shims rather than .exe.
foreach ($cliName in @('codex', 'agy')) {
    $cli = Get-Command $cliName -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cli) {
        Write-Host "INFO: $cliName resolves to $($cli.Source)"
    }
    else {
        Write-Host "INFO: $cliName was not found on PATH (delegation to it falls back; see docs/orchestration/delegation-matrix.md)."
    }
}

# agy global grant (security-boundary.md). agy-run.sh refuses a
# command(...) grant at launch time, but that settings file lives outside
# the repo (no drift check covers it) and the launcher protects nothing
# when agy is run by hand, so a command grant is a WARN here. A missing
# file or missing write_file grant is INFO: it only means the agy lane
# falls back with AGY_UNAVAILABLE. Same path/override as agy-run.sh.
$agySettings = if ($env:HARNESS_AGY_SETTINGS) { $env:HARNESS_AGY_SETTINGS } else { Join-Path $HOME '.gemini\antigravity-cli\settings.json' }
if (-not (Test-Path -LiteralPath $agySettings -PathType Leaf)) {
    Write-Host "INFO: agy global settings not found at $agySettings (headless agy auto-approval unconfigured; agy lane -> AGY_UNAVAILABLE). Minimal file: docs/harness-manual.md install step 1."
}
else {
    # Structural parse of permissions.allow, like agy-run.sh: a text match
    # would also flag a `command(*)` DENY entry.
    $agyAllow = $null
    try {
        $agyJson = [System.IO.File]::ReadAllText($agySettings) | ConvertFrom-Json -ErrorAction Stop
        $agyAllow = @($agyJson.permissions.allow | ForEach-Object { "$_".Trim() })
    }
    catch {
        $agyAllow = $null
    }
    if ($null -eq $agyAllow) {
        Write-Host "INFO: $agySettings is not parseable JSON; agy-run.sh will report AGY_UNAVAILABLE."
    }
    elseif ($agyAllow | Where-Object { $_ -like 'command(*' }) {
        $warningCount++
        Write-Host "WARN: $agySettings grants command(...) to headless agy."
        Write-Host "      An injection in any file agy reads becomes command execution (security-boundary.md)."
        Write-Host "      agy-run.sh refuses to launch with this grant (HARNESS_DENIED), and running agy by hand has no such gate."
        Write-Host "      Fix: remove the command(...) entry; the harness grant is write_file(*) only."
    }
    elseif ($agyAllow | Where-Object { $_ -like 'write_file(*' }) {
        Write-Host "OK: agy global grant is write_file only ($agySettings)."
    }
    else {
        Write-Host "INFO: $agySettings has no write_file grant; every agy lane is a write task, so agy-run.sh will report AGY_UNAVAILABLE."
    }
}

# Template drift (opt-in). Compares the harness files this project carries
# with the template checkout; consumer-side fixes that never flowed back
# (the 2026-09-02 review found three in one day) show up here as DRIFT.
if ($TemplateDir) {
    if (-not (Test-Path -LiteralPath $TemplateDir -PathType Container)) {
        Write-Host "INFO: TemplateDir '$TemplateDir' does not exist; skipping template drift check."
    }
    else {
        $driftGlobs = @('.claude/agents/*.md', '.claude/rules/*.md', 'docs/orchestration/*.md', '.claude/skills/*/SKILL.md',
                        '.claude/hooks/*.py', '.claude/scripts/*', '.claude/settings.json',
                        '.claude/sandbox-sensitive.json', 'check-windows-aliases.ps1', 'check-posix.sh',
                        '.codex/hooks.json')
        $driftCount = 0
        $comparedCount = 0
        $notInstalledCount = 0
        foreach ($glob in $driftGlobs) {
            $templateFiles = @(Get-ChildItem -Path (Join-Path $TemplateDir $glob) -File -ErrorAction SilentlyContinue)
            foreach ($templateFile in $templateFiles) {
                $relative = $templateFile.FullName.Substring($TemplateDir.TrimEnd('\', '/').Length).TrimStart('\', '/')
                $localFile = Join-Path $PSScriptRoot $relative
                if (-not (Test-Path -LiteralPath $localFile -PathType Leaf)) {
                    $notInstalledCount++
                    continue
                }
                $comparedCount++
                # Normalize line endings so autocrlf differences are not drift.
                $templateText = ([System.IO.File]::ReadAllText($templateFile.FullName)) -replace "`r`n", "`n"
                $localText = ([System.IO.File]::ReadAllText($localFile)) -replace "`r`n", "`n"
                if ($templateText -ne $localText) {
                    $driftCount++
                    Write-Host "DRIFT: $relative differs from the template."
                }
            }
        }
        Write-Host "INFO: template comparison covers installed files only; $notInstalledCount template files not installed (not an installation completeness check)."
        Write-Host "INFO: template drift: $driftCount of $comparedCount harness files differ from '$TemplateDir' (INFO only; reconcile deliberately in either direction)."
    }
}
else {
    Write-Host "INFO: template drift check skipped (pass -TemplateDir or set HARNESS_TEMPLATE_DIR to the tagteam checkout)."
}

Write-Host "SUMMARY: $warningCount warning(s)."
if ($warningCount -gt 0) {
    exit 1
}

exit 0
