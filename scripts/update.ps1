<#
.SYNOPSIS
  Convert any PDFs waiting in a vendor folder's "new pdf" staging folder into
  this repo's RAG doc format, then refresh that vendor's docs/index.json and
  docs/README.md.

.DESCRIPTION
  For every folder at the repo root that has both a "docs" subfolder and a
  "new pdf" subfolder -- any corpus folder shaped that way is picked up
  automatically, so adding a new one needs no change here:

    1. For every *.pdf sitting in "<vendor>\new pdf\":
       - Reads an optional sidecar "<pdfname>.json" next to it for
         title / slug / dictionary / supersedes overrides.
       - Moves the PDF into "<vendor>\" (next to the other source PDFs).
       - Runs convert_manual.py on it to produce docs\<slug>\.
       - If the sidecar named a "supersedes" slug, removes that old
         docs\<slug>\ folder and records the swap in superseded.json.
    2. Runs build_index.py on the vendor folder to refresh docs\index.json
       and docs\README.md from whatever's actually on disk.

  A PDF with no sidecar still converts fine -- its title just defaults to a
  cleaned-up version of the filename, which you'll likely want to fix by
  hand afterward (edit docs\<slug>\manifest.json's "title", then rerun
  build_index.py to pick up the change in index.json / README.md).

.EXAMPLE
  # Drop new PDFs in "Tessent Manual\new pdf\" and/or "Synopsys Manual\new pdf\", then:
  .\scripts\update.ps1

.NOTES
  Sidecar JSON format ("<pdfname>.json", all fields optional):
    {
      "title": "Tessent(TM) New Thing User's Manual",
      "slug": "newthing-2026-2",
      "dictionary": true,
      "supersedes": "oldthing-2025-2"
    }
  - title:       shown in index.json / README.md. Default: the filename with
                 "_" / "-" / "." turned into spaces.
  - slug:        docs\<slug>\ folder name. Default: convert_manual.py's own
                 filename-based slug.
  - dictionary:  true for command-dictionary-style manuals (one entry per
                 command, like syn2 / tshell-ref).
  - supersedes:  an existing docs\<slug>\ to retire -- that folder is
                 deleted and a superseded.json entry is added pointing to
                 the new slug. The old PDF itself is left alone at the
                 vendor folder root.
#>

[CmdletBinding()]
param()

function Get-DefaultTitle {
    param([string]$Stem)
    $t = [regex]::Replace($Stem, '[_\-\.]+', ' ')
    return $t.Trim()
}

function Test-Dependencies {
    # Check python exists first -- otherwise a missing interpreter surfaces as
    # a confusing CommandNotFoundException with a stale $LASTEXITCODE.
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        throw "python not found on PATH. Install Python 3, reopen the shell, and retry."
    }
    python -c "import pymupdf4llm" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing scripts/requirements.txt ..."
        python -m pip install -q -r (Join-Path $PSScriptRoot "requirements.txt")
        if ($LASTEXITCODE -ne 0) {
            throw "pip install failed -- run 'pip install -r scripts\requirements.txt' by hand and retry."
        }
    }
}

function Add-SupersededEntry {
    param(
        [string]$VendorDir,
        [string]$OldSlug,
        [string]$NewSlug
    )
    $oldDocsDir = Join-Path (Join-Path $VendorDir "docs") $OldSlug
    $oldManifestPath = Join-Path $oldDocsDir "manifest.json"
    if (-not (Test-Path -LiteralPath $oldManifestPath)) {
        Write-Warning "  supersedes '$OldSlug' has no docs\$OldSlug\manifest.json -- skipping retirement."
        return
    }

    $oldManifest = Get-Content -LiteralPath $oldManifestPath -Raw | ConvertFrom-Json
    $supersededPath = Join-Path $VendorDir "superseded.json"
    $list = New-Object System.Collections.Generic.List[object]
    if (Test-Path -LiteralPath $supersededPath) {
        $existing = Get-Content -LiteralPath $supersededPath -Raw | ConvertFrom-Json
        foreach ($item in @($existing)) { $list.Add($item) }
    }
    $list.Add([PSCustomObject]@{ file = $oldManifest.source_pdf; superseded_by = $NewSlug })

    $json = ConvertTo-Json -InputObject $list.ToArray() -Depth 5
    # Windows PowerShell 5.1's -Encoding utf8 always writes a BOM, which
    # trips up Python's json.loads -- write true no-BOM UTF-8 via .NET instead.
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($supersededPath, $json, $utf8NoBom)

    Remove-Item -LiteralPath $oldDocsDir -Recurse -Force
    Write-Host "  retired docs\$OldSlug -> superseded.json (old PDF left in place)"
}

