#Requires -Version 7.2
<#
.SYNOPSIS
    Deploy lexwatch to Azure: Container Apps (app + worker), PostgreSQL Flexible Server, ACR.

.DESCRIPTION
    Written for Azure Cloud Shell (PowerShell) with Contributor on one resource group and no
    right to create role assignments or Entra app registrations. Therefore:
      - the image is pulled from ACR with the registry admin user (no AcrPull role needed);
      - Foundry is called with an API key (no Cognitive Services User role needed);
      - secrets live as Container Apps secrets (no Key Vault RBAC needed);
      - login (OIDC) is wired in once an Entra admin has created the app registration:
        re-run with -OidcClientId.

    The script is re-runnable: it creates what is missing and updates the rest. Generated
    names and secrets (DB password, SECRET_KEY) are kept in $StateFile (mode 600) so a re-run
    reuses them.

.EXAMPLE
    # First run: infrastructure, image, app and worker (no scoring, no login yet)
    ./deploy/azure/deploy.ps1

.EXAMPLE
    # Add Foundry scoring and Entra ID login (prompts for the client secret)
    ./deploy/azure/deploy.ps1 -SkipBuild -FoundryAccount foundry-dev-swc-resource `
        -FoundryModel Cohere-rerank-v4.0-fast -OidcClientId <application-client-id>
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = 'RG-EUW-ADVISORY-AI-SANDBOX',
    [string]$Location = 'westeurope',
    # Postgres region if Burstable servers are restricted in $Location for this subscription
    [string]$PostgresLocation = '',
    [string]$Prefix = 'lexwatch',
    [string]$RepoPath = '',
    [string]$ImageTag = '',
    [switch]$SkipBuild,
    # Foundry (Azure AI Services) account holding the Cohere rerank deployment
    [string]$FoundryAccount = '',
    [string]$FoundryResourceGroup = '',
    # Deployment name of the rerank model on that account
    [string]$FoundryModel = '',
    # Entra ID app registration (created by an Entra admin, see deploy/azure/README.md)
    [string]$OidcClientId = '',
    [string]$OidcAdminValue = 'lexwatch-admin',
    [ValidateSet('fr', 'en')][string]$DefaultLanguage = 'fr',
    [string]$DailyJobTime = '06:30',
    [string]$StateFile = "$HOME/.lexwatch/azure-deploy.json"
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true   # a failing az call stops the script

function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Note([string]$Message) { Write-Host "    $Message" -ForegroundColor DarkGray }
function Write-Warn([string]$Message) { Write-Host "    ! $Message" -ForegroundColor Yellow }

# True when the az command succeeds (used for "does it exist" checks; errors are silenced).
function Test-Az {
    $PSNativeCommandUseErrorActionPreference = $false
    & az @args --only-show-errors -o none 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function New-RandomString([int]$Length) {
    $chars = [char[]]'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
    $bytes = [System.Security.Cryptography.RandomNumberGenerator]::GetBytes($Length)
    return -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
}

# --- state --------------------------------------------------------------------------------
$stateDir = Split-Path -Parent $StateFile
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
$state = @{}
if (Test-Path $StateFile) { $state = Get-Content $StateFile -Raw | ConvertFrom-Json -AsHashtable }
function Save-State {
    $state | ConvertTo-Json | Set-Content -Path $StateFile -Encoding utf8
    if ($IsLinux -or $IsMacOS) { chmod 600 $StateFile }
}
if (-not $state.suffix) { $state.suffix = (New-RandomString 6).ToLower() }
# URL-safe (it goes into DATABASE_URL) and meets the Postgres complexity rule (3 classes)
if (-not $state.pgPassword) { $state.pgPassword = 'Lx7' + (New-RandomString 32) }
if (-not $state.secretKey) { $state.secretKey = New-RandomString 64 }
Save-State

$acrName = ("$Prefix" + 'acr' + $state.suffix).ToLower() -replace '[^a-z0-9]', ''
$pgName = "$Prefix-pg-$($state.suffix)".ToLower()
$envName = "$Prefix-env"
$appName = "$Prefix-app"
$workerName = "$Prefix-worker"
$pgLocation = if ($PostgresLocation) { $PostgresLocation } else { $Location }
$tags = @('app=lexwatch', 'environment=sandbox', 'dataclassification=internal', 'solutionexposure=internal')

# --- 0. checks ----------------------------------------------------------------------------
Write-Step 'Checking subscription, resource group and source'
$account = az account show --query '{sub:name, tenant:tenantId}' -o json | ConvertFrom-Json
Write-Note "Subscription: $($account.sub)"
if (-not (Test-Az group show -n $ResourceGroup)) { throw "Resource group $ResourceGroup not found or not visible" }
if (-not $RepoPath) { $RepoPath = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path }
if (-not (Test-Path (Join-Path $RepoPath 'Dockerfile'))) { throw "No Dockerfile in $RepoPath" }
Write-Note "Resource group: $ResourceGroup ($Location); names: $acrName, $pgName, $envName"

# --- 1. container registry and image ------------------------------------------------------
Write-Step "Container registry $acrName"
if (-not (Test-Az acr show -n $acrName -g $ResourceGroup)) {
    az acr create -n $acrName -g $ResourceGroup -l $Location --sku Basic --admin-enabled true `
        --tags @tags --only-show-errors -o none
} else {
    az acr update -n $acrName -g $ResourceGroup --admin-enabled true --only-show-errors -o none
}
$acrServer = az acr show -n $acrName -g $ResourceGroup --query loginServer -o tsv
$acrUser = az acr credential show -n $acrName -g $ResourceGroup --query username -o tsv
$acrPassword = az acr credential show -n $acrName -g $ResourceGroup --query 'passwords[0].value' -o tsv

