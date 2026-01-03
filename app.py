from typing import AsyncGenerator
import logging
import re
from fastapi import Depends, FastAPI, HTTPException, Query, Header, Response, Request, status, UploadFile
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import urllib.parse
from models import genai
from models.genai import (
    Content,
    Part,
)
import uvicorn
import uuid
import os
import time
import json
import asyncio
import aiohttp
from contextlib import asynccontextmanager
from fastapi.responses import StreamingResponse
from browser import BrowserPool, InterceptTask
from models import _adapter as adapter, aistudio, genai
from config import config, AIOHTTP_PROXY, AIOHTTP_PROXY_AUTH
from utils import TinyProfiler, Profiler, CredentialManager
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from models import genai, openai as openai_models


credentialManager = CredentialManager(config.Credentials)
browser_pool = BrowserPool(credentialManager)
# In-memory storage for files
FILES = {}

from models.aistudio import StreamEvent, StreamParser, ResponseError


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await browser_pool.start()
    yield
    # Shutdown
    await browser_pool.stop()


app = FastAPI(lifespan=lifespan)

security = HTTPBearer()

async def verify_openai_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if config.AuthKey and credentials.credentials != config.AuthKey:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def api_key_auth(
    key: str | None = Query(None, description="API Key"),
    x_goog_api_key: str | None = Header(None, alias="x-goog-api-key"),
):
    if config.AuthKey:
        auth_key = key or x_goog_api_key
        if auth_key != config.AuthKey:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API Key",
            )

@app.get("/v1/models", dependencies=[Depends(verify_openai_token)])
async def list_models_openai() -> openai_models.ModelList:
    aistudio_models = browser_pool.get_Models()
    data = []
    for m in aistudio_models:
        # Extract model ID from name (e.g., "models/gemini-1.5-pro" -> "gemini-1.5-pro")
        model_id = m.name.split('/')[-1] if hasattr(m, 'name') else str(m)
        data.append(openai_models.ModelCard(id=model_id))
    
    return openai_models.ModelList(data=data)


@app.post("/v1/chat/completions", dependencies=[Depends(verify_openai_token)])
async def chat_completions(request: openai_models.ChatCompletionRequest):
    genai_request = adapter.OpenAIRequestToGenAIRequest(request)
    model_name = request.model
    
    request_id = adapter._randomPromptId()
    with Profiler(request_id) as profiler:
        profiler.span('openai: receive request', {'model': model_name})
        
        prompt_history = adapter.GenAIRequestToAiStudioPromptHistory(model_name, genai_request, prompt_id=request_id)
        
        future = asyncio.Future()
        task = InterceptTask(prompt_history, future, profiler)
        await browser_pool.put_task(task)
        
        try:
            headers, body = await future
        except TimeoutError:
            raise HTTPException(status_code=504, detail="Upstream Request Timeout")
        except Exception as e:
             raise HTTPException(status_code=500, detail=f"Worker Error: {str(e)}")

        # Callback để handle rate limit
        async def handle_rate_limit(model: str):
            if task._worker:
                await browser_pool.handle_rate_limit(task._worker, model)

        if request.stream:
            async def openai_stream():
                chat_id = f"chatcmpl-{uuid.uuid4()}"
                try:
                    # Truyền task.email vào đây
                    async for event in StreamGenerator(model_name, headers, body, profiler, on_rate_limit=handle_rate_limit, worker_email=task.email):
                        genai_resp = adapter.AiStudioStreamEventToGenAIResponse(event)
                        chunk = adapter.GenAIResponseToOpenAIChunk(genai_resp, model_name, chat_id)
                        if chunk.choices[0].delta.content or chunk.choices[0].finish_reason:
                            yield f"data: {chunk.model_dump_json(exclude_none=True)}\n\n"
                    yield "data: [DONE]\n\n"
                except ResponseError as e:
                    if e.is_rate_limit():
                        error_data = {"error": {"message": str(e), "type": "rate_limit_error", "code": 429}}
                        yield f"data: {json.dumps(error_data)}\n\n"
                    else:
                        raise
            
            return StreamingResponse(openai_stream(), media_type="text/event-stream")
        else:
            full_content = ""
            finish_reason = "stop"
            
            try:
                # Truyền task.email vào đây
                async for event in StreamGenerator(model_name, headers, body, profiler, on_rate_limit=handle_rate_limit, worker_email=task.email):
                    genai_resp = adapter.AiStudioStreamEventToGenAIResponse(event)
                    if genai_resp.candidates:
                        cand = genai_resp.candidates[0]
                        if cand.content and cand.content.parts:
                            for part in cand.content.parts:
                                if not getattr(part, 'thought', False) and part.text:
                                    full_content += part.text
                        if cand.finishReason:
                            finish_reason = str(cand.finishReason).lower()
            except ResponseError as e:
                if e.is_rate_limit():
                    raise HTTPException(status_code=429, detail=str(e))
                raise HTTPException(status_code=500, detail=str(e))
            
            resp = openai_models.ChatCompletionResponse(
                id=f"chatcmpl-{uuid.uuid4()}",
                created=int(time.time()),
                model=model_name,
                choices=[
                    openai_models.Choice(
                        index=0,
                        message=openai_models.ChatMessage(role="assistant", content=full_content),
                        finish_reason=finish_reason
                    )
                ],
                usage=openai_models.Usage()
            )
            return JSONResponse(content=resp.model_dump(exclude_none=True))


