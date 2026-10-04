param([Parameter(Mandatory=$true)][int]$DebugPort)
$ErrorActionPreference = 'Stop'
# UI Automation invokes a named button without focusing the window or sending keys.
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
try {
    $owners = @(Get-NetTCPConnection -LocalPort $DebugPort -State Listen | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($owners.Count -ne 1) { throw 'Cannot identify one browser process for this debugging port' }
    $browserPid = [int]$owners[0]
    $pidCondition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty, $browserPid)
    # Chrome/GoLogin has used several titles for the browser-owned profile
    # chooser. Match only those known credential/profile dialogs.
    $headingConditions = @('Sign in as', 'Choose a profile', "Who's using Chrome?", 'Choose an account') | ForEach-Object {
        New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, $_)
    }
    $headingCondition = New-Object System.Windows.Automation.OrCondition(,$headingConditions)
    $closeConditions = @('Close', 'Cancel') | ForEach-Object {
        New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, $_)
    }
    $buttonCondition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)
    $closeNameCondition = New-Object System.Windows.Automation.OrCondition(,$closeConditions)
    $closeCondition = New-Object System.Windows.Automation.AndCondition($closeNameCondition, $buttonCondition)
    $notificationTextCondition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, 'Show notifications')
    $blockNameCondition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, 'Block')
    $blockCondition = New-Object System.Windows.Automation.AndCondition($blockNameCondition, $buttonCondition)
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    while ($true) {
        $candidates = @()
        $windows = [System.Windows.Automation.AutomationElement]::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, $pidCondition)
        foreach ($window in $windows) {
            $headings = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $headingCondition)
            foreach ($heading in $headings) {
                # The chooser's Close button is nested several levels below
                # the heading's parent in Orbita/Chrome. Start at the heading
                # itself and allow a few extra ancestor levels.
                $node = $heading
                # Do not climb to the whole browser window and invoke an unrelated Close.
                for ($depth = 0; $depth -lt 8 -and $null -ne $node; $depth++) {
                    if ($node.Equals($window)) { break }
                    $buttons = $node.FindAll([System.Windows.Automation.TreeScope]::Descendants, $closeCondition)
                    if ($buttons.Count -eq 1 -and -not $buttons[0].Current.IsOffscreen -and $buttons[0].Current.IsEnabled) {
                        $pattern = $null
                        if ($buttons[0].TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
                            $candidates += $buttons[0]
                            break
                        }
                    }
                    $node = $walker.GetParent($node)
                }
            }
            $notification = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $notificationTextCondition)
            foreach ($prompt in $notification) {
                $node = $prompt
                for ($depth = 0; $depth -lt 8 -and $null -ne $node; $depth++) {
                    if ($node.Equals($window)) { break }
                    $buttons = $node.FindAll([System.Windows.Automation.TreeScope]::Descendants, $blockCondition)
                    if ($buttons.Count -eq 1 -and -not $buttons[0].Current.IsOffscreen -and $buttons[0].Current.IsEnabled) {
                        $pattern = $null
                        if ($buttons[0].TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
                            $pattern.Invoke()
                            [Console]::Out.WriteLine('{"dismissed":"Windows.NotificationPermission"}')
                            break
                        }
                    }
                    $node = $walker.GetParent($node)
                }
            }
        }
        if ($candidates.Count -eq 1) {
            $invoke = $candidates[0].GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
            $invoke.Invoke()
            $closed = $false
            for ($attempt = 0; $attempt -lt 10; $attempt++) {
                Start-Sleep -Milliseconds 100
                try {
                    $remaining = @()
                    foreach ($window in $windows) {
                        $remaining += @($window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $headingCondition))
                    }
                    if ($remaining.Count -eq 0) { $closed = $true; break }
                } catch [System.Windows.Automation.ElementNotAvailableException] { $closed = $true; break }
            }
            if (-not $closed) { throw 'Close was invoked but the account chooser remained visible' }
            [Console]::Out.WriteLine('{"dismissed":"Windows.ProfileChooser"}')
        }
        Start-Sleep -Milliseconds 1000
    }
} catch {
    [Console]::Out.WriteLine('{"warning":"Windows account chooser dismissal unavailable; inspect the popup before continuing"}')
    exit 1
}
