<#
.SYNOPSIS
    Despliega la web "SSIS -> ADF" en Azure App Service (Linux, Python).

.DESCRIPTION
    Crea (o reutiliza si ya existen):
      - App Service Plan Linux y Web App Python con identidad administrada
      - Configuracion: URL y API key del MCP (leida del Container App), Azure OpenAI, Storage
      - Permisos de la identidad: "Storage Blob Data Contributor" sobre el Storage y
        "Cognitive Services OpenAI User" sobre el recurso de Azure OpenAI / Foundry
    y publica el codigo (zip). Se puede ejecutar de nuevo para publicar cambios.

    Ejecutar desde la carpeta raiz del repositorio, con "az login" hecho.

.EXAMPLE
    .\scripts\deploy_webapp.ps1 -OpenAIResource FoundryLabInetum -OpenAIDeployment gpt-5.4-mini

.EXAMPLE
    # Reutilizar un plan Linux existente (sin coste adicional)
    .\scripts\deploy_webapp.ps1 -OpenAIResource FoundryLabInetum -OpenAIDeployment gpt-5.4-mini `
        -PlanName ASP-IAData01Lab-a9a3 -PlanResourceGroup IA-Data-01-Lab
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $OpenAIResource,                 # nombre del recurso Azure OpenAI / Foundry
    [Parameter(Mandatory = $true)]
    [string] $OpenAIDeployment,               # nombre de la implementacion (p. ej. gpt-5.4-mini)
    [string] $OpenAIResourceGroup = "",       # por defecto, se busca en la suscripcion

    [ValidatePattern('^[a-zA-Z0-9-]{2,60}$')]
    [string] $WebAppName      = "ssis-adf-web-lab01",   # unico global -> <nombre>.azurewebsites.net
    [string] $PlanName        = "asp-ssis-adf-web",
    [string] $PlanResourceGroup = "",                   # plan existente en otro grupo (la web se crea ahi)
    [string] $Sku             = "B1",
    [string] $ResourceGroup   = "Siss2adfLab",
    [string] $Location        = "eastus",
    [string] $Subscription    = "Inetum-Lab",
    [string] $StorageAccount  = "stsiss2adflab01",
    [string] $McpAppName      = "ssis-adf-agent",       # Container App del MCP
    [string] $DefaultInput    = "az://ssis-packages/Finanzas",
    [string] $DefaultOutput   = "az://adf-artifacts/Finanzas"
)

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

# --- helpers (mismo patron que deploy_azure.ps1 del repo del MCP) -----------
function Format-AzArgs($azArgs) {
    $text = ($azArgs -join ' ')
    return ($text -replace '(MCP_API_KEY=)\S+', '$1***')
}
function Invoke-Az {
    $ErrorActionPreference = "Continue"
    $out = & az @args
    if ($LASTEXITCODE -ne 0) { throw "Fallo: az $(Format-AzArgs $args)" }
    return $out
}
function Test-Az {
    $ErrorActionPreference = "Continue"
    & az @args *> $null
    return ($LASTEXITCODE -eq 0)
}
function Write-Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }

function New-DeployZip($root, $zipPath) {
    # Zip con rutas "/" (Compress-Archive de Windows PowerShell usa "\" y rompe en Linux).
    Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
    if (Test-Path $zipPath) { Remove-Item $zipPath }
    $zip = [System.IO.Compression.ZipFile]::Open($zipPath, 'Create')
    try {
        $files = @(Get-ChildItem -Path (Join-Path $root "app") -Recurse -File |
                   Where-Object { $_.FullName -notmatch '__pycache__' })
        $files += Get-Item (Join-Path $root "requirements.txt")
        foreach ($f in $files) {
            $rel = $f.FullName.Substring($root.Length).TrimStart('\', '/').Replace('\', '/')
            [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $f.FullName, $rel)
        }
    } finally { $zip.Dispose() }
    return $files.Count
}

# ---------------------------------------------------------------------------
Write-Step "Comprobando Azure CLI y sesion"
if (-not (Test-Path ".\app\main.py")) {
    throw "Ejecuta el script desde la carpeta raiz del repositorio ssis-adf-web."
}
if (-not (Test-Az account show)) { Invoke-Az login | Out-Null }
if ($Subscription) { Invoke-Az account set --subscription $Subscription | Out-Null }
Invoke-Az config set extension.use_dynamic_install=yes_without_prompt --only-show-errors | Out-Null
$subName = Invoke-Az account show --query name -o tsv
Write-Host "Suscripcion: $subName"

# ---------------------------------------------------------------------------
Write-Step "Leyendo la URL y la API key del MCP ($McpAppName)"
$mcpFqdn = Invoke-Az containerapp show -n $McpAppName -g $ResourceGroup `
    --query properties.configuration.ingress.fqdn -o tsv
