[CmdletBinding()]
param(
    [string] $SubscriptionId = '8cebb108-a4d5-402b-a0c4-f7556126277f',
    [string] $ResourceGroup = 'rg-azure-ai-agents',
    [string] $Location = 'westus',
    [ValidatePattern('^[a-z0-9-]{3,20}$')]
    [string] $NamePrefix = 'dev',
    [string] $ApplicationInsightsResourceId = '/subscriptions/8cebb108-a4d5-402b-a0c4-f7556126277f/resourceGroups/rg-a2a-dev/providers/Microsoft.Insights/components/dev-multiagent-ai',
    [string] $ApimManagedIdentityClientId = '4ad11483-5daa-4066-ac34-770f3d468f54',
    [string] $ApimName = '',
    [string] $ApimResourceGroup = '',
    [string] $CostApiApplicationClientId = 'fd4ee8ef-65be-42e5-8702-aa840ef8e22e',
    [string] $ConsoleApplicationClientId = '',
    [string] $CostApiApplicationName = 'Cost Enforcement API',
    [string] $ConsoleApplicationName = 'Cost Budget Console',
    [string] $ContainerRegistryName = '',
    [string] $CosmosAccountName = '',
    [string] $ContainerAppsEnvironmentName = '',
    [string] $VirtualNetworkName = '',
    [string] $CostApiName = '',
    [string] $ConsoleAppName = '',
    [string] $CostApiImageRepository = 'cost-enforcement-api',
    [string] $ConsoleImageRepository = 'budget-console',
    [string] $ImageTag = (Get-Date -Format 'yyyyMMddHHmmss'),
    [string[]] $ConsoleAllowedEmailDomains = @('microsoft.com'),
    [string] $ConsoleOwnerUsername = '',
    [switch] $SkipImageBuild
)

$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $PSScriptRoot
$templateFile = Join-Path $appRoot 'infra/main.bicep'
$costApiDockerfile = Join-Path $appRoot 'Dockerfile'
$consoleDockerfile = Join-Path $appRoot 'Dockerfile.console'

function Invoke-AzJson {
    param([Parameter(Mandatory)][string[]] $Arguments)
    $output = & az @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI failed: az $($Arguments -join ' ')"
    }
    if ([string]::IsNullOrWhiteSpace(($output -join ''))) {
        return $null
    }
    return ($output -join "`n") | ConvertFrom-Json
}

function Invoke-AzCommand {
    param([Parameter(Mandatory)][string[]] $Arguments)
    & az @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI failed: az $($Arguments -join ' ')"
    }
}

function Invoke-Graph {
    param(
        [Parameter(Mandatory)][ValidateSet('GET', 'POST', 'PATCH')][string] $Method,
        [Parameter(Mandatory)][string] $Path,
        [object] $Body
    )
    $arguments = @('rest', '--method', $Method, '--url', "$script:graphEndpoint$Path", '--output', 'json')
    $bodyPath = $null
    try {
        if ($null -ne $Body) {
            $bodyPath = [IO.Path]::GetTempFileName()
            $bodyJson = $Body | ConvertTo-Json -Depth 20 -Compress
            [IO.File]::WriteAllText($bodyPath, $bodyJson, [Text.UTF8Encoding]::new($false))
            $arguments += @('--headers', 'Content-Type=application/json', '--body', "@$bodyPath")
        }
        return Invoke-AzJson -Arguments $arguments
    }
    finally {
        if ($bodyPath) {
            Remove-Item -LiteralPath $bodyPath -Force -ErrorAction SilentlyContinue
        }
    }
}