async def StreamGenerator(model_name: str, headers: dict[str, str], body: str, profiler: Profiler, on_rate_limit: callable = None, worker_email: str = None) -> AsyncGenerator[StreamEvent, None]:
    url = config.AIStudioAPIUrl
    timeout = aiohttp.ClientTimeout(total=config.AioHTTPTimeout, connect=None, sock_connect=None, sock_read=None)
    async with aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(ssl=False if AIOHTTP_PROXY else True)) as session:

        # Thêm [email] vào log span
        profiler.span(f'[{worker_email}] aiohtpp: send request to aistudio', body)

        resp = await session.post(
            url, headers=headers, data=body,
            proxy=AIOHTTP_PROXY, proxy_auth=AIOHTTP_PROXY_AUTH,
        )

        chunks: list[bytes] = []

        async def inner():
            idx = 0
            async for chunk in resp.content.iter_any():
                chunks.append(chunk)
                # Thêm [email] vào log span của từng chunk
                profiler.span(f'[{worker_email}] aiohttp: chunk {idx}', chunk.decode())
                yield chunk
                idx += 1
        
        profiler.span(f'[{worker_email}] aiohttp: start stream response from aistudio')
        with profiler:
            try:
                async for event in StreamParser(inner()):
                    yield event
            except ResponseError as e:
                if e.is_rate_limit():
                    profiler.span(f'[{worker_email}] Rate limit detected, triggering rotation')
                    if on_rate_limit:
                        # Gọi hàm này để trigger việc đổi worker ở background
                        # Lưu ý: handle_rate_limit trong browser.py giờ đã chạy rất nhanh (không chờ browser bật)
                        await on_rate_limit(model_name)
                # Luôn raise lỗi để thoát khỏi hàm này và trả về response cho client
                raise
            profiler.span(f'[{worker_email}] aiohttp: finish stream response from aistudio')


