"""
╔══════════════════════════════════════════════════════════════════════════╗
║  AZURE AI GATEWAY — Unified Endpoint Router                             ║
║  Single /v1/chat/completions → Azure OpenAI Custom · OpenAI PAYG ·     ║
║                                PTU · AI Foundry · AML Workspace         ║
║  Auth   : AAD SPN (MSAL client-credentials) + Ocp-Apim-Subscription-Key║
║  Features: Rate limiting · Retry/backoff · Circuit breaker · Audit log  ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import os, time, json, logging, threading, uuid, datetime
from collections import defaultdict, deque
from functools import wraps

import requests
from flask import Flask, request, jsonify, Response, g
from msal import ConfidentialClientApplication

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("azure-router")

app = Flask(__name__)
_lock = threading.Lock()

# ─────────────────────────────────────────────────────────────────────────────
# ENVIRONMENT CONFIGURATION
# All sensitive values come from environment variables / Key Vault refs
# ─────────────────────────────────────────────────────────────────────────────
class Config:
    # ── AAD / SPN ─────────────────────────────────────────────────────────
    TENANT_ID        = os.environ["AZURE_TENANT_ID"]
    CLIENT_ID        = os.environ["AZURE_CLIENT_ID"]
    CLIENT_SECRET    = os.environ["AZURE_CLIENT_SECRET"]
    AAD_SCOPE        = os.environ.get("AZURE_AAD_SCOPE",
                                      "https://cognitiveservices.azure.com/.default")

    # ── Subscription / API keys ───────────────────────────────────────────
    AOAI_CUSTOM_KEY  = os.environ.get("AOAI_CUSTOM_SUBKEY", "")    # Azure OpenAI Custom deployment
    AOAI_PAYG_KEY    = os.environ.get("AOAI_PAYG_SUBKEY", "")      # Azure OpenAI Pay-as-you-go
    AOAI_PTU_KEY     = os.environ.get("AOAI_PTU_SUBKEY", "")       # PTU (Provisioned Throughput)
    FOUNDRY_KEY      = os.environ.get("AI_FOUNDRY_SUBKEY", "")     # AI Foundry / Model Catalog
    AML_KEY          = os.environ.get("AML_SUBKEY", "")            # AML Workspace managed endpoint
    OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "")        # OpenAI.com PAYG

    # ── Endpoint URLs ─────────────────────────────────────────────────────
    AOAI_CUSTOM_URL  = os.environ.get("AOAI_CUSTOM_URL", "")
    # e.g. https://<resource>.openai.azure.com/openai/deployments/<deploy>/chat/completions?api-version=2024-05-01-preview
    AOAI_PAYG_URL    = os.environ.get("AOAI_PAYG_URL", "")
    AOAI_PTU_URL     = os.environ.get("AOAI_PTU_URL", "")
    AI_FOUNDRY_URL   = os.environ.get("AI_FOUNDRY_URL", "")
    # e.g. https://<project>.inference.ai.azure.com/models/<model>/chat/completions
    AML_URL          = os.environ.get("AML_URL", "")
    # e.g. https://<endpoint>.<region>.inference.ml.azure.com/score
    OPENAI_URL       = "https://api.openai.com/v1/chat/completions"

    # ── Gateway settings ──────────────────────────────────────────────────
    GATEWAY_API_KEY  = os.environ.get("GATEWAY_API_KEY", "changeme-set-this")
    REQUEST_TIMEOUT  = int(os.environ.get("REQUEST_TIMEOUT_S", "60"))
    MAX_RETRIES      = int(os.environ.get("MAX_RETRIES", "3"))
    PORT             = int(os.environ.get("PORT", "8080"))


cfg = Config()

# ─────────────────────────────────────────────────────────────────────────────
# MODEL → BACKEND ROUTING TABLE
# Priority order used for fallback chains
# ─────────────────────────────────────────────────────────────────────────────
#  backend keys (canonical names used throughout this file):
#    "aoai_custom"  – Azure OpenAI Custom deployment  (AAD + sub-key)
#    "aoai_payg"    – Azure OpenAI Pay-as-you-go      (AAD + sub-key)
#    "ptu"          – Azure OpenAI PTU                (AAD + sub-key)
#    "ai_foundry"   – Azure AI Foundry / Model Catalog(AAD + sub-key)
#    "aml"          – Azure ML Workspace endpoint     (AAD + sub-key)
#    "openai_payg"  – OpenAI.com                      (Bearer API key only)

ROUTE_TABLE = {
    # ── Model-name prefixes → primary backend (+ ordered fallback chain) ──
    "gpt-4o":              ["ptu",          "aoai_custom",  "aoai_payg"],
    "gpt-4o-mini":         ["aoai_payg",    "ptu",          "aoai_custom"],
    "gpt-4-turbo":         ["aoai_custom",  "ptu",          "aoai_payg"],
    "gpt-35-turbo":        ["aoai_payg",    "aoai_custom"],
    "gpt-3.5-turbo":       ["openai_payg"],
    "o1":                  ["aoai_custom",  "ptu"],
    "o3":                  ["ptu",          "aoai_custom"],
    "phi-4":               ["ai_foundry",   "aml"],
    "phi-3":               ["ai_foundry",   "aml"],
    "llama":               ["ai_foundry",   "aml"],
    "mistral":             ["ai_foundry",   "aml"],
    "cohere":              ["ai_foundry",   "aml"],
    "custom":              ["aoai_custom",  "ai_foundry",   "aml"],
    # fallback for unknown models
    "_default":            ["aoai_payg",    "aoai_custom",  "ai_foundry"],
}

# Explicit header override: X-Route-Backend: ptu  → always use that backend
VALID_BACKENDS = {"aoai_custom", "aoai_payg", "ptu", "ai_foundry", "aml", "openai_payg"}

# ─────────────────────────────────────────────────────────────────────────────
# BACKEND DESCRIPTOR
# ─────────────────────────────────────────────────────────────────────────────
BACKENDS = {
    "aoai_custom": {
        "label": "Azure OpenAI – Custom Deployment",
        "url":   lambda: cfg.AOAI_CUSTOM_URL,
        "auth":  "aad+subkey",
        "subkey": lambda: cfg.AOAI_CUSTOM_KEY,
    },
    "aoai_payg": {
        "label": "Azure OpenAI – Pay-as-you-go",
        "url":   lambda: cfg.AOAI_PAYG_URL,
        "auth":  "aad+subkey",
        "subkey": lambda: cfg.AOAI_PAYG_KEY,
    },
    "ptu": {
        "label": "Azure OpenAI – PTU (Provisioned Throughput)",
        "url":   lambda: cfg.AOAI_PTU_URL,
        "auth":  "aad+subkey",
        "subkey": lambda: cfg.AOAI_PTU_KEY,
    },
    "ai_foundry": {
        "label": "Azure AI Foundry / Model Catalog",
        "url":   lambda: cfg.AI_FOUNDRY_URL,
        "auth":  "aad+subkey",
        "subkey": lambda: cfg.FOUNDRY_KEY,
    },
    "aml": {
        "label": "Azure ML Workspace – Managed Endpoint",
        "url":   lambda: cfg.AML_URL,
        "auth":  "aad+subkey",
        "subkey": lambda: cfg.AML_KEY,
    },
    "openai_payg": {
        "label": "OpenAI.com – Pay-as-you-go",
        "url":   lambda: cfg.OPENAI_URL,
        "auth":  "apikey",
        "subkey": lambda: cfg.OPENAI_API_KEY,
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# AAD TOKEN CACHE  (per-backend, auto-refresh before expiry)
# ─────────────────────────────────────────────────────────────────────────────
_msal_app = ConfidentialClientApplication(
    client_id=cfg.CLIENT_ID,
    client_credential=cfg.CLIENT_SECRET,
    authority=f"https://login.microsoftonline.com/{cfg.TENANT_ID}",
)
_token_cache: dict = {}           # scope → {token, expires_at}
_token_lock  = threading.Lock()

def get_aad_token(scope: str = None) -> str:
    """Return a valid AAD Bearer token; refresh automatically if within 60s of expiry."""
    scope = scope or cfg.AAD_SCOPE
    with _token_lock:
        cached = _token_cache.get(scope)
        if cached and time.time() < cached["expires_at"] - 60:
            return cached["token"]
        result = _msal_app.acquire_token_for_client(scopes=[scope])
        if "access_token" not in result:
            raise RuntimeError(f"AAD token acquisition failed: {result.get('error_description')}")
        expires_at = time.time() + result.get("expires_in", 3600)
        _token_cache[scope] = {"token": result["access_token"], "expires_at": expires_at}
        log.info("AAD token refreshed (scope=%s, expires_in=%ss)", scope, result.get("expires_in"))
        return result["access_token"]

# ─────────────────────────────────────────────────────────────────────────────
# RATE LIMITER  — token-bucket per (client_ip, backend)
# ─────────────────────────────────────────────────────────────────────────────
class TokenBucket:
    """Thread-safe token bucket for rate limiting."""
    def __init__(self, capacity: int, refill_rate: float):
        self.capacity    = capacity
        self.refill_rate = refill_rate   # tokens/second
        self.tokens      = float(capacity)
        self.last_refill = time.monotonic()
        self._lock       = threading.Lock()

    def consume(self, tokens: int = 1) -> bool:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

# Per-backend rate limits  {backend: (capacity, refill_rate)}
BACKEND_RATE_LIMITS = {
    "ptu":         (200, 3.33),    # 200 burst, 200 RPM refill
    "aoai_custom": (100, 1.67),    # 100 burst, 100 RPM
    "aoai_payg":   (60,  1.0),     # 60 burst,  60 RPM
    "ai_foundry":  (80,  1.33),
    "aml":         (50,  0.83),
    "openai_payg": (40,  0.67),
}

# Global buckets keyed by (client_ip, backend)
_rate_buckets: dict = {}
_rate_lock = threading.Lock()

def get_bucket(client_ip: str, backend: str) -> TokenBucket:
    key = (client_ip, backend)
    with _rate_lock:
        if key not in _rate_buckets:
            cap, rate = BACKEND_RATE_LIMITS.get(backend, (60, 1.0))
            _rate_buckets[key] = TokenBucket(cap, rate)
        return _rate_buckets[key]

# ─────────────────────────────────────────────────────────────────────────────
# CIRCUIT BREAKER  — per backend
# ─────────────────────────────────────────────────────────────────────────────
class CircuitBreaker:
    CLOSED   = "CLOSED"
    OPEN     = "OPEN"
    HALF_OPEN= "HALF_OPEN"

    def __init__(self, failure_threshold=5, recovery_timeout=30):
        self.threshold  = failure_threshold
        self.timeout    = recovery_timeout
        self.state      = self.CLOSED
        self.failures   = 0
        self.opened_at  = 0
        self._lock      = threading.Lock()

    def call_allowed(self) -> bool:
        with self._lock:
            if self.state == self.CLOSED:
                return True
            if self.state == self.OPEN:
                if time.monotonic() - self.opened_at > self.timeout:
                    self.state = self.HALF_OPEN
                    log.info("Circuit HALF_OPEN → probing")
                    return True
                return False
            return True  # HALF_OPEN: allow one probe

    def record_success(self):
        with self._lock:
            self.failures = 0
            self.state    = self.CLOSED

    def record_failure(self):
        with self._lock:
            self.failures += 1
            if self.failures >= self.threshold:
                self.state     = self.OPEN
                self.opened_at = time.monotonic()
                log.warning("Circuit OPEN for backend after %d failures", self.failures)

_breakers: dict[str, CircuitBreaker] = {b: CircuitBreaker() for b in BACKENDS}

# ─────────────────────────────────────────────────────────────────────────────
# AUDIT LOG  — ring buffer
# ─────────────────────────────────────────────────────────────────────────────
_audit: deque = deque(maxlen=1000)
_audit_lock   = threading.Lock()

def audit(request_id, client_ip, model, backend, status, latency_ms, error=None):
    entry = {
        "ts":          datetime.datetime.utcnow().isoformat() + "Z",
        "request_id":  request_id,
        "client_ip":   client_ip,
        "model":       model,
        "backend":     backend,
        "status":      status,
        "latency_ms":  latency_ms,
        "error":       error,
    }
    with _audit_lock:
        _audit.appendleft(entry)
    log.info("AUDIT %s", json.dumps(entry))

# ─────────────────────────────────────────────────────────────────────────────
# ROUTING HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def resolve_backend_chain(model: str, override: str = None) -> list[str]:
    """Return ordered list of backends to try for the given model."""
    if override and override in VALID_BACKENDS:
        return [override]
    model_lower = model.lower()
    for prefix, chain in ROUTE_TABLE.items():
        if prefix != "_default" and model_lower.startswith(prefix):
            return chain
    return ROUTE_TABLE["_default"]


def build_headers(backend_key: str) -> dict:
    """Build HTTP headers for the given backend (AAD token + sub-key or plain API key)."""
    backend = BACKENDS[backend_key]
    headers = {"Content-Type": "application/json"}

    if backend["auth"] == "aad+subkey":
        token  = get_aad_token()
        subkey = backend["subkey"]()
        headers["Authorization"]              = f"Bearer {token}"
        headers["Ocp-Apim-Subscription-Key"]  = subkey
    elif backend["auth"] == "apikey":
        headers["Authorization"] = f"Bearer {backend['subkey']()}"

    return headers


def call_backend(backend_key: str, payload: dict, timeout: int) -> requests.Response:
    """Make a single POST to a backend; raises on network error."""
    url     = BACKENDS[backend_key]["url"]()
    headers = build_headers(backend_key)
    return requests.post(url, json=payload, headers=headers, timeout=timeout)

# ─────────────────────────────────────────────────────────────────────────────
# GATEWAY AUTHENTICATION MIDDLEWARE
# Clients must send:  Authorization: Bearer <GATEWAY_API_KEY>
#                 or  Ocp-Apim-Subscription-Key: <GATEWAY_API_KEY>
# ─────────────────────────────────────────────────────────────────────────────
def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        sub_key     = request.headers.get("Ocp-Apim-Subscription-Key", "")
        token       = ""
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        elif sub_key:
            token = sub_key

        if not token or token != cfg.GATEWAY_API_KEY:
            log.warning("AUTH FAIL from %s", request.remote_addr)
            return jsonify({"error": {"message": "Unauthorized", "code": 401}}), 401
        return f(*args, **kwargs)
    return wrapper

# ─────────────────────────────────────────────────────────────────────────────
# CORE ROUTE  — POST /v1/chat/completions
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/v1/chat/completions", methods=["POST"])
@require_auth
def chat_completions():
    request_id = str(uuid.uuid4())
    client_ip  = request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()
    t0         = time.monotonic()

    # ── Parse request body ────────────────────────────────────────────────
    try:
        body = request.get_json(force=True)
    except Exception:
        return jsonify({"error": {"message": "Invalid JSON body", "code": 400}}), 400

    model    = body.get("model", "gpt-4o")
    override = request.headers.get("X-Route-Backend", "").lower().strip()
    chain    = resolve_backend_chain(model, override)

    log.info("[%s] model=%s chain=%s client=%s", request_id, model, chain, client_ip)

    last_error = None
    for backend_key in chain:
        # ── Circuit breaker ───────────────────────────────────────────────
        breaker = _breakers[backend_key]
        if not breaker.call_allowed():
            log.warning("[%s] Circuit OPEN for %s — skipping", request_id, backend_key)
            continue

        # ── Rate limiter ──────────────────────────────────────────────────
        bucket = get_bucket(client_ip, backend_key)
        if not bucket.consume():
            latency = int((time.monotonic() - t0) * 1000)
            audit(request_id, client_ip, model, backend_key, 429, latency,
                  "rate_limited")
            log.warning("[%s] RATE LIMIT hit for %s/%s", request_id, client_ip, backend_key)
            # Try next backend in chain rather than 429 immediately
            last_error = {"status": 429, "backend": backend_key, "error": "rate_limited"}
            continue

        # ── Call backend with retry + exponential backoff ─────────────────
        attempt = 0
        while attempt < cfg.MAX_RETRIES:
            attempt += 1
            try:
                resp = call_backend(backend_key, body, cfg.REQUEST_TIMEOUT)
                latency = int((time.monotonic() - t0) * 1000)

                if resp.status_code == 200:
                    breaker.record_success()
                    audit(request_id, client_ip, model, backend_key, 200, latency)
                    log.info("[%s] OK backend=%s latency=%dms", request_id, backend_key, latency)
                    # Propagate response as-is (preserves streaming tokens, usage, etc.)
                    return Response(
                        resp.content,
                        status=200,
                        content_type=resp.headers.get("Content-Type", "application/json"),
                        headers={
                            "X-Request-Id":      request_id,
                            "X-Routed-Backend":  backend_key,
                            "X-Latency-Ms":      str(latency),
                        },
                    )

                elif resp.status_code == 429:
                    # Respect Retry-After header from upstream
                    retry_after = int(resp.headers.get("Retry-After", "2"))
                    log.warning("[%s] 429 from %s attempt=%d retry_after=%ds",
                                request_id, backend_key, attempt, retry_after)
                    if attempt < cfg.MAX_RETRIES:
                        time.sleep(min(retry_after, 10))
                    last_error = {"status": 429, "backend": backend_key}
                    continue

                elif resp.status_code in (500, 502, 503, 504):
                    breaker.record_failure()
                    backoff = 2 ** (attempt - 1)
                    log.warning("[%s] %d from %s attempt=%d backoff=%ds",
                                request_id, resp.status_code, backend_key, attempt, backoff)
                    if attempt < cfg.MAX_RETRIES:
                        time.sleep(backoff)
                    last_error = {"status": resp.status_code, "backend": backend_key,
                                  "body": resp.text[:200]}
                    continue

                else:
                    # 4xx client errors — don't retry, surface immediately
                    latency = int((time.monotonic() - t0) * 1000)
                    audit(request_id, client_ip, model, backend_key, resp.status_code, latency,
                          resp.text[:200])
                    try:
                        err_body = resp.json()
                    except Exception:
                        err_body = {"message": resp.text}
                    return jsonify({
                        "error": err_body,
                        "x_request_id":     request_id,
                        "x_routed_backend": backend_key,
                    }), resp.status_code

            except requests.exceptions.Timeout:
                breaker.record_failure()
                log.error("[%s] TIMEOUT calling %s attempt=%d", request_id, backend_key, attempt)
                if attempt < cfg.MAX_RETRIES:
                    time.sleep(2 ** (attempt - 1))
                last_error = {"status": 504, "backend": backend_key, "error": "timeout"}

            except requests.exceptions.ConnectionError as exc:
                breaker.record_failure()
                log.error("[%s] CONNECTION ERROR %s: %s", request_id, backend_key, exc)
                last_error = {"status": 502, "backend": backend_key, "error": str(exc)}
                break  # don't retry connection errors — move to next backend

    # ── All backends exhausted ────────────────────────────────────────────
    latency = int((time.monotonic() - t0) * 1000)
    audit(request_id, client_ip, model, "ALL", 503, latency, str(last_error))
    log.error("[%s] ALL backends failed last_error=%s", request_id, last_error)
    return jsonify({
        "error": {
            "message": "All backends unavailable or rate-limited. Please retry.",
            "code":    503,
            "details": last_error,
        },
        "x_request_id": request_id,
    }), 503


# ─────────────────────────────────────────────────────────────────────────────
# MANAGEMENT ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "ts": datetime.datetime.utcnow().isoformat() + "Z"})


@app.route("/v1/models", methods=["GET"])
@require_auth
def list_models():
    """Return available model → backend mappings."""
    return jsonify({
        "object": "list",
        "data": [
            {"id": k, "backends": v, "object": "model"}
            for k, v in ROUTE_TABLE.items()
            if k != "_default"
        ],
    })


@app.route("/v1/backends", methods=["GET"])
@require_auth
def list_backends():
    """Return backend health / circuit-breaker states."""
    result = {}
    for key, meta in BACKENDS.items():
        br = _breakers[key]
        result[key] = {
            "label":           meta["label"],
            "circuit_state":   br.state,
            "circuit_failures": br.failures,
            "url_configured":  bool(meta["url"]()),
        }
    return jsonify(result)


@app.route("/v1/audit", methods=["GET"])
@require_auth
def audit_log():
    limit = min(int(request.args.get("limit", 50)), 1000)
    with _audit_lock:
        entries = list(_audit)[:limit]
    return jsonify({"count": len(entries), "entries": entries})


@app.route("/v1/rate_limits", methods=["GET"])
@require_auth
def rate_limits():
    """Return current token bucket states (for observability)."""
    result = {}
    with _rate_lock:
        for (ip, be), bucket in _rate_buckets.items():
            result[f"{ip}:{be}"] = {
                "tokens_remaining": round(bucket.tokens, 1),
                "capacity":         bucket.capacity,
            }
    return jsonify(result)


# ─────────────────────────────────────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Azure AI Gateway starting on port %d", cfg.PORT)
    app.run(host="0.0.0.0", port=cfg.PORT, debug=False, threaded=True)