function Get-OrCreateApplication {
    param(
        [string] $ClientId,
        [Parameter(Mandatory)][string] $DisplayName,
        [ValidateSet('AzureADMyOrg', 'AzureADMultipleOrgs')]
        [string] $SignInAudience = 'AzureADMyOrg'
    )
    if ($ClientId) {
        return Invoke-AzJson -Arguments @('ad', 'app', 'show', '--id', $ClientId, '--output', 'json')
    }
    $matches = @(Invoke-AzJson -Arguments @('ad', 'app', 'list', '--display-name', $DisplayName, '--output', 'json'))
    if ($matches.Count -gt 1) {
        throw "More than one app registration is named '$DisplayName'; supply its client ID explicitly."
    }
    if ($matches.Count -eq 1) {
        return $matches[0]
    }
    return Invoke-AzJson -Arguments @('ad', 'app', 'create', '--display-name', $DisplayName, '--sign-in-audience', $SignInAudience, '--output', 'json')
}

function Ensure-ServicePrincipal {
    param([Parameter(Mandatory)][string] $ClientId)
    $servicePrincipals = @(Invoke-AzJson -Arguments @('ad', 'sp', 'list', '--filter', "appId eq '$ClientId'", '--output', 'json'))
    if ($servicePrincipals.Count -eq 0) {
        return Invoke-AzJson -Arguments @('ad', 'sp', 'create', '--id', $ClientId, '--output', 'json')
    }
    return $servicePrincipals[0]
}

function Ensure-AppRoles {
    param(
        [Parameter(Mandatory)][object] $Application,
        [Parameter(Mandatory)][object[]] $RequiredRoles
    )
    $roles = @($Application.appRoles)
    $changed = $false
    foreach ($requiredRole in $RequiredRoles) {
        if (-not ($roles | Where-Object value -eq $requiredRole.value)) {
            $roles += [ordered]@{
                allowedMemberTypes = @($requiredRole.allowedMemberTypes)
                description = $requiredRole.description
                displayName = $requiredRole.displayName
                id = [guid]::NewGuid().Guid
                isEnabled = $true
                value = $requiredRole.value
            }
            $changed = $true
        }
    }
    if ($changed) {
        Invoke-Graph -Method PATCH -Path "/applications/$($Application.id)" -Body @{ appRoles = $roles } | Out-Null
    }
    return Invoke-AzJson -Arguments @('ad', 'app', 'show', '--id', $Application.appId, '--output', 'json')
}

function Ensure-AppRoleAssignment {
    param(
        [Parameter(Mandatory)][string] $PrincipalId,
        [Parameter(Mandatory)][string] $PrincipalCollection,
        [Parameter(Mandatory)][string] $ResourceServicePrincipalId,
        [Parameter(Mandatory)][string] $AppRoleId
    )
    $assignments = Invoke-Graph -Method GET -Path "/$PrincipalCollection/$PrincipalId/appRoleAssignments" -Body $null
    $exists = @($assignments.value) | Where-Object {
        $_.resourceId -eq $ResourceServicePrincipalId -and $_.appRoleId -eq $AppRoleId
    }
    if (-not $exists) {
        Invoke-Graph -Method POST -Path "/$PrincipalCollection/$PrincipalId/appRoleAssignments" -Body @{
            principalId = $PrincipalId
            resourceId = $ResourceServicePrincipalId
            appRoleId = $AppRoleId
        } | Out-Null
    }
}

function Get-PrincipalCollection {
    param([Parameter(Mandatory)][string] $ObjectId)
    $directoryObject = Invoke-Graph -Method GET -Path "/directoryObjects/$ObjectId" -Body $null
    switch ($directoryObject.'@odata.type') {
        '#microsoft.graph.user' { return 'users' }
        '#microsoft.graph.group' { return 'groups' }
        '#microsoft.graph.servicePrincipal' { return 'servicePrincipals' }
        default { throw "Directory object '$ObjectId' cannot receive an application role." }
    }
}

