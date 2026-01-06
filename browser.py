import hashlib
import logging
from typing import Any, AsyncGenerator, TypedDict
import time
import re
import os
import typing
import json
import asyncio
import traceback
import random
import string
import aiohttp
import contextlib
import pathlib
from models.aistudio import PromptHistory, flatten, inflate, StreamParser, Model, ListModelsResponse, ResponseError
from datetime import datetime, timezone
from camoufox.async_api import AsyncCamoufox, AsyncNewBrowser
from playwright.async_api import Route, expect, async_playwright, BrowserContext, Browser, Page
import dataclasses
from config import config, AIOHTTP_PROXY, AIOHTTP_PROXY_AUTH, CAMOUFOX_PROXY
from utils import Profiler, CredentialManager, Credential


logger = logging.getLogger('Browser')


def sapisidhash(cookies: dict[str, str]) -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    sapisid: str = cookies.get(
        '__Secure-3PAPISID',
        cookies.get('SAPISID', cookies.get('__Secure-1PAPISID'))) or ''
    assert sapisid
    m = hashlib.sha1(' '.join(
        (str(now), sapisid, "https://aistudio.google.com")).encode())
    sapisidhash = '_'.join((str(now), m.hexdigest()))
    return ' '.join(
        f'{key} {sapisidhash}' for key in ('SAPISIDHASH', 'SAPISID1PHASH', 'SAPISID3PHASH'))



fixed_responsed = [
    [
        [
            [[[[[None,"**Thinking**\n\n不，你不想。\n\n\n",None,None,None,None,None,None,None,None,None,None,1]],"model"]]],
            None,[6,None,74,None,[[1,6]],None,None,None,None,68]],
        [
            [[[[[None,"请求已转移到Python中。"]],"model"],1]],
            None,[6,9,215,None,[[1,6]],None,None,None,None,200]],
        [
            None,None,None,["1749019849541811",109021497,4162363067]]
    ]
]


@dataclasses.dataclass
class InterceptTask:
    prompt_history: PromptHistory
    future: asyncio.Future
    profiler: Profiler
    email: str | None = None  # Thêm trường này để lưu email worker
    _worker: 'BrowserWorker | None' = None # (Nếu bạn đã thêm từ bước trước)



class AccountInfo(TypedDict):
    email: str | None
    apiKey: str | None


