#!/bin/bash
# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  AZURE AI GATEWAY — Container Apps Deploy Script                       ║
# ║  Single-endpoint router: AOAI Custom · PAYG · PTU · AI Foundry · AML  ║
# ║  Auth: AAD SPN (MSAL) + Ocp-Apim-Subscription-Key                     ║
# ║                                                                         ║
# ║  Prerequisites                                                          ║
# ║    az login  (or run inside Cloud Shell / DevOps pipeline)             ║
# ║    Docker daemon running locally  (for local build path only)          ║
# ╚══════════════════════════════════════════════════════════════════════════╝

set -euo pipefail

# ════════════════════════════════════════════════════════════════════════════
# 1. CONFIGURATION — edit these before first run
# ════════════════════════════════════════════════════════════════════════════

SUBSCRIPTION_ID="<YOUR-AZURE-SUBSCRIPTION-ID>"          # az account list
RESOURCE_GROUP="rg-ai-gateway"
LOCATION="eastus"                                        # change as needed
ACR_NAME="acraigw$(openssl rand -hex 4)"                # unique ACR name
IMAGE_TAG="azure-ai-gateway:latest"
APP_ENV_NAME="cae-ai-gateway"
APP_NAME="azure-ai-gateway"

# ── AAD Service Principal ──────────────────────────────────────────────────
AZURE_TENANT_ID="<YOUR-TENANT-ID>"
AZURE_CLIENT_ID="<YOUR-SPN-CLIENT-ID>"
AZURE_CLIENT_SECRET="<YOUR-SPN-CLIENT-SECRET>"          # use Key Vault ref in prod
AZURE_AAD_SCOPE="https://cognitiveservices.azure.com/.default"

# ── Gateway master API key (clients call the gateway with this) ───────────
GATEWAY_API_KEY="$(openssl rand -hex 32)"               # auto-generated; save this!

# ── Backend subscription / API keys ───────────────────────────────────────
AOAI_CUSTOM_SUBKEY="<YOUR-AOAI-CUSTOM-SUBSCRIPTION-KEY>"
AOAI_PAYG_SUBKEY="<YOUR-AOAI-PAYG-SUBSCRIPTION-KEY>"
AOAI_PTU_SUBKEY="<YOUR-AOAI-PTU-SUBSCRIPTION-KEY>"
AI_FOUNDRY_SUBKEY="<YOUR-AI-FOUNDRY-SUBSCRIPTION-KEY>"
AML_SUBKEY="<YOUR-AML-WORKSPACE-SUBSCRIPTION-KEY>"
OPENAI_API_KEY="<YOUR-OPENAI-DOT-COM-API-KEY>"         # leave blank if not using

# ── Backend endpoint URLs ──────────────────────────────────────────────────
# Azure OpenAI Custom deployment  (include api-version query param)
AOAI_CUSTOM_URL="https://<RESOURCE>.openai.azure.com/openai/deployments/<DEPLOY-NAME>/chat/completions?api-version=2024-05-01-preview"

# Azure OpenAI Pay-as-you-go
AOAI_PAYG_URL="https://<RESOURCE>.openai.azure.com/openai/deployments/<DEPLOY-NAME>/chat/completions?api-version=2024-05-01-preview"

# Azure OpenAI PTU (Provisioned Throughput Unit) endpoint
AOAI_PTU_URL="https://<PTU-RESOURCE>.openai.azure.com/openai/deployments/<PTU-DEPLOY>/chat/completions?api-version=2024-05-01-preview"

# Azure AI Foundry / Model Catalog inference endpoint
AI_FOUNDRY_URL="https://<PROJECT>.inference.ai.azure.com/models/<MODEL-NAME>/chat/completions"

# Azure ML Workspace – managed online endpoint
AML_URL="https://<ENDPOINT-NAME>.<REGION>.inference.ml.azure.com/score"

# ── Scaling / resources ────────────────────────────────────────────────────
MIN_REPLICAS=1
MAX_REPLICAS=10
CPU_CORES="1.0"
MEMORY_GI="2.0"
REQUEST_TIMEOUT_S=60
MAX_RETRIES=3

# ════════════════════════════════════════════════════════════════════════════
# 2. PREFLIGHT
# ════════════════════════════════════════════════════════════════════════════
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  Azure AI Gateway — Deployment Starting                 ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo "   Subscription : $SUBSCRIPTION_ID"
echo "   Resource Group: $RESOURCE_GROUP"
echo "   Location      : $LOCATION"
echo "   App           : $APP_NAME"
echo ""