$mcpUrl = "https://$mcpFqdn/mcp"
$mcpKeys = Invoke-Az containerapp secret show -n $McpAppName -g $ResourceGroup `
    --secret-name mcp-api-keys --query value -o tsv
$mcpKey = ($mcpKeys -split ',')[0].Trim()
if (-not $mcpKey) { throw "No se pudo leer la API key del MCP (secreto mcp-api-keys)." }
Write-Host "MCP: $mcpUrl"

# ---------------------------------------------------------------------------
Write-Step "Recurso de Azure OpenAI $OpenAIResource"
if (-not $OpenAIResourceGroup) {
    $OpenAIResourceGroup = Invoke-Az cognitiveservices account list `
        --query "[?name=='$OpenAIResource'].resourceGroup | [0]" -o tsv
    if (-not $OpenAIResourceGroup) { throw "No encuentro el recurso '$OpenAIResource' en la suscripcion." }
}
$acct = (Invoke-Az cognitiveservices account show -n $OpenAIResource -g $OpenAIResourceGroup -o json) `
    -join "`n" | ConvertFrom-Json
# Recursos Foundry / AI Services exponen un endpoint especifico de OpenAI
$oaiEndpoint = $acct.properties.endpoints.'OpenAI Language Model Instance API'
if (-not $oaiEndpoint) { $oaiEndpoint = $acct.properties.endpoint }
$oai = @{ id = $acct.id; endpoint = $oaiEndpoint }
if (-not (Test-Az cognitiveservices account deployment show -n $OpenAIResource -g $OpenAIResourceGroup `
        --deployment-name $OpenAIDeployment)) {
    Write-Host "Despliegues disponibles en ${OpenAIResource}:" -ForegroundColor Yellow
    & az cognitiveservices account deployment list -n $OpenAIResource -g $OpenAIResourceGroup `
        --query "[].{nombre:name, modelo:properties.model.name}" -o table | Out-Host
    throw "No existe el despliegue '$OpenAIDeployment'."
}
Write-Host "Endpoint: $($oai.endpoint)  Despliegue: $OpenAIDeployment"

# ---------------------------------------------------------------------------
# La web se crea en el mismo grupo que su plan.
$AppRg = if ($PlanResourceGroup) { $PlanResourceGroup } else { $ResourceGroup }
if (Test-Az appservice plan show -n $PlanName -g $AppRg) {
    $plan = (Invoke-Az appservice plan show -n $PlanName -g $AppRg -o json) -join "`n" | ConvertFrom-Json
    if (-not $plan.reserved) { throw "El plan '$PlanName' es de Windows; esta web necesita un plan Linux." }
    Write-Step "Usando el plan existente $PlanName ($($plan.sku.name), $($plan.location)) - sin coste adicional"
} else {
    if ($PlanResourceGroup) { throw "No existe el plan '$PlanName' en el grupo '$PlanResourceGroup'." }
    Write-Step "Creando App Service Plan $PlanName ($Sku, Linux, $Location)"
    Invoke-Az appservice plan create -n $PlanName -g $AppRg -l $Location `
        --is-linux --sku $Sku -o none
}

Write-Step "Web App $WebAppName"
if (-not (Test-Az webapp show -n $WebAppName -g $AppRg)) {
    Invoke-Az webapp create -n $WebAppName -g $AppRg -p $PlanName `
        --runtime "PYTHON:3.12" --https-only true -o none
}
Invoke-Az webapp identity assign -n $WebAppName -g $AppRg -o none
$principal = Invoke-Az webapp identity show -n $WebAppName -g $AppRg --query principalId -o tsv

Invoke-Az webapp config set -n $WebAppName -g $AppRg --always-on true --ftps-state Disabled `
    --startup-file "gunicorn -k uvicorn.workers.UvicornWorker -w 2 -b 0.0.0.0:8000 --timeout 300 app.main:app" `
    -o none
Invoke-Az webapp config appsettings set -n $WebAppName -g $AppRg -o none --settings `
    "MCP_URL=$mcpUrl" `
    "MCP_API_KEY=$mcpKey" `
    "AZURE_OPENAI_ENDPOINT=$($oai.endpoint)" `
    "AZURE_OPENAI_DEPLOYMENT=$OpenAIDeployment" `
    "AZURE_OPENAI_API_VERSION=v1" `
    "AZURE_STORAGE_ACCOUNT=$StorageAccount" `
    "DEFAULT_INPUT=$DefaultInput" `
    "DEFAULT_OUTPUT=$DefaultOutput" `
    "WEBSITES_PORT=8000" `
    "SCM_DO_BUILD_DURING_DEPLOYMENT=true"

# ---------------------------------------------------------------------------
Write-Step "Permisos de la identidad de la web"
$storageId = Invoke-Az storage account show -n $StorageAccount -g $ResourceGroup --query id -o tsv
$roles = @(
    @{ role = "Storage Blob Data Contributor";  scope = $storageId },
    @{ role = "Cognitive Services OpenAI User"; scope = $oai.id }
)
foreach ($r in $roles) {
    $exists = Invoke-Az role assignment list --assignee $principal --scope $r.scope `
        --role $r.role --query "[0].id" -o tsv
    if (-not $exists) {
        Invoke-Az role assignment create --assignee-object-id $principal `
            --assignee-principal-type ServicePrincipal --role $r.role --scope $r.scope -o none
        Write-Host "Asignado: $($r.role)"
    }
}