class BrowserPool:
    queue: asyncio.Queue[InterceptTask]
    _workers: dict[asyncio.Task, 'BrowserWorker']
    _next_cred_idx: int # Index để theo dõi credential tiếp theo

    def __init__(self, credentialManager: CredentialManager, worker_count: int = config.WorkerCount, *, endpoint: str | None = None, loop=None):
        self._loop = loop
        self._endpoint = endpoint
        self.queue = asyncio.Queue()
        self._credMgr = credentialManager
        self._worker_count = min(worker_count, len(self._credMgr.credentials))
        self._workers = {}
        self._models: list[Model] = []
        self._worker_lock = asyncio.Lock()
        self._next_cred_idx = 0 # Bắt đầu từ 0

    def set_Models(self, models: list[Model]):
        self._models = models

    def get_Models(self) -> list[Model]:
        #TODO: model list override
        return self._models

    def worker_done_callback(self, task: asyncio.Task) -> None:
        if exc := task.exception():
            logger.error('worker %s failed with exception', task.get_name(), exc_info=exc)
        else:
            logger.info('worker %s exit normally', task.get_name())
        if worker := self._workers.pop(task, None):
            asyncio.create_task(worker.stop())
            if not worker.ready.done():
                worker.ready.set_result(None)

    async def start(self):
        # Khởi động số lượng worker ban đầu
        for i in range(self._worker_count):
            await self._spawn_worker()
            # sleep 10s。防止一次启动太多浏览器
            await asyncio.sleep(10)

        logger.info('waiting for workers to be ready')
        await asyncio.gather(*[
            worker.ready
            for task, worker in self._workers.items() if not task.done()
        ], return_exceptions=True)
        for task, worker in self._workers.items():
            account_info = await worker.ready
            if not account_info:
                continue
            if not worker._credential.email and account_info['email']:
                worker._credential.email = account_info['email']
            if not worker._credential.apikey and account_info['apiKey']:
                worker._credential.apikey = account_info['apiKey']

        if len(self._workers) <= 0:
            raise BaseException('No Worker Available')
        logger.info('%d Workers Up and Running', len(self._workers))
        return self

    async def _spawn_worker(self):
        """Hàm nội bộ để spawn worker tiếp theo theo vòng tròn"""
        if not self._credMgr.credentials:
            return

        # Lấy credential theo vòng tròn
        cred = self._credMgr.credentials[self._next_cred_idx]
        # Cập nhật index cho lần sau (Round Robin)
        self._next_cred_idx = (self._next_cred_idx + 1) % len(self._credMgr.credentials)

        worker = BrowserWorker(
            credential=cred,
            pool=self,
            endpoint=self._endpoint,
            loop=self._loop
        )
        logger.info('Spawning worker with %s (Round Robin)', cred.email)
        task = asyncio.create_task(worker.run(), name=f"BrowserWorker-{cred.email}")
        task.add_done_callback(self.worker_done_callback)
        self._workers[task] = worker
        return worker

    async def stop(self):
        await asyncio.gather(*[
            worker.stop() for worker in self._workers.values()], return_exceptions=True)

    async def put_task(self, task: InterceptTask):
        await self.queue.put(task)

    async def handle_rate_limit(self, worker: 'BrowserWorker', model: str):
        """
        Xử lý rate limit: 
        1. Tắt worker bị lỗi ngay lập tức.
        2. Mở worker mới ở background (không chờ đợi).
        """
        async with self._worker_lock:
            # Tìm task của worker này
            worker_task = None
            for task, w in self._workers.items():
                if w is worker:
                    worker_task = task
                    break
            
            if worker_task:
                logger.warning(f'Rate limit hit for {worker._credential.email}. Rotating to next credential.')
                
                # 1. Dừng worker hiện tại
                # Xóa khỏi danh sách quản lý trước để không nhận task mới
                del self._workers[worker_task]
                
                # Chạy việc stop ở background để không block request hiện tại
                asyncio.create_task(worker.stop())
                worker_task.cancel()
            
            # 2. Spawn worker mới (Fire and forget - không await ready)
            # Việc này giúp trả về 429 cho client ngay lập tức mà không cần chờ trình duyệt mới bật lên
            asyncio.create_task(self._spawn_worker_and_wait())

    async def _spawn_worker_and_wait(self):
        """Helper để spawn và log kết quả, chạy ở background"""
        try:
            worker = await self._spawn_worker()
            if worker:
                await worker.ready
                logger.info(f'New rotated worker {worker._credential.email} is READY')
        except Exception as e:
            logger.error(f"Failed to spawn rotated worker: {e}")

    def get_available_worker_count(self) -> int:
        return len(self._workers)