function ConvertTo-PlainText {
    param([Parameter(Mandatory)][Security.SecureString] $SecureValue)
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

function New-OwnerPasswordHash {
    param([Parameter(Mandatory)][string] $Password)
    $iterations = 310000
    $salt = [byte[]]::new(16)
    [Security.Cryptography.RandomNumberGenerator]::Fill($salt)
    $derive = [Security.Cryptography.Rfc2898DeriveBytes]::new(
        $Password,
        $salt,
        $iterations,
        [Security.Cryptography.HashAlgorithmName]::SHA256
    )
    try {
        $digest = $derive.GetBytes(32)
        $encode = {
            param([byte[]] $Value)
            [Convert]::ToBase64String($Value).TrimEnd('=').Replace('+', '-').Replace('/', '_')
        }
        return 'pbkdf2_sha256${0}${1}${2}' -f $iterations, (& $encode $salt), (& $encode $digest)
    }
    finally {
        $derive.Dispose()
    }
}

function New-SessionSecret {
    $bytes = [byte[]]::new(32)
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes)
}

function New-ArmParameterFile {
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][bool] $DeployApps,
        [Parameter(Mandatory)][string] $TenantId,
        [Parameter(Mandatory)][string] $CostClientId,
        [Parameter(Mandatory)][string] $ConsoleClientId,
        [Parameter(Mandatory)][string] $OwnerUsername,
        [Parameter(Mandatory)][string] $OwnerPasswordHash,
        [Parameter(Mandatory)][string] $OwnerSessionSecret
    )
    $values = [ordered]@{
        namePrefix = $NamePrefix
        location = $Location
        tenantId = $TenantId
        audience = $CostClientId
        apimManagedIdentityClientId = $ApimManagedIdentityClientId
        consoleClientId = $ConsoleClientId
        consoleAllowedEmailDomains = $ConsoleAllowedEmailDomains
        consoleOwnerUsername = $OwnerUsername
        consoleOwnerPasswordHash = $OwnerPasswordHash
        consoleOwnerSessionSecret = $OwnerSessionSecret
        costApiScope = "api://$CostClientId/.default"
        applicationInsightsResourceId = $ApplicationInsightsResourceId
        imageRepository = $CostApiImageRepository
        imageTag = $ImageTag
        consoleImageRepository = $ConsoleImageRepository
        consoleImageTag = $ImageTag
        deployContainerApp = $DeployApps
        deployConsole = $DeployApps
    }
    $optionalValues = [ordered]@{
        containerRegistryName = $ContainerRegistryName
        cosmosAccountName = $CosmosAccountName
        containerAppsEnvironmentName = $ContainerAppsEnvironmentName
        virtualNetworkName = $VirtualNetworkName
        costApiName = $CostApiName
        consoleAppName = $ConsoleAppName
    }
    foreach ($entry in $optionalValues.GetEnumerator()) {
        if ($entry.Value) {
            $values[$entry.Key] = $entry.Value
        }
    }
    $parameters = [ordered]@{}
    foreach ($entry in $values.GetEnumerator()) {
        $parameters[$entry.Key] = @{ value = $entry.Value }
    }
    [ordered]@{
        '$schema' = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'
        contentVersion = '1.0.0.0'
        parameters = $parameters
    } | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $Path -Encoding utf8NoBOM
}

function Invoke-InfrastructureDeployment {
    param(
        [Parameter(Mandatory)][bool] $DeployApps,
        [Parameter(Mandatory)][string] $TenantId,
        [Parameter(Mandatory)][string] $CostClientId,
        [Parameter(Mandatory)][string] $ConsoleClientId,
        [Parameter(Mandatory)][string] $OwnerUsername,
        [Parameter(Mandatory)][string] $OwnerPasswordHash,
        [Parameter(Mandatory)][string] $OwnerSessionSecret
    )
    $parameterFile = [IO.Path]::GetTempFileName()
    try {
        New-ArmParameterFile -Path $parameterFile -DeployApps $DeployApps -TenantId $TenantId -CostClientId $CostClientId -ConsoleClientId $ConsoleClientId -OwnerUsername $OwnerUsername -OwnerPasswordHash $OwnerPasswordHash -OwnerSessionSecret $OwnerSessionSecret
        $phase = if ($DeployApps) { 'apps' } else { 'bootstrap' }
        return Invoke-AzJson -Arguments @(
            'deployment', 'group', 'create',
            '--name', "$NamePrefix-finops-$phase",
            '--resource-group', $ResourceGroup,
            '--template-file', $templateFile,
            '--parameters', "@$parameterFile",
            '--query', 'properties.outputs',
            '--output', 'json'
        )
    }
    finally {
        Remove-Item -LiteralPath $parameterFile -Force -ErrorAction SilentlyContinue
    }
}

