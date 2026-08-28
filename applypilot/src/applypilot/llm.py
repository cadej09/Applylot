"""
Unified LLM client for ApplyPilot.

Auto-detects the PRIMARY provider from environment:
  GEMINI_API_KEY  -> Google Gemini (default: gemini-2.0-flash)
  OPENAI_API_KEY  -> OpenAI (default: gpt-4o-mini)
  LLM_URL         -> Any OpenAI-compatible endpoint (Anthropic /v1, Ollama, ...)

LLM_MODEL env var overrides the model name for any provider.

FALLBACK CHAIN (9router-style): when the primary hard-fails (credits
exhausted, invalid key, rate-limit retries used up, timeouts), requests
automatically fall through to the next configured provider so the pipeline
keeps running instead of dying:

  primary -> Gemini (GEMINI_API_KEY) -> NVIDIA NIM (NVIDIA_API_KEY)
          -> Groq (GROQ_API_KEY) -> local Ollama

Optional env vars: NVIDIA_MODEL, GROQ_MODEL, LOCAL_MODEL override fallback
model names; LLM_FALLBACK_LOCAL=0 disables the Ollama last resort.
"""

import logging
import os
import time

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vertex AI (Gemini via Google Cloud billing — uses trial credits)
# ---------------------------------------------------------------------------
# Auth is a service-account JSON (VERTEX_SA_KEY) that mints ~1h OAuth tokens.
# The client accepts a CALLABLE api_key, so tokens refresh transparently.

_VERTEX_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_vertex_creds = None


def _vertex_config() -> tuple[str, str] | None:
    """(base_url, default_model) when Vertex is configured, else None."""
    project = os.environ.get("VERTEX_PROJECT", "")
    sa_key = os.environ.get("VERTEX_SA_KEY", "")
    if not (project and sa_key and os.path.exists(sa_key)):
        return None
    loc = os.environ.get("VERTEX_LOCATION", "us-central1")
    host = ("aiplatform.googleapis.com" if loc == "global"
            else f"{loc}-aiplatform.googleapis.com")
    base = (f"https://{host}/v1/projects/{project}/locations/{loc}"
            f"/endpoints/openapi")
    return base, os.environ.get("VERTEX_MODEL", "google/gemini-2.5-flash")


def _vertex_token() -> str:
    """Return a fresh OAuth access token, refreshing when near expiry."""
    global _vertex_creds
    from google.auth.transport.requests import Request as _GARequest
    from google.oauth2 import service_account
    if _vertex_creds is None:
        _vertex_creds = service_account.Credentials.from_service_account_file(
            os.environ["VERTEX_SA_KEY"], scopes=[_VERTEX_SCOPE])
    if not _vertex_creds.valid:
        _vertex_creds.refresh(_GARequest())
    return _vertex_creds.token


# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def _detect_provider() -> tuple[str, str, str]:
    """Return (base_url, model, api_key) based on environment variables.

    Reads env at call time (not module import time) so that load_env() called
    in _bootstrap() is always visible here.
    """
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    local_url = os.environ.get("LLM_URL", "")
    model_override = os.environ.get("LLM_MODEL", "")

    if gemini_key and not local_url:
        return (
            "https://generativelanguage.googleapis.com/v1beta/openai",
            model_override or "gemini-2.0-flash",
            gemini_key,
        )

    if openai_key and not local_url:
        return (
            "https://api.openai.com/v1",
            model_override or "gpt-4o-mini",
            openai_key,
        )

    if local_url.strip().lower() == "vertex":
        cfg = _vertex_config()
        if not cfg:
            raise RuntimeError(
                "LLM_URL=vertex requires VERTEX_PROJECT and VERTEX_SA_KEY "
                "(path to a service-account JSON) in the environment/.env")
        base, model = cfg
        return base, model_override or model, _vertex_token

    if local_url.strip().lower() == "claude-cli":
        # Claude via the local Claude Code CLI: billed to the Max
        # subscription, NOT the API. Zero marginal cost, best-in-chain
        # writing quality. Requires `claude` on PATH and an active login.
        return "claude-cli", model_override or "haiku", ""

    if local_url:
        return (
            local_url.rstrip("/"),
            model_override or "local-model",
            os.environ.get("LLM_API_KEY", ""),
        )

    raise RuntimeError(
        "No LLM provider configured. "
        "Set GEMINI_API_KEY, OPENAI_API_KEY, or LLM_URL in your environment."
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_MAX_RETRIES = 2
# Cloud/HTTP providers must fail FAST so the FallbackLLM can advance to the
# next provider instead of hanging ~50 min on a stalled NIM socket. Only local
# models (Ollama on localhost) keep the long headroom for load + long gens.
_CLOUD_TIMEOUT = 75   # seconds — cloud/HTTP providers
_LOCAL_TIMEOUT = 600  # seconds — local models on modest GPUs
_TIMEOUT = _CLOUD_TIMEOUT  # back-compat default

# Base wait on first 429/503 (doubles each retry, caps at 60s).
# Gemini free tier is 15 RPM = 4s minimum between requests; 10s gives headroom.
_RATE_LIMIT_BASE_WAIT = 10


_GEMINI_COMPAT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
_GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"


class LLMClient:
    """Thin LLM client supporting OpenAI-compatible and native Gemini endpoints.

    For Gemini keys, starts on the OpenAI-compat layer. On a 403 (which
    happens with preview/experimental models not exposed via compat), it
    automatically switches to the native generateContent API and stays there
    for the lifetime of the process.
    """

    def __init__(self, base_url: str, model: str, api_key: str) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        # Local models (Ollama/localhost) get the long timeout; everything else
        # is a cloud endpoint that must fail fast into the fallback chain.
        self._is_local = (
            "localhost" in base_url or "127.0.0.1" in base_url
            or base_url == "claude-cli"
        )
        timeout = _LOCAL_TIMEOUT if self._is_local else _CLOUD_TIMEOUT
        self._client = httpx.Client(timeout=timeout)
        # True once we've confirmed the native Gemini API works for this model
        self._use_native_gemini: bool = False
        self._is_gemini: bool = base_url.startswith(_GEMINI_COMPAT_BASE)

    # -- Native Gemini API --------------------------------------------------

    def _chat_native_gemini(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the native Gemini generateContent API.

        Used automatically when the OpenAI-compat endpoint returns 403,
        which happens for preview/experimental models not exposed via compat.

        Converts OpenAI-style messages to Gemini's contents/systemInstruction
        format transparently.
        """
        contents: list[dict] = []
        system_parts: list[dict] = []

        for msg in messages:
            role = msg["role"]
            text = msg.get("content", "")
            if role == "system":
                system_parts.append({"text": text})
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": text}]})
            elif role == "assistant":
                # Gemini uses "model" instead of "assistant"
                contents.append({"role": "model", "parts": [{"text": text}]})

        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}

        url = f"{_GEMINI_NATIVE_BASE}/models/{self.model}:generateContent"
        resp = self._client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            params={"key": self.api_key},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    # -- OpenAI-compat API --------------------------------------------------

    def _chat_compat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the OpenAI-compatible endpoint."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        # api_key may be a callable (Vertex OAuth tokens refresh hourly)
        key = self.api_key() if callable(self.api_key) else self.api_key
        if key:
            headers["Authorization"] = f"Bearer {key}"

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # Moonshot/Kimi (kimi-k3 et al) FIX these sampling params server-side and
        # 400 on any explicit value other than the default:
        #   "invalid temperature: only 1 is allowed for this model"
        # Their docs say to omit them entirely, so drop temperature here rather
        # than sending our usual 0.2 (score) / 0.4 (tailor).
        if "moonshot.ai" in self.base_url or "moonshot.cn" in self.base_url:
            payload.pop("temperature", None)
            # kimi-k3 is a REASONING model: hidden reasoning_content is billed
            # against max_tokens BEFORE any answer is emitted. At max_tokens=200
            # a scoring prompt burned 185 tokens thinking and returned an empty
            # string (finish_reason=length) — which looks like a dead provider
            # but isn't. Production already asks for 4096/8192; this floor stops
            # a small budget from silently producing nothing.
            payload["max_tokens"] = max(int(max_tokens), 2000)
        # Gemini 2.5 Flash "thinks" by default and bills those hidden tokens.
        # The reasoning_effort switch is only supported on the AI Studio
        # compat endpoint — Vertex's compat layer 400s on it, so Vertex
        # calls keep default thinking (slightly pricier, still cheap).
        if "gemini-2.5-flash" in self.model and self._is_gemini:
            payload["reasoning_effort"] = os.environ.get(
                "GEMINI_REASONING_EFFORT", "none")

        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )

        # 403 on Gemini compat = model not available on compat layer.
        # Raise a specific sentinel so chat() can switch to native API.
        if resp.status_code == 403 and self._is_gemini:
            raise _GeminiCompatForbidden(resp)

        return self._handle_compat_response(resp)

    @staticmethod
    def _handle_compat_response(resp: httpx.Response) -> str:
        resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"]
        content = msg.get("content")
        # A 200 with content=null or "" is NOT a usable answer. Providers do
        # this when a reasoning model spends the whole token budget thinking
        # (finish_reason=length) or a safety filter blanks the reply. Returning
        # it verbatim handed None to callers that expect a string and crashed
        # every tailor job on 2026-07-28 ("'NoneType' object has no attribute
        # 'strip'"). Raise instead, so FallbackLLM moves to the next provider
        # exactly as it would for a 5xx.
        if content is None or not str(content).strip():
            finish = data["choices"][0].get("finish_reason")
            reasoning = msg.get("reasoning_content")
            hint = " (spent the budget on reasoning_content)" if reasoning else ""
            raise EmptyLLMResponse(
                f"provider returned empty content (finish_reason={finish}){hint}")
        return content

    # -- Claude Code CLI (Max-plan billing) ----------------------------------

    def _chat_claude_cli(self, messages: list[dict]) -> str:
        """Run the prompt through the local `claude` CLI (subscription-billed).

        Any failure (missing binary, session limit, empty output) raises
        immediately so FallbackLLM moves to the next provider instead of
        retrying here.
        """
        import shutil
        import subprocess

        exe = shutil.which("claude") or shutil.which("claude.cmd")
        if not exe:
            raise RuntimeError("claude CLI not found on PATH")

        system = "\n\n".join(
            m.get("content", "") for m in messages if m["role"] == "system")
        convo = "\n\n".join(
            m.get("content", "") for m in messages if m["role"] != "system")

        cmd = [exe, "-p", "--model", self.model, "--output-format", "text"]
        sys_file = None
        if system:
            # REPLACE the CLI's agent system prompt, don't append to it —
            # Claude Code's own prompt makes the model conversational, which
            # breaks strict-format outputs. Pass it via FILE: the npm
            # `claude.cmd` shim runs through cmd.exe, whose 8,191-char
            # command-line limit a full system prompt exceeds
            # ("The command line is too long", 2026-07-16).
            import tempfile
            fd, sys_file = tempfile.mkstemp(suffix=".txt", text=True)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(system)
            cmd += ["--system-prompt-file", sys_file]

        env = os.environ.copy()
        # Bill the Max plan, never the API — same guard as the apply launcher.
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)

        try:
            proc = subprocess.run(
                cmd, input=convo, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=600, env=env)
        finally:
            if sys_file:
                try:
                    os.remove(sys_file)
                except OSError:
                    pass
        out = (proc.stdout or "").strip()
        if proc.returncode != 0 or not out:
            err = (proc.stderr or "").strip()[:300]
            raise RuntimeError(
                f"claude CLI failed (rc={proc.returncode}): {err or 'empty output'}")
        return out

    # -- public API ---------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        """Send a chat completion request and return the assistant message text."""
        if self.base_url == "claude-cli":
            return self._chat_claude_cli(messages)
        # Qwen3 optimization: prepend /no_think to skip chain-of-thought
        # reasoning, saving tokens on structured extraction tasks.
        if "qwen" in self.model.lower() and messages:
            first = messages[0]
            if first.get("role") == "user" and not first["content"].startswith("/no_think"):
                messages = [{"role": first["role"], "content": f"/no_think\n{first['content']}"}] + messages[1:]

        for attempt in range(_MAX_RETRIES):
            try:
                # Route to native Gemini if we've already confirmed it's needed
                if self._use_native_gemini:
                    return self._chat_native_gemini(messages, temperature, max_tokens)

                return self._chat_compat(messages, temperature, max_tokens)

            except _GeminiCompatForbidden as exc:
                # Model not available on OpenAI-compat layer — switch to native.
                log.warning(
                    "Gemini compat endpoint returned 403 for model '%s'. "
                    "Switching to native generateContent API. "
                    "(Preview/experimental models are often compat-only on native.)",
                    self.model,
                )
                self._use_native_gemini = True
                # Retry immediately with native — don't count as a rate-limit wait
                try:
                    return self._chat_native_gemini(messages, temperature, max_tokens)
                except httpx.HTTPStatusError as native_exc:
                    raise RuntimeError(
                        f"Both Gemini endpoints failed. Compat: 403 Forbidden. "
                        f"Native: {native_exc.response.status_code} — "
                        f"{native_exc.response.text[:200]}"
                    ) from native_exc

            except httpx.HTTPStatusError as exc:
                resp = exc.response
                if resp.status_code in (429, 503) and attempt < _MAX_RETRIES - 1:
                    # Respect Retry-After header if provided (Gemini sends this).
                    retry_after = (
                        resp.headers.get("Retry-After")
                        or resp.headers.get("X-RateLimit-Reset-Requests")
                    )
                    if retry_after:
                        try:
                            wait = float(retry_after)
                        except (ValueError, TypeError):
                            wait = _RATE_LIMIT_BASE_WAIT * (2 ** attempt)
                    else:
                        wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)

                    # A Retry-After beyond a couple of minutes means a DAILY
                    # quota is exhausted (Groq free tier does this). Don't
                    # sleep for hours — treat the provider as down so the
                    # fallback chain switches immediately.
                    if wait > 120:
                        log.warning(
                            "LLM rate limited with Retry-After=%ds (daily "
                            "quota exhausted). Failing over to next provider.",
                            int(wait),
                        )
                        raise

                    log.warning(
                        "LLM rate limited (HTTP %s). Waiting %ds before retry %d/%d. "
                        "Tip: Gemini free tier = 15 RPM. Consider a paid account "
                        "or switching to a local model.",
                        resp.status_code, wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

            except httpx.TimeoutException:
                # Cloud providers: do NOT retry a timeout — a stalled cloud
                # socket means the provider is unhealthy, so fail over to the
                # next provider immediately instead of burning another full
                # timeout window. Only local models retry (transient GPU load).
                if self._is_local and attempt < _MAX_RETRIES - 1:
                    wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)
                    log.warning(
                        "Local LLM request timed out, retrying in %ds (attempt %d/%d)",
                        wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

        raise RuntimeError("LLM request failed after all retries")

    def ask(self, prompt: str, **kwargs) -> str:
        """Convenience: single user prompt -> assistant response."""
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        self._client.close()


class EmptyLLMResponse(Exception):
    """A provider answered 200 but with no usable content.

    Treated as a provider failure so FallbackLLM moves down the chain. Common
    causes: a reasoning model burned the whole token budget on hidden
    reasoning_content (finish_reason=length), or a safety filter blanked the
    reply. Before this existed the raw None reached callers and crashed the
    tailor stage with "'NoneType' object has no attribute 'strip'".
    """


class _GeminiCompatForbidden(Exception):
    """Sentinel: Gemini OpenAI-compat returned 403. Switch to native API."""
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"Gemini compat 403: {response.text[:200]}")


# ---------------------------------------------------------------------------
# Fallback chain (9router-style)
# ---------------------------------------------------------------------------

def _fallback_providers(primary_url: str) -> list[tuple[str, str, str]]:
    """Free/cheap backup providers, in preference order.

    Skips any entry that matches the primary URL (no point retrying the
    provider that just failed).
    """
    provs: list[tuple[str, str, str]] = []

    def _keys(*names: str) -> list[str]:
        """Keys for one provider, in order, skipping blanks and duplicates.

        A second account on the same provider is a second daily quota, so the
        variants are listed back-to-back: when account 1 is exhausted the very
        next hop is the same fast provider on a fresh quota, rather than
        dropping to a slower one (user supplied 2nd accounts 2026-07-27).
        """
        seen: set[str] = set()
        out: list[str] = []
        for n in names:
            v = os.environ.get(n, "").strip()
            if v and v not in seen:
                seen.add(v)
                out.append(v)
        return out

    # ── FREE providers first (zero marginal cost — user's #1 goal) ──────────
    # These are always in the chain and are exhausted BEFORE any paid provider
    # is ever contacted.
    for _k in _keys("NVIDIA_API_KEY", "NVIDIA_API_KEY_2", "NVIDIA_API_KEY_3"):
        provs.append((
            "https://integrate.api.nvidia.com/v1",
            os.environ.get("NVIDIA_MODEL", "nvidia/nemotron-3-super-120b-a12b"),
            _k,
        ))

    # Cerebras account 1 (free tier): a large JSON-capable model so an NVIDIA
    # NIM outage doesn't dead-end the writers (Groq's free tier 413s on large
    # tailor prompts). Cerebras dropped Llama entirely; gpt-oss-120b is the
    # strongest model they currently serve and returns clean JSON in
    # message.content. Verify the live list any time with:
    #   GET https://api.cerebras.ai/v1/models
    # Accounts 2 and 3 carry PREPAID CREDIT and live in the paid block below.
    for _k in _keys("CEREBRAS_API_KEY"):
        provs.append((
            "https://api.cerebras.ai/v1",
            os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b"),
            _k,
        ))

    # OpenRouter free tier: Llama 3.3 70B ":free" variant. 50 req/day free
    # (1000/day after a one-time $10 lifetime top-up). Sits before Groq because
    # unlike Groq it accepts full-size TAILOR prompts (Groq 413s on those), so
    # it's a genuine second writer behind NIM/Cerebras.
    for _k in _keys("OPENROUTER_API_KEY", "OPENROUTER_API_KEY_2", "OPENROUTER_API_KEY_3"):
        provs.append((
            "https://openrouter.ai/api/v1",
            # Model history — pick a NON-REASONING model here:
            #  * llama-3.3-70b:free  delisted (404, 2026-07-20).
            #  * nemotron-3-super    DERAILS on structured prompts ("We need to
            #    output in exact format...") instead of answering. HTTP 200, so
            #    the chain never failed over; wrecked two scoring runs.
            #  * ling-3.0-flash      is a REASONING model. Fine for short
            #    scoring replies (285 clean calls), but on a full tailor prompt
            #    it spent 7,746 of 8,192 completion tokens on reasoning_tokens
            #    and returned EMPTY (finish_reason=length) — every tailor job
            #    failed over on 2026-07-28.
            #  * gemma-4-26b-a4b     reasoning_tokens=0, emitted valid tailor
            #    JSON in 907 tokens and scores cleanly. Current default.
            # Verify a replacement on BOTH a scoring and a full-size tailor
            # prompt (scratchpad/test_ling_tailor.py) before switching.
            os.environ.get("OPENROUTER_MODEL", "google/gemma-4-26b-a4b-it:free"),
            _k,
        ))

    # Cloudflare Workers AI free tier: Llama 3.3 70B, 10k neurons/day.
    # OpenAI-compatible endpoint scoped to the account id.
    cf_account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    cf_key = os.environ.get("CLOUDFLARE_API_KEY", "")
    if cf_account and cf_key:
        provs.append((
            f"https://api.cloudflare.com/client/v4/accounts/{cf_account}/ai/v1",
            os.environ.get("CLOUDFLARE_MODEL", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"),
            cf_key,
        ))

    groq_key = os.environ.get("GROQ_API_KEY", "")
    if groq_key:
        provs.append((
            "https://api.groq.com/openai/v1",
            os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
            groq_key,
        ))

    # ══ PAID-CREDIT TIER (user rule 2026-07-27: "use the paid credits only
    # when needed") ══════════════════════════════════════════════════════════
    # Everything ABOVE this line costs nothing per call:
    #   - NVIDIA NIM, Cloudflare, Groq, Cerebras acct 1: free tiers.
    #   - OpenRouter: the ":free" model variants do NOT draw down the account
    #     balance. The one-time $10 top-up only raises the free-model ceiling
    #     from 50 to 1000 requests/day, so those three accounts stay free to
    #     use as long as OPENROUTER_MODEL keeps its ":free" suffix. Do not
    #     point OPENROUTER_MODEL at a non-free model without deciding to spend.
    # Everything BELOW burns real prepaid balance and is only reached once every
    # free provider above is exhausted.

    # Cerebras accounts 2 and 3 hold prepaid credit ($5 / $15 as of 07-27).
    for _k in _keys("CEREBRAS_API_KEY_2", "CEREBRAS_API_KEY_3"):
        provs.append((
            "https://api.cerebras.ai/v1",
            os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b"),
            _k,
        ))

    # Gemini AI Studio: two independent accounts (separate quotas). Both were
    # DISABLED in .env on 2026-07-27 after returning 429 "prepayment credits are
    # depleted" — ~$20 went on one full backlog scoring pass. Re-enable by
    # uncommenting the keys after a top-up; no code change needed.
    # Model note: gemini-2.5-flash 404s ("no longer available to new users") —
    # use the floating `gemini-flash-latest` alias. Verify the live list with:
    #   GET https://generativelanguage.googleapis.com/v1beta/openai/models
    gemini_model = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-flash-latest")
    for var in ("GEMINI_API_KEY", "GEMINI_API_KEY_2"):
        gkey = os.environ.get(var, "")
        if gkey:
            provs.append((
                "https://generativelanguage.googleapis.com/v1beta/openai",
                gemini_model,
                gkey,
            ))

    # Moonshot AI / Kimi K3 (2026-07-25). OpenAI-compatible on the .ai host
    # (the .cn host rejects this key). NOTE: paid — requires a top-up ($1 min),
    # so it sits last in the free block, reached only when everything else is
    # dry. kimi-k3 fixes temperature/top_p server-side; _chat_compat strips
    # temperature for moonshot hosts. kimi-k2.6 returned EMPTY content in
    # testing — do not use it; kimi-k2.7-code is the working alternate.
    moonshot_key = os.environ.get("MOONSHOT_API_KEY", "")
    if moonshot_key:
        provs.append((
            "https://api.moonshot.ai/v1",
            os.environ.get("MOONSHOT_MODEL", "kimi-k3"),
            moonshot_key,
        ))

    if os.environ.get("LLM_FALLBACK_LOCAL", "1") != "0":
        provs.append((
            "http://localhost:11434/v1",
            os.environ.get("LOCAL_MODEL", "llama3.1:8b"),
            "ollama",
        ))

    # ── PAID providers, only when explicitly opted in (default OFF) ─────────
    # Set LLM_ALLOW_PAID=1 to let the chain fall through to billed providers
    # (Vertex/Gemini and — dead-last — Anthropic Haiku) after every free
    # provider is exhausted. Off by default so a run can never incur cost.
    if os.environ.get("LLM_ALLOW_PAID", "0") == "1":
        vertex = _vertex_config()
        if vertex:
            provs.append((vertex[0], vertex[1], _vertex_token))

        gemini_key = os.environ.get("GEMINI_API_KEY", "")
        if gemini_key:
            provs.append((
                "https://generativelanguage.googleapis.com/v1beta/openai",
                os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash"),
                gemini_key,
            ))

        # Claude Haiku goes DEAD LAST: the ANTHROPIC_FALLBACK_KEY is known-dead
        # (always HTTP 400) and it is paid, so it only sits here as a last-ditch
        # quality-insurance slot behind the LLM_ALLOW_PAID flag.
        # NOTE: deliberately NOT named ANTHROPIC_API_KEY — the claude CLI (apply
        # agent) prefers that name over the Max-plan login and bills the API.
        ant_key = os.environ.get("ANTHROPIC_FALLBACK_KEY", "")
        if ant_key:
            provs.append((
                "https://api.anthropic.com/v1",
                os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
                ant_key,
            ))

    primary_root = primary_url.rstrip("/")
    return [p for p in provs if p[0].rstrip("/") != primary_root]


class FallbackLLM:
    """Wraps a chain of LLMClients: primary first, then free/cheap backups.

    Each client keeps its own internal retry loop for transient errors; this
    wrapper only steps in when a provider is hard-down (credits gone, key
    invalid, retries exhausted), so a dead primary never kills the pipeline.
    """

    def __init__(self, primary: LLMClient, fallbacks: list[LLMClient]) -> None:
        self._chain = [primary] + fallbacks
        # Index of the first client worth trying (advances when one dies).
        self._start = 0
        self.model = primary.model
        self.base_url = primary.base_url

    def demote_current(self, why: str = "unusable output") -> bool:
        """Skip the currently-promoted provider for the rest of the run.

        For providers that answer HTTP 200 with GARBAGE (e.g. OpenRouter's
        nemotron replying to a scoring prompt with resume-tailoring prose).
        Exception-based failover can't catch those — the call "succeeded" — so
        the caller detects the bad output and calls this to move down the chain.

        Returns True if another provider is available, False if this was the last.
        """
        if self._start + 1 >= len(self._chain):
            log.error("Cannot demote %s (%s): no providers left in the chain.",
                      self.base_url, why)
            return False
        dead = self._chain[self._start]
        self._start += 1
        nxt = self._chain[self._start]
        log.warning("LLM provider %s (%s) demoted — %s. Now using %s (%s).",
                    dead.base_url, dead.model, why, nxt.base_url, nxt.model)
        self.model = nxt.model
        self.base_url = nxt.base_url
        return True

    def chat(self, messages: list[dict], **kwargs) -> str:
        last_exc: Exception | None = None
        for i in range(self._start, len(self._chain)):
            client = self._chain[i]
            try:
                result = client.chat(messages, **kwargs)
                if i != self._start:
                    # This provider works and earlier ones are dead: promote it
                    # so subsequent calls skip the corpses.
                    log.warning(
                        "LLM fallback promoted: now using %s (%s) for the rest "
                        "of this run.", client.base_url, client.model,
                    )
                    self._start = i
                self.model = client.model
                self.base_url = client.base_url
                return result
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 — any provider failure falls through
                last_exc = exc
                nxt = self._chain[i + 1] if i + 1 < len(self._chain) else None
                if nxt is not None:
                    log.warning(
                        "LLM provider %s failed (%s: %.120s). Falling back to "
                        "%s (%s).", client.base_url, type(exc).__name__,
                        str(exc), nxt.base_url, nxt.model,
                    )
        raise RuntimeError(
            f"All LLM providers in the fallback chain failed. Last error: {last_exc}"
        ) from last_exc

    def ask(self, prompt: str, **kwargs) -> str:
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        for c in self._chain:
            c.close()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: FallbackLLM | None = None


def get_client() -> FallbackLLM:
    """Return (or create) the module-level client singleton (with fallbacks)."""
    global _instance
    if _instance is None:
        base_url, model, api_key = _detect_provider()
        fallbacks = [
            LLMClient(u, m, k) for u, m, k in _fallback_providers(base_url)
        ]
        chain_desc = " -> ".join(
            [f"{model}@{base_url}"] + [f"{c.model}@{c.base_url}" for c in fallbacks]
        )
        log.info("LLM provider chain: %s", chain_desc)
        _instance = FallbackLLM(LLMClient(base_url, model, api_key), fallbacks)
    return _instance