class BrowserWorker:
    _browser: BrowserContext | None
    _credential: Credential
    _endpoint: str | None
    _pool: BrowserPool
    status: str
    ready: asyncio.Future

    def __init__(self, credential: Credential, pool: BrowserPool, *, endpoint: str|None = None, loop=None):
        self._loop = loop
        self._credential = credential
        self._browser = None
        self._endpoint = endpoint
        self._pages = []
        self._pool = pool
        self.ready = asyncio.Future()

    async def browser(self) -> BrowserContext:
        if not self._browser:
            if not self._endpoint:
                _browser = typing.cast(Browser, await AsyncCamoufox(
                    headless=config.Headless,
                    main_world_eval=True,
                    enable_cache=True,
                    locale="US",
                    proxy=CAMOUFOX_PROXY,
                    geoip=True if CAMOUFOX_PROXY else False,
                ).__aenter__())
            else:
                _browser = await (await async_playwright().__aenter__()).firefox.connect(self._endpoint)
            if _browser.contexts:
                context = _browser.contexts[0]
            else:
                storage_state = None

                if self._credential.stateFile and os.path.exists(f'{config.StatesDir}/{self._credential.stateFile}'):
                    storage_state = f'{config.StatesDir}/{self._credential.stateFile}'
                context = await _browser.new_context(
                    storage_state=storage_state,
                    ignore_https_errors=True,
                    locale="US",
                )
            self._browser = context
            self._pages = list(context.pages)
        return self._browser

    async def handle_ListModels(self, route: Route) -> None:
        if not self._pool.get_Models():
            async with aiohttp.ClientSession() as session:

                resp = await session.post(
                    route.request.url,
                    headers=route.request.headers,
                    data=route.request.post_data,
                    proxy=AIOHTTP_PROXY, proxy_auth=AIOHTTP_PROXY_AUTH,
                    ssl=False if AIOHTTP_PROXY else True
                )

                data = inflate(await resp.json(), ListModelsResponse)
                if data:
                    self._pool.set_Models(data.models)
        # TODO: 从缓存快速返回请求
        # TODO: 劫持并修改模型列表
        await route.fallback()

    async def prepare_page(self, page: Page) -> None:
        await page.route("**/www.google-analytics.com/*", lambda route: route.abort())
        await page.route("**/play.google.com/*", lambda route: route.abort())
        await page.route("**/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/ListModels", self.handle_ListModels)
        if not page.url.startswith(config.AIStudioUrl):
            await page.goto(f'{config.AIStudioUrl}/prompts/new_chat')
            await page.evaluate("""()=>{mw:localStorage.setItem("aiStudioUserPreference", '{"isAdvancedOpen":false,"isSafetySettingsOpen":false,"areToolsOpen":true,"autosaveEnabled":false,"hasShownDrivePermissionDialog":true,"hasShownAutosaveOnDialog":true,"enterKeyBehavior":0,"theme":"system","bidiOutputFormat":3,"isSystemInstructionsOpen":true,"warmWelcomeDisplayed":true,"getCodeLanguage":"Python","getCodeHistoryToggle":true,"fileCopyrightAcknowledged":false,"enableSearchAsATool":true,"selectedSystemInstructionsConfigName":null,"thinkingBudgetsByModel":{},"rawModeEnabled":false,"monacoEditorTextWrap":false,"monacoEditorFontLigatures":true,"monacoEditorMinimap":false,"monacoEditorFolding":false,"monacoEditorLineNumbers":true,"monacoEditorStickyScrollEnabled":true,"monacoEditorGuidesIndentation":true}')}""")

    async def validate_state(self) -> AccountInfo | None:
        async with self.page() as page:
            logger.info('start validate state')
            await page.goto(f'{config.AIStudioUrl}/prompts/new_chat')
            if page.url.startswith(config.AIStudioUrl):
                return await self.fetch_account_info(page)
            elif not page.url.startswith('https://accounts.google.com/'):
                raise BaseException(f"Page at unexcpected URL: {page.url}")

            # 没登录
            if not self._credential.email or not self._credential.password:
                return None

            logger.info('login using credential %s', self._credential.email)
            await page.locator('input#identifierId').type(self._credential.email)
            await expect(page.locator('#identifierNext button')).to_be_enabled()
            await page.locator('#identifierNext button').click()
            await asyncio.sleep(3)
            await expect(page.locator('input[name="Passwd"]')).to_be_editable()
            logger.info('login using credential %s type in password', self._credential.email)
            await page.locator('input[name="Passwd"]').type(self._credential.password)
            await expect(page.locator('#passwordNext button')).to_be_enabled()
            await page.locator('#passwordNext button').click()
            await page.wait_for_url(f'{config.AIStudioUrl}/prompts/new_chat')
            if await page.locator('mat-dialog-content .welcome-option button[aria-label="Try Gemini"]').count() > 0:
                await page.locator('mat-dialog-content .welcome-option button[aria-label="Try Gemini"]').click()
            await (await self.browser()).storage_state(path=f'{config.StatesDir}/{self._credential.stateFile}')
            logger.info('store stete for credential %s', self._credential.email)
            return await self.fetch_account_info(page)

    async def fetch_account_info(self, page: Page) -> AccountInfo:
        rtn: AccountInfo = {
            'email': None,
            'apiKey': None
        }
        async def handle_apikeys(route: Route):
            await route.continue_()
            if not route.request.url.endswith('ListCloudApiKeys'):
                return

            if not (resp := await route.request.response()):
                return

            data = json.loads(await resp.body())

            rtn['apiKey'] = data[0][0][2]

        await page.route("**/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/*", handle_apikeys)
        await page.goto('https://aistudio.google.com/apikey')
        email_regex = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')
        # global_data = await page.evaluate('mw:window.WIZ_global_data')
        global_data = json.loads(await page.locator('script[type="application/json"]').text_content())
        for _, value in global_data.items():
            if not isinstance(value, str):
                continue
            if email_regex.match(value):
                rtn['email'] = value
        await page.unroute("**/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/*", handle_apikeys)
        return rtn

    @contextlib.asynccontextmanager
    async def page(self):
        if not self._pages:
            page = await (await self.browser()).new_page()
        else:
            page = self._pages.pop(0)
        await self.prepare_page(page)
        try:
            yield page
        finally:
            self._pages.append(page)
            await page.unroute_all()

    async def stop(self):
        if self._browser:
            await self._browser.close()
            self._browser = None

    async def run(self):
        if not (accountInfo := await self.validate_state()):
            logger.info('State is not valid for credential %s', self._credential.email)
            await self.stop()
            return
        logger.info('Worker %s is ready', self._credential.email)
        self.ready.set_result(accountInfo)
        
        while True:
            try:
                # Lấy task từ queue
                task = await self._pool.queue.get()
                
                # Kiểm tra xem task đã bị cancel (do timeout ở app.py) chưa
                if task.future.done():
                    logger.warning(f"Worker {self._credential.email} picked up a cancelled task, skipping.")
                    continue

                task.email = self._credential.email
                if hasattr(task, '_worker'): task._worker = self

                task.profiler.span(f'worker: task fetched by {self._credential.email}')
                
                # Thêm timeout cứng cho việc xử lý 1 request trong worker
                # Nếu quá 120s mà InterceptRequest chưa xong -> Kill worker này để tránh treo
                try:
                    async with asyncio.timeout(120):
                        await self.InterceptRequest(task.prompt_history, task.future, task.profiler)
                except asyncio.TimeoutError:
                    logger.error(f"Worker {self._credential.email} HUNG processing request. Restarting worker.")
                    if not task.future.done():
                        task.future.set_exception(TimeoutError("Worker processing hung"))
                    
                    # Thoát vòng lặp để worker này kết thúc, trigger worker_done_callback -> spawn worker mới
                    break 

            except Exception as exc:
                task.profiler.span('worker: failed with exception', traceback.format_exception(exc))
                if not task.future.done():
                    task.future.set_exception(exc)
                logger.error('Worker %s error processing task: %s', self._credential.email, exc)
                # Nếu lỗi quá nghiêm trọng (ví dụ mất kết nối browser), cũng nên break để restart
                if "Target closed" in str(exc) or "Connection closed" in str(exc):
                    break

    async def InterceptRequest(self, prompt_history: PromptHistory, future: asyncio.Future, profiler: Profiler, timeout: int=120):
        prompt_id = prompt_history.prompt.uri.split('/')[-1]

        async def handle_route(route: Route) -> None:
            match route.request.url.split('/')[-1]:
                case 'GenerateContent':
                    profiler.span('Route: Intercept GenerateContent')
                    future.set_result((route.request.headers, route.request.post_data))
                    await route.fulfill(
                        content_type='application/json+protobuf; charset=UTF-8',
                        body=json.dumps(fixed_responsed, separators=(',', ':'))
                    )
                case 'ResolveDriveResource':
                    profiler.span('Route: serve PromptHistory')
                    data = json.dumps(flatten(prompt_history), separators=(',', ':'))
                    await route.fulfill(
                        content_type='application/json+protobuf; charset=UTF-8',
                        body=data,
                    )
                case 'GenerateAccessToken':
                    # 阻止保存Prompt至历史
                    await route.fulfill(
                        content_type='application/json+protobuf; charset=UTF-8',
                        body='[16,"Request is missing required authentication credential. Expected OAuth 2 access token, login cookie or other valid authentication credential. Seehttps://developers.google.com/identity/sign-in/web/devconsole-project."]',
                        status=401,
                    )
                case 'CreatePrompt':
                    await route.abort()
                case 'UpdatePrompt':
                    await route.abort()
                case 'ListPrompts':
                    profiler.span('Route: serve ListPrompts')
                    data = json.dumps(flatten([prompt_history.prompt.promptMetadata]), separators=(',', ':'))
                    await route.fulfill(
                        content_type='application/json+protobuf; charset=UTF-8',
                        body=data,
                    )
                case 'CountTokens':
                    await route.fulfill(
                        content_type='application/json+protobuf; charset=UTF-8',
                        body=json.dumps([4,[],[[[3],1]],None,None,[[1,4]]],  separators=(',', ':'))
                    )
                case _:
                    await route.fallback()

        async with asyncio.timeout(timeout):
            async with self.page() as page:
                profiler.span('Page: Created')
                await page.route("**/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/*", handle_route)
                
                # Thêm timeout cho goto
                try:
                    await page.goto(f'{config.AIStudioUrl}/prompts/{prompt_id}', timeout=30000) # 30s timeout load trang
                except Exception as e:
                    profiler.span(f'Page: Load Failed {str(e)}')
                    raise e

                profiler.span('Page: Loaded')
                last_turn = page.locator('ms-chat-turn').last
                # in ra xem last turn nội dung xem có phải là placeholder không
                logger.debug(f"Last Turn Content: {await last_turn.locator('ms-text-chunk').inner_text()}")
                await expect(last_turn.locator('ms-text-chunk')).to_have_text('(placeholder)', timeout=20000)
                profiler.span('Page: Placeholder Visible')
                
                # --- SỬA ĐOẠN NÀY ---
                # Xử lý Cookie Banner (nếu có)
                try:
                    cookie_reject = page.locator('.glue-cookie-notification-bar__reject')
                    if await cookie_reject.is_visible(timeout=1000):
                        await cookie_reject.click(timeout=2000)
                except Exception:
                    pass

                # Xử lý Settings Panel (Thủ phạm gây lỗi bên Native)
                # Dùng try-except và timeout ngắn. Nếu click thất bại, code sẽ bỏ qua và chạy tiếp xuống dưới
                # thay vì treo worker vĩnh viễn.
                try:
                    settings_btn = page.locator('button[aria-label="Close run settings panel"]')
                    if await settings_btn.is_visible(timeout=1500):
                        # Chỉ chờ click tối đa 2s
                        await settings_btn.click(force=True, timeout=2000)
                        profiler.span('Page: Settings Panel Closed')
                except Exception as e:
                    # Log warning nhưng KHÔNG raise lỗi để luồng chính tiếp tục
                    logger.warning(f"Worker {self._credential.email}: Could not close settings panel (Native UI), ignoring. Error: {e}")

                # XÓA ĐOẠN CODE CŨ GÂY LỖI NÀY:
                # if await page.locator('button[aria-label="Close run settings panel"]').is_visible():
                #     await page.locator('button[aria-label="Close run settings panel"]').click(force=True)
                # --- HẾT SỬA ---
                
                # Đảm bảo nút rerun hiện lên bằng cách hover
                await last_turn.hover()
                profiler.span('Page: Last Turn Hover')
                rerun = last_turn.locator('[name="rerun-button"]')
                await expect(rerun).to_be_visible()
                profiler.span('Page: Rerun Visible')

                # Cơ chế Retry: Click và chờ phản hồi, nếu không thấy thì click lại
                for i in range(5): # Thử tối đa 5 lần
                    if future.done():
                        break
                    
                    # Click nút Rerun
                    await rerun.click(force=True)
                    profiler.span(f'Page: Rerun Clicked ({i+1})')
                    
                    try:
                        # Chờ tối đa 2 giây để xem request có được chặn hay không
                        await asyncio.wait_for(asyncio.shield(future), timeout=2)
                        break # Nếu future xong thì thoát vòng lặp
                    except asyncio.TimeoutError:
                        if i < 4:
                            logger.warning(f"Click attempt {i+1} failed to trigger request, retrying...")
                            # Hover lại để đảm bảo nút vẫn visible/active
                            await last_turn.hover()
                
                # Chờ kết quả cuối cùng (nếu đã break ở trên thì cái này sẽ trả về ngay)
                await future
                await page.unroute("**/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/*")
