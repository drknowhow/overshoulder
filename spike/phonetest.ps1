# A WinForms window with a Send and a Delete button, for the live phone-approval check.
# Usage: phonetest.ps1 OUT.json (run by phase3_phone.py). Writes {"ready": true} once shown, then {"clicked": [...]} after each click.
param([string]$Out)

Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.Text = 'Overshoulder phone test'
$form.StartPosition = 'Manual'
$form.Location = New-Object System.Drawing.Point(560, 300)
$form.ClientSize = New-Object System.Drawing.Size(360, 160)
$script:clicked = @()

function Save { @{ ready = $true; clicked = $script:clicked } | ConvertTo-Json -Compress | Set-Content -Encoding utf8 $Out }

$label = New-Object System.Windows.Forms.Label
$label.Text = 'Nothing clicked yet'
$label.Location = New-Object System.Drawing.Point(20, 100)
$label.AutoSize = $true
$form.Controls.Add($label)

foreach ($spec in @(@('Send', 20), @('Delete', 180))) {
    $b = New-Object System.Windows.Forms.Button
    $b.Text = $spec[0]
    $b.Location = New-Object System.Drawing.Point($spec[1], 30)
    $b.Size = New-Object System.Drawing.Size(140, 40)
    $b.Add_Click({
        param($s, $e)
        $script:clicked += $s.Text
        $label.Text = "Clicked: $($script:clicked -join ', ')"
        Save
    })
    $form.Controls.Add($b)
}

$form.Add_Shown({ Save })
[void]$form.ShowDialog()