# ---------------------------------------------------------------------------
Write-Step "Publicando el codigo (la instalacion de dependencias tarda unos minutos)"
$root = (Get-Location).Path
$zipPath = Join-Path $env:TEMP "ssis-adf-web.zip"
$count = New-DeployZip $root $zipPath
Write-Host "Paquete: $count archivos"
Invoke-Az webapp deploy -n $WebAppName -g $AppRg --src-path $zipPath --type zip `
    --track-status false -o none
Remove-Item $zipPath -ErrorAction SilentlyContinue

# ---------------------------------------------------------------------------
$url = "https://$WebAppName.azurewebsites.net"
Write-Step "Comprobando $url/healthz (el primer arranque tarda unos minutos)"
$ok = $false
for ($i = 1; $i -le 40 -and -not $ok; $i++) {
    try {
        $h = Invoke-RestMethod "$url/healthz" -TimeoutSec 15
        if ($h.status -eq "ok") { $ok = $true }
    } catch { Start-Sleep -Seconds 15 }
}

Write-Host ""
if ($ok) {
    Write-Host "Listo. La web responde: $url" -ForegroundColor Green
    if ($h.missing_settings -and $h.missing_settings.Count) {
        Write-Host "Falta configurar: $($h.missing_settings -join ', ')" -ForegroundColor Yellow
    }
} else {
    Write-Host "La web no respondio todavia. Revisa los logs con:" -ForegroundColor Yellow
    Write-Host "  az webapp log tail -n $WebAppName -g $AppRg"
}
Write-Host ""
Write-Host "IMPORTANTE: activa el inicio de sesion antes de compartir la URL." -ForegroundColor Yellow
Write-Host "  Portal > $WebAppName > Authentication > Add identity provider > Microsoft"
Write-Host "  (crear registro de aplicacion nuevo, 'Require authentication')."