Invoke-AzCommand -Arguments @('account', 'set', '--subscription', $SubscriptionId)
$account = Invoke-AzJson -Arguments @('account', 'show', '--output', 'json')
$cloud = Invoke-AzJson -Arguments @('cloud', 'show', '--output', 'json')
$script:graphEndpoint = "$($cloud.endpoints.microsoftGraphResourceId.TrimEnd('/'))/v1.0"
Invoke-AzCommand -Arguments @('group', 'create', '--name', $ResourceGroup, '--location', $Location, '--output', 'none')

if (-not $ApimManagedIdentityClientId) {
    if (-not $ApimName -or -not $ApimResourceGroup) {
        throw 'Supply ApimManagedIdentityClientId or both ApimName and ApimResourceGroup.'
    }
    $apimPrincipalId = (& az apim show --name $ApimName --resource-group $ApimResourceGroup --query identity.principalId --output tsv).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $apimPrincipalId) {
        throw "APIM '$ApimName' does not have a system-assigned identity."
    }
    $ApimManagedIdentityClientId = (& az ad sp show --id $apimPrincipalId --query appId --output tsv).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $ApimManagedIdentityClientId) {
        throw 'Could not resolve the APIM managed identity client ID.'
    }
}

$costApplication = Get-OrCreateApplication -ClientId $CostApiApplicationClientId -DisplayName $CostApiApplicationName
$apiSettings = if ($costApplication.api) { $costApplication.api } else { [pscustomobject]@{} }
$apiSettings | Add-Member -NotePropertyName requestedAccessTokenVersion -NotePropertyValue 2 -Force
Invoke-Graph -Method PATCH -Path "/applications/$($costApplication.id)" -Body @{
    identifierUris = @("api://$($costApplication.appId)")
    api = $apiSettings
} | Out-Null
$costApplication = Ensure-AppRoles -Application $costApplication -RequiredRoles @(
    [pscustomobject]@{
        value = 'CostEnforcement.Admin'
        displayName = 'Cost Enforcement Administrator'
        description = 'Allows an application to manage budgets and prices.'
        allowedMemberTypes = @('Application')
    }
)
$costServicePrincipal = Ensure-ServicePrincipal -ClientId $costApplication.appId

$consoleApplication = Get-OrCreateApplication -ClientId $ConsoleApplicationClientId -DisplayName $ConsoleApplicationName
$consoleServicePrincipal = Ensure-ServicePrincipal -ClientId $consoleApplication.appId
Invoke-Graph -Method PATCH -Path "/servicePrincipals/$($consoleServicePrincipal.id)" -Body @{ appRoleAssignmentRequired = $false } | Out-Null

if (-not $ConsoleOwnerUsername) {
    $resolvedOwnerUsername = & az ad signed-in-user show --query userPrincipalName --output tsv 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($resolvedOwnerUsername -join ''))) {
        throw 'Supply ConsoleOwnerUsername when the signed-in Azure identity is not a directory user.'
    }
    $ConsoleOwnerUsername = ($resolvedOwnerUsername -join "`n").Trim()
}
$ownerPassword = $env:CONSOLE_OWNER_PASSWORD
if (-not $ownerPassword) {
    $ownerPassword = ConvertTo-PlainText (Read-Host "Password for local owner '$ConsoleOwnerUsername'" -AsSecureString)
}
if ($ownerPassword.Length -lt 12) {
    throw 'The console owner password must contain at least 12 characters.'
}
$ownerPasswordHash = New-OwnerPasswordHash -Password $ownerPassword
$ownerPassword = $null
$ownerSessionSecret = New-SessionSecret

