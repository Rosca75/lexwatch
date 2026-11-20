# Deploying lexwatch to Azure

`deploy.ps1` deploys lexwatch to Azure from Azure Cloud Shell (PowerShell). It needs only
**Contributor on one resource group**: it doesn't need the rights to create role assignments
or Entra app registrations.

| Component | Azure resource | Notes |
|---|---|---|
| Web app | Container App `lexwatch-app`, external ingress, 1 replica | HTTPS on `*.azurecontainerapps.io` |
| Worker | Container App `lexwatch-worker`, no ingress, exactly 1 replica | Daily job at 06:30 Europe/Luxembourg |
| Database | PostgreSQL Flexible Server 16, Burstable B1ms, 32 GB | TLS required. The firewall admits Azure services only. `unaccent` is allow-listed. |
| Image | Azure Container Registry, Basic tier | Built in the registry with `az acr run` (BuildKit), so Docker isn't needed in Cloud Shell |
| Secrets | Container Apps secrets | Covers `SECRET_KEY`, `DATABASE_URL`, the Foundry key and the OIDC client secret |
| Scoring | Existing Foundry account, accessed with an API key | Rerank deployment name passed with `-FoundryModel` |
| Login | Entra ID app registration | Created by an Entra admin (see below) |
| Email | None | `DIGEST_ENABLED=false` until an SMTP relay is available |

Sandbox trade-offs, to revisit before production:

- **Registry admin user** instead of a managed identity with the AcrPull role.
- **API key for Foundry** instead of the Cognitive Services User role.
- **Public Postgres endpoint** restricted to Azure services, instead of VNet integration with a private endpoint.

## Run

```powershell
git clone https://github.com/rosca75/lexwatch.git; cd lexwatch
./deploy/azure/deploy.ps1                       # infrastructure, image, app, worker
```

Re-run with more parameters as the prerequisites below become available. Each run reuses
what exists, and `-SkipBuild` reuses the last image:

```powershell
./deploy/azure/deploy.ps1 -SkipBuild -FoundryAccount <account> -FoundryResourceGroup <rg> -FoundryModel <deployment-name>
./deploy/azure/deploy.ps1 -SkipBuild -OidcClientId <application-client-id>   # prompts for the client secret
```

Generated names and secrets are kept in `~/.lexwatch/azure-deploy.json` (mode 600). Keep that
file private. If it is lost, a re-run creates a new registry, a new server and new secrets.

## Request to the Entra ID admin

Ask for an app registration named `lexwatch (sandbox)`, configured as follows:

- **Supported account types:** this organisation only (single tenant).
- **Platform Web, redirect URIs:** `https://<app-url>/auth/callback` and `https://<app-url>/logged-out`.
  The script prints `<app-url>`.
- **App role:** display name `lexwatch admin`, value `lexwatch-admin`, allowed member types
  Users/Groups. Assign it to the administrators.
- **Token configuration:** add the optional claim `email` to the ID token.
- **API permissions:** Microsoft Graph delegated `openid`, `profile`, `email`, with admin consent.
- **Client secret** (6 to 12 months), sent through a secure channel. Diary its expiry.
- **Restricting access (optional):** under *Enterprise applications → lexwatch → Properties*, set
  *Assignment required* = Yes, then assign the users or groups allowed in.

## Rerank deployment on Foundry

1. Open the Foundry account in the portal.
2. Go to *Models + endpoints → Deploy model*, then pick Cohere Rerank: v4.0 Fast, or v5 Fast if it is offered.
3. Use the model name as the deployment name.

Cohere models are billed through the Azure Marketplace, so a sandbox subscription may block the
purchase. If it does, the portal says so.

## Operate

```powershell
az containerapp logs show -n lexwatch-app -g <rg> --tail 100
az containerapp logs show -n lexwatch-worker -g <rg> --tail 100 --follow
az containerapp exec -n lexwatch-worker -g <rg> --command "lexwatch run-job"   # run the daily job now
```
