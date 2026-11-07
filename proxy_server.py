import os
import json
import logging
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("openai-proxy")

UPSTREAM_BASE_URL = os.getenv("UPSTREAM_BASE_URL", "https://api.reformboss.com/v1").rstrip("/")
DEFAULT_API_KEY = os.getenv("UPSTREAM_API_KEY", "")
TARGET_MODEL = os.getenv("TARGET_MODEL", "zai-org/GLM-5.3-Flash")
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "zai-org/GLM-5.3")
FORCE_DEFAULT_KEY = os.getenv("FORCE_DEFAULT_KEY", "false").lower() in ("true", "1", "yes")

app = FastAPI(
    title="OpenAI-compatible Gateway for GLM-5.3 Flash & Fallback",
    description="Proxy server with primary GLM-5.3 Flash and fallback to GLM-5.3."
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MODELS_LIST = [
    {
        "id": "glm-5.3-flash",
        "object": "model",
        "created": 1700000000,
        "owned_by": "zhipu",
    },
    {
        "id": "zai-org/GLM-5.3-Flash",
        "object": "model",
        "created": 1700000000,
        "owned_by": "zhipu",
    },
    {
        "id": "glm-5.3",
        "object": "model",
        "created": 1700000000,
        "owned_by": "zhipu",
    },
    {
        "id": "gpt-4o",
        "object": "model",
        "created": 1700000000,
        "owned_by": "alias",
    },
    {
        "id": "gpt-4",
        "object": "model",
        "created": 1700000000,
        "owned_by": "alias",
    },
    {
        "id": "claude-3-5-sonnet",
        "object": "model",
        "created": 1700000000,
        "owned_by": "alias",
    },
]


@app.get("/")
@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "primary_model": TARGET_MODEL,
        "fallback_model": FALLBACK_MODEL,
        "upstream": UPSTREAM_BASE_URL,
        "force_default_key": FORCE_DEFAULT_KEY
    }


@app.get("/models")
@app.get("/v1/models")
async def get_models():
    return {"object": "list", "data": MODELS_LIST}


@app.post("/chat/completions")
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": f"Invalid JSON payload: {e}", "type": "invalid_request_error"}}
        )

    # Авторизация
    auth_header = request.headers.get("Authorization", "").strip()
    if FORCE_DEFAULT_KEY or not auth_header:
        upstream_auth = f"Bearer {DEFAULT_API_KEY}"
    else:
        upstream_auth = auth_header

    headers = {
        "Authorization": upstream_auth,
        "Content-Type": "application/json",
        "Accept": "application/json" if not body.get("stream") else "text/event-stream",
    }

    upstream_url = f"{UPSTREAM_BASE_URL}/chat/completions"
    is_stream = bool(body.get("stream", False))

    # Модель для запроса
    requested = body.get("model", "")
    if requested in (TARGET_MODEL, FALLBACK_MODEL):
        model_to_use = requested
    else:
        model_to_use = TARGET_MODEL
    body["model"] = model_to_use

    client = httpx.AsyncClient(timeout=120.0)

    # 1. Потоковый режим (SSE)
    if is_stream:
        async def event_generator():
            try:
                async with client.stream(
                    "POST",
                    upstream_url,
                    json=body,
                    headers=headers
                ) as response:
                    # Если основная модель вернула ошибку или 429, пробуем фоллбэк
                    if (response.status_code >= 500 or response.status_code == 429) and body["model"] != FALLBACK_MODEL:
                        logger.warning(f"Upstream returned {response.status_code} for {body['model']}, falling back to {FALLBACK_MODEL}")
                        body["model"] = FALLBACK_MODEL
                        async with client.stream("POST", upstream_url, json=body, headers=headers) as fb_resp:
                            async for chunk in fb_resp.aiter_raw():
                                if chunk:
                                    yield chunk
                        return

                    if response.status_code != 200:
                        err_content = await response.aread()
                        yield f"data: {err_content.decode('utf-8', errors='ignore')}\n\n".encode("utf-8")
                        return

                    async for chunk in response.aiter_raw():
                        if chunk:
                            yield chunk
            except Exception as exc:
                logger.error(f"Streaming error: {exc}")
                err_payload = json.dumps({"error": {"message": str(exc), "type": "proxy_error"}})
                yield f"data: {err_payload}\n\n".encode("utf-8")
            finally:
                await client.aclose()

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )

    # 2. Непотоковый режим (JSON с поддержкой фоллбэка)
    try:
        response = await client.post(upstream_url, json=body, headers=headers)
        if (response.status_code >= 500 or response.status_code == 429) and body["model"] != FALLBACK_MODEL:
            logger.warning(f"Primary model {body['model']} returned {response.status_code}. Retrying with fallback {FALLBACK_MODEL}...")
            body["model"] = FALLBACK_MODEL
            response = await client.post(upstream_url, json=body, headers=headers)

        return Response(
            content=response.content,
            status_code=response.status_code,
            media_type="application/json"
        )
    except Exception as exc:
        # Пробуем фоллбэк при сетевом сбое
        if body["model"] != FALLBACK_MODEL:
            try:
                logger.warning(f"Network error with {body['model']}: {exc}. Retrying with fallback {FALLBACK_MODEL}...")
                body["model"] = FALLBACK_MODEL
                fb_resp = await client.post(upstream_url, json=body, headers=headers)
                return Response(content=fb_resp.content, status_code=fb_resp.status_code, media_type="application/json")
            except Exception as fb_exc:
                exc = fb_exc

        return JSONResponse(
            status_code=502,
            content={"error": {"message": f"Upstream error: {str(exc)}", "type": "upstream_error"}}
        )
    finally:
        await client.aclose()


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")
    logger.info(f"Starting OpenAI Proxy on {host}:{port} [Primary: {TARGET_MODEL}, Fallback: {FALLBACK_MODEL}]")
    uvicorn.run("proxy_server:app", host=host, port=port, reload=False)