$bootstrapOutputs = Invoke-InfrastructureDeployment -DeployApps $false -TenantId $account.tenantId -CostClientId $costApplication.appId -ConsoleClientId $consoleApplication.appId -OwnerUsername $ConsoleOwnerUsername -OwnerPasswordHash $ownerPasswordHash -OwnerSessionSecret $ownerSessionSecret
$registryName = $bootstrapOutputs.registryName.value
if (-not $SkipImageBuild) {
    Invoke-AzCommand -Arguments @('acr', 'build', '--registry', $registryName, '--image', "${CostApiImageRepository}:$ImageTag", '--file', $costApiDockerfile, $appRoot)
    Invoke-AzCommand -Arguments @('acr', 'build', '--registry', $registryName, '--image', "${ConsoleImageRepository}:$ImageTag", '--file', $consoleDockerfile, $appRoot)
}

$deploymentOutputs = Invoke-InfrastructureDeployment -DeployApps $true -TenantId $account.tenantId -CostClientId $costApplication.appId -ConsoleClientId $consoleApplication.appId -OwnerUsername $ConsoleOwnerUsername -OwnerPasswordHash $ownerPasswordHash -OwnerSessionSecret $ownerSessionSecret
$resolvedConsoleAppName = if ($ConsoleAppName) { $ConsoleAppName } else { "$NamePrefix-budget-console" }
Invoke-AzCommand -Arguments @(
    'containerapp', 'auth', 'update',
    '--name', $resolvedConsoleAppName,
    '--resource-group', $ResourceGroup,
    '--enabled', 'false',
    '--unauthenticated-client-action', 'AllowAnonymous',
    '--token-store', 'false',
    '--output', 'none'
)
$consoleAuth = Invoke-AzJson -Arguments @(
    'containerapp', 'auth', 'show',
    '--name', $resolvedConsoleAppName,
    '--resource-group', $ResourceGroup,
    '--output', 'json'
)
if ($consoleAuth.platform.enabled -or $consoleAuth.login.tokenStore.enabled) {
    throw "Container Apps authentication or its token store is still enabled for '$resolvedConsoleAppName'. It conflicts with the console's MSAL authorization-code flow."
}
$consoleUrl = $deploymentOutputs.consoleUrl.value.TrimEnd('/')
$redirectUri = $consoleUrl
$localRedirectUri = 'http://localhost:8010'
$latestConsoleApplication = Invoke-AzJson -Arguments @('ad', 'app', 'show', '--id', $consoleApplication.appId, '--output', 'json')
$redirectUris = @((
    @($latestConsoleApplication.spa.redirectUris) +
    $redirectUri +
    $localRedirectUri
) | Sort-Object -Unique)
Invoke-Graph -Method PATCH -Path "/applications/$($consoleApplication.id)" -Body @{
    signInAudience = 'AzureADMyOrg'
    spa = @{
        redirectUris = $redirectUris
    }
} | Out-Null

$costAdminRoleId = ($costApplication.appRoles | Where-Object value -eq 'CostEnforcement.Admin').id
Ensure-AppRoleAssignment -PrincipalId $deploymentOutputs.consolePrincipalId.value -PrincipalCollection 'servicePrincipals' -ResourceServicePrincipalId $costServicePrincipal.id -AppRoleId $costAdminRoleId

Write-Host "Console URL: $consoleUrl"
Write-Host "Cost API URL: $($deploymentOutputs.apiUrl.value)"
Write-Host "Cost API application ID: $($costApplication.appId)"
Write-Host "Console application ID: $($consoleApplication.appId)"