function Update-Vendor {
    param([string]$VendorDir)

    $vendorName = Split-Path -Leaf $VendorDir
    $stagingDir = Join-Path $VendorDir "new pdf"
    $pdfs = @(Get-ChildItem -LiteralPath $stagingDir -Filter *.pdf -File -ErrorAction SilentlyContinue)
    if ($pdfs.Count -eq 0) {
        Write-Host "[$vendorName] no PDFs waiting in 'new pdf\' -- skipping."
        return
    }

    $convertedAny = $false
    foreach ($pdf in $pdfs) {
        Write-Host ""
        Write-Host "[$vendorName] $($pdf.Name)"

        $destPdf = Join-Path $VendorDir $pdf.Name
        if (Test-Path -LiteralPath $destPdf) {
            Write-Warning "  already exists at '$vendorName\$($pdf.Name)' -- skipping (remove one copy and rerun)."
            continue
        }

        $sidecarPath = Join-Path $stagingDir ($pdf.BaseName + ".json")
        $meta = $null
        if (Test-Path -LiteralPath $sidecarPath) {
            try {
                $meta = Get-Content -LiteralPath $sidecarPath -Raw | ConvertFrom-Json -ErrorAction Stop
            } catch {
                Write-Warning "  couldn't parse $($pdf.BaseName).json ($($_.Exception.Message)) -- using filename defaults."
            }
        }

        $title = Get-DefaultTitle $pdf.BaseName
        if ($meta -and $meta.title) { $title = $meta.title }

        $dictionary = $false
        if ($meta -and $meta.dictionary) { $dictionary = $true }

        $supersedes = $null
        if ($meta -and $meta.supersedes) { $supersedes = $meta.supersedes }

        try {
            Move-Item -LiteralPath $pdf.FullName -Destination $destPdf -ErrorAction Stop
        } catch {
            Write-Warning "  couldn't move $($pdf.Name) into '$vendorName\' ($($_.Exception.Message)) -- skipping."
            continue
        }

        $convertArgs = New-Object System.Collections.Generic.List[string]
        $convertArgs.Add($destPdf)
        $convertArgs.Add("--title"); $convertArgs.Add($title)
        if ($meta -and $meta.slug) { $convertArgs.Add("--slug"); $convertArgs.Add($meta.slug) }
        if ($dictionary) { $convertArgs.Add("--dictionary") }

        Write-Host "  converting (title: '$title')..."
        # Deliberately NOT assigning this pipeline ($x = ...) -- that would
        # suppress live console output and buffer everything until the end,
        # which is bad news on a 1000+ page PDF. Tee-Object -Variable alone
        # both streams to the console as it happens and captures the lines.
        python (Join-Path $PSScriptRoot "convert_manual.py") @convertArgs | Tee-Object -Variable convertOutput
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            Write-Warning "  conversion failed -- $($pdf.Name) is now at '$vendorName\$($pdf.Name)' unconverted; fix and rerun."
            continue
        }
        $convertedAny = $true

        $newSlug = $null
        $wroteLine = $convertOutput | Where-Object { $_ -match '^Wrote \d+ sections to (.+)$' } | Select-Object -First 1
        if ($wroteLine -match '^Wrote \d+ sections to (.+)$') {
            $newSlug = Split-Path -Leaf $Matches[1].Trim()
        }

        if ($supersedes) {
            if ($newSlug) {
                Add-SupersededEntry -VendorDir $VendorDir -OldSlug $supersedes -NewSlug $newSlug
            } else {
                Write-Warning "  couldn't determine the new slug from convert_manual.py's output -- skipping retirement of '$supersedes'."
            }
        }

        if (Test-Path -LiteralPath $sidecarPath) {
            Remove-Item -LiteralPath $sidecarPath
        }
    }

    if ($convertedAny) {
        Write-Host ""
        Write-Host "[$vendorName] refreshing docs/index.json and docs/README.md ..."
        python (Join-Path $PSScriptRoot "build_index.py") $VendorDir
    }
}

$repoRoot = Split-Path -Parent $PSScriptRoot

Test-Dependencies

$vendorDirs = @(Get-ChildItem -LiteralPath $repoRoot -Directory | Where-Object {
    (Test-Path -LiteralPath (Join-Path $_.FullName "docs")) -and
    (Test-Path -LiteralPath (Join-Path $_.FullName "new pdf"))
})

if ($vendorDirs.Count -eq 0) {
    Write-Host "No vendor folder with both a 'docs\' and a 'new pdf\' subfolder found under $repoRoot."
    exit 0
}

foreach ($v in $vendorDirs) {
    Update-Vendor -VendorDir $v.FullName
}

Write-Host ""
Write-Host "Done. Remember to add any new slugs to that vendor's CLAUDE.md 'Manuals and what they cover' table."