@app.post("/v1beta/models/{model}:generateContent", dependencies=[Depends(api_key_auth)])
async def generate_content(model: str, request_body: genai.GenerateContentRequest, request: Request) -> Response:
    request_id = adapter._randomPromptId()
    with Profiler(request_id) as profiler:
        profiler.span('fastapi: receive request', {'model': model, 'request': request_body.model_dump()})

        prompt_history = adapter.GenAIRequestToAiStudioPromptHistory(model, request_body, prompt_id=request_id)
        profiler.span('adapter: GenAIRequestToAiStudioPromptHistory')
        future = asyncio.Future()
        task = InterceptTask(prompt_history, future, profiler)
        await browser_pool.put_task(task)
        profiler.span('fastapi: task scheduled')
        
        try:
            headers, body = await future
        except TimeoutError:
            raise HTTPException(status_code=504, detail="Upstream Request Timeout (Worker timed out)")
        except asyncio.CancelledError:
            raise HTTPException(status_code=499, detail="Client Closed Request")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Worker Error: {str(e)}")

        # Callback để handle rate limit
        async def handle_rate_limit(model_name: str):
            if hasattr(task, '_worker') and task._worker:
                await browser_pool.handle_rate_limit(task._worker, model_name)

        try:
            # Truyền task.email vào đây
            events = [event async for event in StreamGenerator(model, headers, body, profiler, on_rate_limit=handle_rate_limit, worker_email=task.email)]
        except ResponseError as e:
            if e.is_rate_limit():
                raise HTTPException(status_code=429, detail=str(e))
            raise HTTPException(status_code=500, detail=str(e))

        response = adapter.AiStudioStreamEventToGenAIResponse(events)
        if response.candidates:
            # Gộp các text parts liền kề cùng loại (thought/text) để đảm bảo Markdown không bị vỡ
            for candidate in response.candidates:
                if candidate.content and candidate.content.parts:
                    new_parts = []
                    for part in candidate.content.parts:
                        if not part.text:
                            continue
                        
                        # Kiểm tra thuộc tính thought (nếu có), mặc định là False
                        is_thought = getattr(part, 'thought', False)
                        
                        # Nếu part hiện tại cùng loại (thought/không thought) với part trước đó thì gộp
                        if new_parts and getattr(new_parts[-1], 'thought', False) == is_thought:
                            new_parts[-1].text += part.text
                        else:
                            new_parts.append(part)
                    
                    candidate.content.parts = new_parts

            response.candidates[-1].finishReason = genai.FinishReason.STOP

        return JSONResponse(content=response.model_dump(exclude_none=True))


@app.post("/v1beta/models/{model}:streamGenerateContent", dependencies=[Depends(api_key_auth)])
async def stream_generate_content(model: str, request_body: genai.GenerateContentRequest, request: Request) -> Response:
    request_id = adapter._randomPromptId()
    with Profiler(request_id) as profiler:
        profiler.span('fastapi: receive request', {'model': model, 'request': request_body.model_dump()})

        prompt_history = adapter.GenAIRequestToAiStudioPromptHistory(model, request_body)
        profiler.span('adapter: GenAIRequestToAiStudioPromptHistory')
        future = asyncio.Future()
        task = InterceptTask(prompt_history, future, profiler)
        await browser_pool.put_task(task)
        profiler.span('fastapi: task scheduled')
        
        try:
            headers, body = await future
        except TimeoutError:
            raise HTTPException(status_code=504, detail="Upstream Request Timeout (Worker timed out)")
        except asyncio.CancelledError:
            raise HTTPException(status_code=499, detail="Client Closed Request")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Worker Error: {str(e)}")

        # Callback để handle rate limit  
        async def handle_rate_limit(model_name: str):
            if hasattr(task, '_worker') and task._worker:
                # Gọi vào pool để xoay vòng worker
                await browser_pool.handle_rate_limit(task._worker, model_name)

        async def response_generator():
            try:
                async for event in StreamGenerator(model, headers, body, profiler, on_rate_limit=handle_rate_limit, worker_email=task.email):
                    response_chunk = adapter.AiStudioStreamEventToGenAIResponse(event)
                    data = response_chunk.model_dump_json(exclude_none=True)
                    logging.debug('yield event %r', data)
                    yield f"data: {data}\n\n"
            except ResponseError as e:
                if e.is_rate_limit():
                    # Trả về lỗi 429 NGAY LẬP TỨC cho client
                    error_response = {
                        "error": {
                            "code": 429, 
                            "message": "Rate limit exceeded. Server is rotating credentials. Please retry in a few seconds.",
                            "status": "RESOURCE_EXHAUSTED"
                        }
                    }
                    yield f"data: {json.dumps(error_response)}\n\n"
                else:
                    raise

        return StreamingResponse(response_generator(), media_type="text/event-stream")


