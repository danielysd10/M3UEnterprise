[CmdletBinding(SupportsShouldProcess)]
param(
  [Parameter(Mandatory)] [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })] [string] $PythonPath,
  [Parameter(Mandatory)] [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })] [string] $FfprobePath,
  [string] $RulePrefix = 'M3U Enterprise - Block private egress'
)

$ErrorActionPreference = 'Stop'

# Deliberately requires explicit executable paths. Do not bind these rules to a
# globally shared Python installation without first isolating the application.
$blockedSets = @(
  @{ Family = 'IPv4'; Addresses = @('127.0.0.0/8','10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','169.254.0.0/16') },
  # Windows Firewall rejects ::1 as a RemoteAddress.  Loopback remains
  # fail-closed in network_target_policy before a connection is attempted;
  # these rules cover the IPv6 ranges Windows can enforce here.
  @{ Family = 'IPv6'; Addresses = @('fc00::/7','fe80::/10') }
)
foreach ($program in @((Resolve-Path -LiteralPath $PythonPath), (Resolve-Path -LiteralPath $FfprobePath))) {
  foreach ($blockedSet in $blockedSets) {
    # Windows Firewall does not accept IPv4 and IPv6 CIDRs in one rule.  Keep
    # the families separate so the guard is valid on every supported host.
    $name = "$RulePrefix - $([IO.Path]::GetFileName($program)) - $($blockedSet.Family)"
    if ($PSCmdlet.ShouldProcess($program, "Create outbound private-network block rule '$name'")) {
      Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
      New-NetFirewallRule -DisplayName $name -Direction Outbound -Action Block -Program $program -RemoteAddress $blockedSet.Addresses -Profile Any -ErrorAction Stop | Out-Null
    }
  }
}