if ($SkipBuild) {
    if (-not $ImageTag) { $ImageTag = $state.imageTag }
    if (-not $ImageTag) { throw '-SkipBuild needs a previous build (or -ImageTag)' }
    Write-Note "Skipping build, using lexwatch:$ImageTag"
} else {
    if (-not $ImageTag) {
        $sha = git -C $RepoPath rev-parse --short HEAD
        $ImageTag = "$sha-$(Get-Date -Format 'yyyyMMddHHmm')"
    }
    Write-Step "Building lexwatch:$ImageTag in ACR (a few minutes)"
    az acr run -r $acrName -f deploy/azure/acr-build.yaml --set "tag=$ImageTag" $RepoPath --only-show-errors
    $state.imageTag = $ImageTag
    Save-State
}
$image = "$acrServer/lexwatch:$ImageTag"

# --- 2. PostgreSQL ------------------------------------------------------------------------
Write-Step "PostgreSQL Flexible Server $pgName ($pgLocation)"
if (-not (Test-Az postgres flexible-server show -n $pgName -g $ResourceGroup)) {
    Write-Note 'Creating (5-10 minutes). Public endpoint, TLS required, firewall: Azure services only.'
    az postgres flexible-server create -n $pgName -g $ResourceGroup -l $pgLocation `
        --tier Burstable --sku-name Standard_B1ms --storage-size 32 --version 16 `
        --admin-user lexwatch --admin-password $state.pgPassword --database-name lexwatch `
        --public-access 0.0.0.0 --tags @tags --yes --only-show-errors -o none
}
# Migration 0003 creates the unaccent extension; Azure requires it to be allow-listed first.
az postgres flexible-server parameter set -g $ResourceGroup -s $pgName -n azure.extensions `
    -v UNACCENT --only-show-errors -o none
$pgHost = az postgres flexible-server show -n $pgName -g $ResourceGroup --query fullyQualifiedDomainName -o tsv
$databaseUrl = "postgresql+psycopg://lexwatch:$($state.pgPassword)@${pgHost}:5432/lexwatch?sslmode=require"

# --- 3. Container Apps environment --------------------------------------------------------
Write-Step "Container Apps environment $envName"
if (-not (Test-Az containerapp env show -n $envName -g $ResourceGroup)) {
    az containerapp env create -n $envName -g $ResourceGroup -l $Location `
        --logs-destination log-analytics --tags @tags --only-show-errors -o none
}
$defaultDomain = az containerapp env show -n $envName -g $ResourceGroup --query properties.defaultDomain -o tsv
$baseUrl = "https://$appName.$defaultDomain"
Write-Note "App URL: $baseUrl"

# --- 4. Foundry (optional until the rerank deployment exists) ------------------------------
if ($FoundryAccount) {
    Write-Step "Foundry account $FoundryAccount"
    $fRg = if ($FoundryResourceGroup) { $FoundryResourceGroup } else { $ResourceGroup }
    $sub = az cognitiveservices account show -n $FoundryAccount -g $fRg --query properties.customSubDomainName -o tsv
    if (-not $sub) { throw "$FoundryAccount has no custom subdomain; it cannot serve the Foundry rerank route" }
    $state.foundryEndpoint = "https://$sub.services.ai.azure.com/providers/cohere/v2/rerank"
    $state.foundryApiKey = az cognitiveservices account keys list -n $FoundryAccount -g $fRg --query key1 -o tsv
    if ($FoundryModel) { $state.foundryModel = $FoundryModel }
    if (-not $state.foundryModel) { throw '-FoundryModel (the rerank deployment name) is required with -FoundryAccount' }
    if (-not (Test-Az cognitiveservices account deployment show -n $FoundryAccount -g $fRg --deployment-name $state.foundryModel)) {
        Write-Warn "No deployment named '$($state.foundryModel)' on ${FoundryAccount}: scoring will fail until it exists."
    }
    Save-State
}

# --- 5. OIDC (optional until the Entra app registration exists) ----------------------------
if ($OidcClientId) {
    $state.oidcClientId = $OidcClientId
    if ($env:LEXWATCH_OIDC_CLIENT_SECRET) {
        $state.oidcClientSecret = $env:LEXWATCH_OIDC_CLIENT_SECRET
    } elseif (-not $state.oidcClientSecret) {
        $state.oidcClientSecret = Read-Host -MaskInput 'Client secret of the Entra app registration'
    }
    Save-State
}

# --- 6. app and worker --------------------------------------------------------------------
$secrets = [ordered]@{ 'secret-key' = $state.secretKey; 'database-url' = $databaseUrl }
$envVars = @(
    'ENV=production', 'LOG_LEVEL=INFO', "BASE_URL=$baseUrl", "DEFAULT_LANGUAGE=$DefaultLanguage",
    'SECRET_KEY=secretref:secret-key', 'DATABASE_URL=secretref:database-url',
    'SESSION_HTTPS_ONLY=true', 'DEV_AUTH=false',
    'HTTP_USER_AGENT=lexwatch/0.1 (+internal bill tracker)',
    'SCORER=foundry', 'DIGEST_ENABLED=false',
    "DAILY_JOB_TIME=$DailyJobTime", 'APP_TIMEZONE=Europe/Luxembourg',
    # Forces a new revision on every run, so changed secrets are picked up.
    "DEPLOYED_AT=$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ' -AsUTC)"
)
if ($state.foundryEndpoint) {
    $secrets['foundry-api-key'] = $state.foundryApiKey
    $envVars += "FOUNDRY_ENDPOINT=$($state.foundryEndpoint)", "FOUNDRY_MODEL=$($state.foundryModel)",
        'FOUNDRY_API_KEY=secretref:foundry-api-key', 'FOUNDRY_API_KEY_HEADER=api-key'
} else {
    Write-Warn 'Foundry not configured yet: ingestion runs, scoring fails until you re-run with -FoundryAccount.'
}
if ($state.oidcClientId) {
    $secrets['oidc-client-secret'] = $state.oidcClientSecret
    $envVars += "OIDC_ISSUER=https://login.microsoftonline.com/$($account.tenant)/v2.0",
        "OIDC_CLIENT_ID=$($state.oidcClientId)", 'OIDC_CLIENT_SECRET=secretref:oidc-client-secret',
        'OIDC_ROLE_CLAIM=roles', "OIDC_ADMIN_VALUE=$OidcAdminValue"
} else {
    Write-Warn 'OIDC not configured yet: nobody can log in until you re-run with -OidcClientId.'
}
$secretArgs = @($secrets.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" })

function Deploy-ContainerApp([string]$Name, [string[]]$AppArgs, [switch]$Ingress) {
    Write-Step "Container app $Name"
    if (Test-Az containerapp show -n $Name -g $ResourceGroup) {
        az containerapp secret set -n $Name -g $ResourceGroup --secrets @secretArgs --only-show-errors -o none
        $update = @('containerapp', 'update', '-n', $Name, '-g', $ResourceGroup, '--image', $image,
            '--replace-env-vars') + $envVars
        if ($AppArgs) { $update += @('--args') + $AppArgs }
        az @update --only-show-errors -o none
    } else {
        $create = @('containerapp', 'create', '-n', $Name, '-g', $ResourceGroup,
            '--environment', $envName, '--image', $image,
            '--registry-server', $acrServer, '--registry-username', $acrUser, '--registry-password', $acrPassword,
            '--cpu', '0.5', '--memory', '1Gi', '--min-replicas', '1', '--max-replicas', '1',
            '--tags') + $tags + @('--secrets') + $secretArgs + @('--env-vars') + $envVars
        if ($AppArgs) { $create += @('--args') + $AppArgs }
        if ($Ingress) { $create += @('--ingress', 'external', '--target-port', '8000', '--transport', 'auto') }
        az @create --only-show-errors -o none
    }
}

# The image's default command is `serve` (0.0.0.0:8000). Both containers run migrations at
# start; a Postgres advisory lock makes that safe.
Deploy-ContainerApp -Name $appName -Ingress
Deploy-ContainerApp -Name $workerName -AppArgs @('worker')

# --- 7. smoke test ------------------------------------------------------------------------
Write-Step 'Waiting for /health and /ready'
$ok = $false
foreach ($i in 1..30) {
    try {
        $h = Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 "$baseUrl/health"
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 "$baseUrl/ready"
        if ($h.StatusCode -eq 200 -and $r.StatusCode -eq 200) { $ok = $true; break }
    } catch { Start-Sleep -Seconds 10 }
}
if ($ok) { Write-Host "    /health and /ready: 200" -ForegroundColor Green }
else {
    Write-Warn 'Not ready after 5 minutes. Logs:'
    Write-Host "    az containerapp logs show -n $appName -g $ResourceGroup --tail 100"
    Write-Host "    az containerapp logs show -n $workerName -g $ResourceGroup --tail 100"
}

Write-Step 'Done'
Write-Host "    URL:      $baseUrl"
Write-Host "    Image:    $image"
Write-Host "    Database: $pgHost"
Write-Host "    State:    $StateFile (contains secrets; keep it private)"
if (-not $state.oidcClientId) {
    Write-Host "`n    Ask your Entra admin for an app registration (details in deploy/azure/README.md):"
    Write-Host "      Redirect URIs (Web): $baseUrl/auth/callback   and   $baseUrl/logged-out"
    Write-Host "      App role:            value '$OidcAdminValue' (allowed member types: Users/Groups)"
    Write-Host '      A client secret, sent to you securely'
}
