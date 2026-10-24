# A WinForms window whose buttons are only painted pixels, for the locator tests: UI Automation sees
# the window and no buttons, so an agent finds them by their text.
# Usage: drawnapp.ps1 OUT.json [-Aware]. Without -Aware the process is not DPI aware, so on a scaled
# monitor Windows stretches the window, as it does for many older apps. Writes
# {"ready", "clicked": [], "points": []} once shown, then after every click
# {"clicked": [names], "points": [[x, y] in the window's own pixels]}. A click on a button also writes
# "Clicked <name>" in the status line. A green square sits in the bottom-right corner. One real WinForms
# button, Apply, is there too, so UI Automation's box for it can be compared with what OCR reads.
param([string]$Out, [switch]$Aware)

if ($Aware) {
    Add-Type -Namespace Overshoulder -Name Dpi -MemberDefinition '[DllImport("shcore.dll")] public static extern int SetProcessDpiAwareness(int v);'
    [void][Overshoulder.Dpi]::SetProcessDpiAwareness(2)
}
Add-Type -AssemblyName System.Windows.Forms, System.Drawing

$form = New-Object System.Windows.Forms.Form
$form.Text = 'Overshoulder drawn test'
$form.StartPosition = 'Manual'
$form.Location = New-Object System.Drawing.Point(560, 300)
$form.ClientSize = New-Object System.Drawing.Size(410, 200)
$form.BackColor = [System.Drawing.Color]::White
$buttons = [ordered]@{
    Save   = New-Object System.Drawing.Rectangle(20, 30, 110, 40)
    Open   = New-Object System.Drawing.Rectangle(150, 30, 110, 40)
    Delete = New-Object System.Drawing.Rectangle(280, 30, 110, 40)
}
$font = New-Object System.Drawing.Font('Segoe UI', 12)
$ink = [System.Drawing.Color]::FromArgb(17, 24, 39)
$face = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(229, 231, 235))
$edge = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(55, 65, 81), 2)
$green = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(22, 163, 74))
$center = [System.Windows.Forms.TextFormatFlags]::HorizontalCenter -bor [System.Windows.Forms.TextFormatFlags]::VerticalCenter
$script:status = 'Nothing clicked yet'
$script:clicked = @()
$script:points = @()

function Save {
    $json = @{ ready = $true; clicked = @($script:clicked); points = @($script:points) } | ConvertTo-Json -Compress -Depth 3
    [System.IO.File]::WriteAllText($Out, $json)
}

$form.Add_Paint({
    param($s, $e)
    foreach ($name in $buttons.Keys) {
        $r = $buttons[$name]
        $e.Graphics.FillRectangle($face, $r)
        $e.Graphics.DrawRectangle($edge, $r)
        [System.Windows.Forms.TextRenderer]::DrawText($e.Graphics, $name, $font, $r, $ink, $center)
    }
    [System.Windows.Forms.TextRenderer]::DrawText($e.Graphics, $script:status, $font, (New-Object System.Drawing.Point(20, 110)), $ink)
    $c = $form.ClientSize
    $e.Graphics.FillRectangle($green, $c.Width - 34, $c.Height - 34, 24, 24)
})
$form.Add_MouseClick({
    param($s, $e)
    $script:points += , @($e.X, $e.Y)
    foreach ($name in $buttons.Keys) {
        if ($buttons[$name].Contains($e.Location)) {
            $script:clicked += $name
            $script:status = "Clicked $name"
        }
    }
    $form.Invalidate()
    Save
})
$apply = New-Object System.Windows.Forms.Button
$apply.Text = 'Apply'
$apply.Font = $font
$apply.Location = New-Object System.Drawing.Point(150, 140)
$apply.Size = New-Object System.Drawing.Size(110, 40)
$apply.Add_Click({
    $script:clicked += 'Apply'
    $script:status = 'Clicked Apply'
    $form.Invalidate()
    Save
})
$form.Controls.Add($apply)
$form.Add_Shown({ Save })
[System.Windows.Forms.Application]::Run($form)