az account set --subscription "$SUBSCRIPTION_ID"

# ════════════════════════════════════════════════════════════════════════════
# 3. ENABLE REQUIRED AZURE RESOURCE PROVIDERS
# ════════════════════════════════════════════════════════════════════════════
echo "▶ Registering resource providers..."
az provider register --namespace Microsoft.App            --wait
az provider register --namespace Microsoft.ContainerRegistry --wait
az provider register --namespace Microsoft.OperationalInsights --wait
az provider register --namespace Microsoft.CognitiveServices  --wait

# ════════════════════════════════════════════════════════════════════════════
# 4. RESOURCE GROUP
# ════════════════════════════════════════════════════════════════════════════
echo "▶ Creating resource group '$RESOURCE_GROUP'..."
az group create \
  --name     "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --output   none

# ════════════════════════════════════════════════════════════════════════════
# 5. AZURE CONTAINER REGISTRY (ACR)
# ════════════════════════════════════════════════════════════════════════════
echo "▶ Creating Azure Container Registry '$ACR_NAME'..."
az acr create \
  --resource-group "$RESOURCE_GROUP" \
  --name           "$ACR_NAME" \
  --sku            Basic \
  --admin-enabled  true \
  --output         none

ACR_SERVER=$(az acr show \
  --name           "$ACR_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query loginServer \
  --output tsv)

FULL_IMAGE="${ACR_SERVER}/${IMAGE_TAG}"

# ════════════════════════════════════════════════════════════════════════════
# 6. BUILD & PUSH IMAGE VIA ACR TASKS (no local Docker required)
# ════════════════════════════════════════════════════════════════════════════
echo "▶ Building and pushing image via ACR Tasks..."
az acr build \
  --registry        "$ACR_NAME" \
  --image           "$IMAGE_TAG" \
  --file            Dockerfile \
  .

# ════════════════════════════════════════════════════════════════════════════
# 7. LOG ANALYTICS WORKSPACE  (Container Apps observability)
# ════════════════════════════════════════════════════════════════════════════
echo "▶ Creating Log Analytics workspace..."
LAW_NAME="law-ai-gateway"
az monitor log-analytics workspace create \
  --resource-group "$RESOURCE_GROUP" \
  --workspace-name "$LAW_NAME" \
  --output         none

LAW_CUSTOMER_ID=$(az monitor log-analytics workspace show \
  --resource-group "$RESOURCE_GROUP" \
  --workspace-name "$LAW_NAME" \
  --query           customerId \
  --output          tsv)

LAW_SHARED_KEY=$(az monitor log-analytics workspace get-shared-keys \
  --resource-group "$RESOURCE_GROUP" \
  --workspace-name "$LAW_NAME" \
  --query           primarySharedKey \
  --output          tsv)

# ════════════════════════════════════════════════════════════════════════════
# 8. CONTAINER APPS ENVIRONMENT
# ════════════════════════════════════════════════════════════════════════════
echo "▶ Creating Container Apps environment '$APP_ENV_NAME'..."
az containerapp env create \
  --name                           "$APP_ENV_NAME" \
  --resource-group                 "$RESOURCE_GROUP" \
  --location                       "$LOCATION" \
  --logs-workspace-id              "$LAW_CUSTOMER_ID" \
  --logs-workspace-key             "$LAW_SHARED_KEY" \
  --output                         none

# ════════════════════════════════════════════════════════════════════════════
# 9. DEPLOY CONTAINER APP
# ════════════════════════════════════════════════════════════════════════════
echo "▶ Deploying Container App '$APP_NAME'..."
echo "   GATEWAY_API_KEY = $GATEWAY_API_KEY"
echo "   ⚠  Save this key — it is the bearer token your clients must send."
echo ""

az containerapp create \
  --name                   "$APP_NAME" \
  --resource-group         "$RESOURCE_GROUP" \
  --environment            "$APP_ENV_NAME" \
  --image                  "$FULL_IMAGE" \
  --registry-server        "$ACR_SERVER" \
  --registry-identity      system \
  --target-port            8080 \
  --ingress                external \
  --min-replicas           "$MIN_REPLICAS" \
  --max-replicas           "$MAX_REPLICAS" \
  --cpu                    "$CPU_CORES" \
  --memory                 "${MEMORY_GI}Gi" \
  --env-vars \
    "PORT=8080" \
    "AZURE_TENANT_ID=${AZURE_TENANT_ID}" \
    "AZURE_CLIENT_ID=${AZURE_CLIENT_ID}" \
    "AZURE_CLIENT_SECRET=secretref:azure-client-secret" \
    "AZURE_AAD_SCOPE=${AZURE_AAD_SCOPE}" \
    "GATEWAY_API_KEY=secretref:gateway-api-key" \
    "AOAI_CUSTOM_SUBKEY=secretref:aoai-custom-subkey" \
    "AOAI_PAYG_SUBKEY=secretref:aoai-payg-subkey" \
    "AOAI_PTU_SUBKEY=secretref:aoai-ptu-subkey" \
    "AI_FOUNDRY_SUBKEY=secretref:ai-foundry-subkey" \
    "AML_SUBKEY=secretref:aml-subkey" \
    "OPENAI_API_KEY=secretref:openai-api-key" \
    "AOAI_CUSTOM_URL=${AOAI_CUSTOM_URL}" \
    "AOAI_PAYG_URL=${AOAI_PAYG_URL}" \
    "AOAI_PTU_URL=${AOAI_PTU_URL}" \
    "AI_FOUNDRY_URL=${AI_FOUNDRY_URL}" \
    "AML_URL=${AML_URL}" \
    "REQUEST_TIMEOUT_S=${REQUEST_TIMEOUT_S}" \
    "MAX_RETRIES=${MAX_RETRIES}" \
  --secrets \
    "azure-client-secret=${AZURE_CLIENT_SECRET}" \
    "gateway-api-key=${GATEWAY_API_KEY}" \
    "aoai-custom-subkey=${AOAI_CUSTOM_SUBKEY}" \
    "aoai-payg-subkey=${AOAI_PAYG_SUBKEY}" \
    "aoai-ptu-subkey=${AOAI_PTU_SUBKEY}" \
    "ai-foundry-subkey=${AI_FOUNDRY_SUBKEY}" \
    "aml-subkey=${AML_SUBKEY}" \
    "openai-api-key=${OPENAI_API_KEY}" \
  --output none

# ════════════════════════════════════════════════════════════════════════════
# 10. ASSIGN MANAGED IDENTITY TO PULL FROM ACR
# ════════════════════════════════════════════════════════════════════════════
echo "▶ Granting Container App system identity pull access to ACR..."
APP_PRINCIPAL_ID=$(az containerapp show \
  --name           "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query           identity.principalId \
  --output          tsv)

ACR_ID=$(az acr show \
  --name           "$ACR_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query           id \
  --output          tsv)

az role assignment create \
  --assignee    "$APP_PRINCIPAL_ID" \
  --role        AcrPull \
  --scope       "$ACR_ID" \
  --output      none

# ════════════════════════════════════════════════════════════════════════════
# 11. OUTPUT
# ════════════════════════════════════════════════════════════════════════════
FQDN=$(az containerapp show \
  --name           "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query           properties.configuration.ingress.fqdn \
  --output          tsv)

GATEWAY_URL="https://${FQDN}"

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  ✅  DEPLOYED SUCCESSFULLY                              ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║  Gateway URL  : $GATEWAY_URL"
echo "║  Health check : ${GATEWAY_URL}/health"
echo "║  GATEWAY_API_KEY : $GATEWAY_API_KEY"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║  Quick smoke test:"
echo "║"
echo "║  curl -s ${GATEWAY_URL}/v1/chat/completions \\"
echo "║    -H 'Authorization: Bearer ${GATEWAY_API_KEY}' \\"
echo "║    -H 'Content-Type: application/json' \\"
echo "║    -d '{"
echo '║         "model":"gpt-4o",'
echo '║         "messages":[{"role":"user","content":"Hello"}]'
echo "║        }'"
echo "║"
echo "║  To force a specific backend add header:"
echo "║    X-Route-Backend: ptu"
echo "║    X-Route-Backend: aoai_custom | aoai_payg | ai_foundry | aml"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "▶ Audit log endpoint : ${GATEWAY_URL}/v1/audit"
echo "▶ Backend health     : ${GATEWAY_URL}/v1/backends"
echo "▶ Model routing map  : ${GATEWAY_URL}/v1/models"
echo "▶ Rate limit status  : ${GATEWAY_URL}/v1/rate_limits"