async def forward_request(request: Request) -> Response:
    api_key = credentialManager.api_key

    path = request.url.path
    target_url = f"https://generativelanguage.googleapis.com{path}"

    body = await request.body()

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("content-length", "host")
    }
    headers.update({
        "x-goog-api-key": api_key,
    })

    timeout = aiohttp.ClientTimeout(total=config.AioHTTPTimeout, connect=None, sock_connect=None, sock_read=None)
    
    session = aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(ssl=False if AIOHTTP_PROXY else True))

    try:
        method = request.method
        upstream_response = await session.request(
            method,
            target_url,
            data=body,
            headers=headers,
            proxy=AIOHTTP_PROXY, proxy_auth=AIOHTTP_PROXY_AUTH
        )
    except aiohttp.ClientError as e:
        await session.close()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Error contacting upstream service: {e}")

    async def content_iterator():
        try:
            async for chunk in upstream_response.content.iter_any():
                yield chunk
        finally:
            await upstream_response.release()
            await session.close()

    response_headers = {
        k: v for k, v in upstream_response.headers.items()
        if k.lower() not in ('content-encoding', 'transfer-encoding', 'connection')
    }

    return StreamingResponse(
        content=content_iterator(),
        status_code=upstream_response.status,
        media_type=upstream_response.content_type,
        headers=response_headers,
    )


@app.post("/v1beta/models/{model}:countTokens", dependencies=[Depends(api_key_auth)])
async def count_tokens(model: str, request: Request):
    """
    Forwards the request to Google's countTokens API and streams the response back.
    """
    return await forward_request(request)


@app.get('/v1beta/models', dependencies=[Depends(api_key_auth)])
async def ListModel() -> genai.ListModelsResponse:
    aistudio_models_list = browser_pool.get_Models()
    if not aistudio_models_list:
        return genai.ListModelsResponse(models=[])

    aistudio_response = aistudio.ListModelsResponse(models=aistudio_models_list)
    
    return adapter.AIStudioListModelToGenAIListModel(aistudio_response)


# TODO: 支持 Gemini API 文件上传接口


@app.post("/upload/v1beta/files", response_model=genai.FileResponse, dependencies=[Depends(api_key_auth)])
async def upload_file(request: genai.UploadFileRequest):
    """
    Starts a file upload.
    """
    file_id = str(uuid.uuid4())
    file_name = f"files/{file_id}"
    file = request.file
    file.name = file_name
    file.state = "PROCESSING"
    file.uri = f"https://generativelanguage.googleapis.com/v1beta/{file_name}"
    FILES[file_name] = file
    upload_url = f"/upload/{file_id}"

    return JSONResponse(
        content=genai.FileResponse(file=file).model_dump(),
        headers={"x-goog-upload-url": upload_url}
    )


@app.get("/v1beta/files/{name}", response_model=genai.FileResponse, dependencies=[Depends(api_key_auth)])
async def get_file(name: str):
    """
    Gets file information.
    """
    file_info = FILES.get(f"files/{name}")
    if file_info:
        if file_info.state == "PROCESSING":
            # Simulate processing time
            time.sleep(1)
            file_info.state = "ACTIVE"
        return genai.FileResponse(file=file_info)
    return Response(status_code=404)


@app.put("/upload/{upload_id}")
async def upload_file_chunk(upload_id: str, request: Request):
    """
    Uploads file content.
    """
    file_name = f"files/{upload_id}"
    if file_name in FILES:
        # In a real implementation, you would process the uploaded bytes.
        # Here we just finalize the upload.
        FILES[file_name].state = "ACTIVE"
        return genai.FileResponse(file=FILES[file_name])
    return Response(status_code=404)


# 管理接口

regex = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\.json$')

@app.post("/admin/upload_state", dependencies=[Depends(api_key_auth)])
async def upload_state(state: UploadFile,  request: Request):
    filename = urllib.parse.unquote(state.filename)
    assert filename is not None
    print(filename)
    if not regex.match(filename):
        return Response(status_code=400)
    content = await state.read()
    # TODO: validate state
    logging.info('save state %s', filename)
    with open(f'{config.StatesDir}/{filename}', 'wb') as f:
        f.write(content)
    return Response(status_code=200)


if __name__ == "__main__":
    uvicorn.run(app, host=config.UvicornHost, port=config.UvicornPort)
