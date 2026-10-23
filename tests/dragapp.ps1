# A WinForms window with one box that can be dragged, for the drag desktop tests. WinForms takes the
# pointer from each message, so a posted drag can move the box.
# Usage: dragapp.ps1 OUT.json. Writes {"ready", "start", "drop"} once shown (screen pixels), then
# {"released", "box": [left, top, right, bottom], "moves"} after every release of the box.
param([string]$Out)

Add-Type -Namespace Overshoulder -Name Dpi -MemberDefinition '[DllImport("shcore.dll")] public static extern int SetProcessDpiAwareness(int v);'
[void][Overshoulder.Dpi]::SetProcessDpiAwareness(2)  # physical pixels, like Overshoulder
Add-Type -AssemblyName System.Windows.Forms, System.Drawing

$form = New-Object System.Windows.Forms.Form
$form.Text = 'Overshoulder drag test'
$form.StartPosition = 'Manual'
$form.Location = New-Object System.Drawing.Point(520, 260)
$form.ClientSize = New-Object System.Drawing.Size(400, 280)
$form.BackColor = [System.Drawing.Color]::White
$script:box = New-Object System.Drawing.Rectangle(30, 30, 60, 60)
$script:at = $null
$script:moves = 0
$brush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(37, 99, 235))

$form.Add_Paint({ param($s, $e) $e.Graphics.FillRectangle($brush, $script:box) })
$form.Add_MouseDown({
    param($s, $e)
    if ($e.Button -eq 'Left' -and $script:box.Contains($e.Location)) { $script:at = $e.Location }
})
$form.Add_MouseMove({
    param($s, $e)
    if ($null -ne $script:at -and $e.Button -eq 'Left') {
        $script:box.Offset($e.X - $script:at.X, $e.Y - $script:at.Y)
        $script:at = $e.Location
        $script:moves++
        $form.Invalidate()
    }
})
$form.Add_MouseUp({
    param($s, $e)
    if ($null -ne $script:at) {
        $script:at = $null
        $b = $script:box
        $json = @{ released = $true; box = @($b.Left, $b.Top, $b.Right, $b.Bottom); moves = $script:moves } | ConvertTo-Json -Compress
        [System.IO.File]::WriteAllText($Out, $json)
    }
})
$form.Add_Shown({
    $p = $form.PointToScreen((New-Object System.Drawing.Point(0, 0)))
    $json = @{ ready = $true; start = @(($p.X + 60), ($p.Y + 60)); drop = @(($p.X + 300), ($p.Y + 200)) } | ConvertTo-Json -Compress
    [System.IO.File]::WriteAllText($Out, $json)
})
[System.Windows.Forms.Application]::Run($form)
