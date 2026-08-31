param(
    [Parameter(Mandatory = $true)][string]$InputPath,
    [Parameter(Mandatory = $true)][string]$OutputPath
)

$ErrorActionPreference = 'Stop'
$source = (Resolve-Path -LiteralPath $InputPath).Path
if ([System.IO.Path]::GetExtension($source).ToLowerInvariant() -ne '.xls') {
    throw "Input must be a legacy .xls workbook: $source"
}
$target = [System.IO.Path]::GetFullPath($OutputPath)
if (Test-Path -LiteralPath $target) {
    throw "Output already exists: $target"
}
$targetParent = [System.IO.Path]::GetDirectoryName($target)
if (-not (Test-Path -LiteralPath $targetParent -PathType Container)) {
    throw "Output parent does not exist: $targetParent"
}

$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false
$book = $null
try {
    $book = $excel.Workbooks.Open($source, 0, $true)
    $book.SaveAs($target, 51)
    $book.Close($false)
    $book = $null
}
finally {
    if ($null -ne $book) {
        $book.Close($false)
        [System.Runtime.InteropServices.Marshal]::ReleaseComObject($book) | Out-Null
    }
    $excel.Quit()
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($excel) | Out-Null
}

if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
    throw "Excel conversion did not produce output: $target"
}
