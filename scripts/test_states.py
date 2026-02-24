#!/usr/bin/env python3
"""
Script kiểm tra tính hợp lệ của các browser states.
Hỗ trợ kiểm tra đồng thời nhiều states nếu worker > 1 trong config.

Usage:
    python scripts/test_states.py [--workers N] [--headless] [--state STATE_FILE]
"""

import asyncio
import argparse
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# Add parent directory to path to import project modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from camoufox.async_api import AsyncCamoufox
from playwright.async_api import async_playwright, BrowserContext, Browser, Page
from config import config, CAMOUFOX_PROXY


logger = logging.getLogger('StateValidator')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


@dataclass
class StateValidationResult:
    """Kết quả kiểm tra một state file"""
    state_file: str
    is_valid: bool
    email: str | None = None
    api_key: str | None = None
    error: str | None = None
    redirect_url: str | None = None


class StateValidator:
    """Class để kiểm tra tính hợp lệ của state files"""
    
    def __init__(
        self, 
        states_dir: str = config.StatesDir,
        headless: bool | Literal['virtual'] = 'virtual',
    ):
        self.states_dir = states_dir
        self.headless = headless
        self.aistudio_url = config.AIStudioUrl
    
    async def validate_single_state(self, state_file: str) -> StateValidationResult:
        """
        Kiểm tra một state file có còn hợp lệ không.
        
        Returns:
            StateValidationResult với thông tin về state
        """
        state_path = f'{self.states_dir}/{state_file}'
        
        if not os.path.exists(state_path):
            return StateValidationResult(
                state_file=state_file,
                is_valid=False,
                error=f"State file not found: {state_path}"
            )
        
        browser: Browser | None = None
        context: BrowserContext | None = None
        
        try:
            logger.info(f'Validating state: {state_file}')
            
            # Khởi tạo browser với Camoufox
            browser = await AsyncCamoufox(
                headless=self.headless,
                main_world_eval=True,
                enable_cache=True,
                locale="US",
                proxy=CAMOUFOX_PROXY,
                geoip=True if CAMOUFOX_PROXY else False,
            ).__aenter__()
            
            # Tạo context với storage state
            context = await browser.new_context(
                storage_state=state_path,
                ignore_https_errors=True,
                locale="US",
            )
            
            page = await context.new_page()
            
            # Navigate đến AI Studio
            await page.goto(f'{self.aistudio_url}/prompts/new_chat', timeout=30000)
            
            current_url = page.url
            
            # Kiểm tra URL sau khi navigate
            if current_url.startswith(self.aistudio_url):
                # State valid - đang ở AI Studio
                email = await self._extract_email(page)
                
                return StateValidationResult(
                    state_file=state_file,
                    is_valid=True,
                    email=email,
                    redirect_url=current_url
                )
            
            elif current_url.startswith('https://accounts.google.com/'):
                # State không valid - bị redirect đến login page
                return StateValidationResult(
                    state_file=state_file,
                    is_valid=False,
                    error="Session expired - Redirected to Google login page",
                    redirect_url=current_url
                )
            
            else:
                # URL không xác định
                return StateValidationResult(
                    state_file=state_file,
                    is_valid=False,
                    error=f"Unexpected redirect URL",
                    redirect_url=current_url
                )
                
        except Exception as e:
            logger.error(f'Error validating {state_file}: {e}')
            return StateValidationResult(
                state_file=state_file,
                is_valid=False,
                error=str(e)
            )
        
        finally:
            if context:
                await context.close()
            if browser:
                await browser.close()
    
    async def _extract_email(self, page: Page) -> str | None:
        """Trích xuất email từ page nếu có thể"""
        import re
        import json
        
        try:
            # Thử lấy email từ /apikey page
            await page.goto('https://aistudio.google.com/apikey', timeout=15000)
            
            email_regex = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')
            
            # Lấy dữ liệu từ script JSON
            script_element = page.locator('script[type="application/json"]')
            if await script_element.count() > 0:
                global_data = json.loads(await script_element.first.text_content())
                for _, value in global_data.items():
                    if isinstance(value, str) and email_regex.match(value):
                        return value
        except Exception as e:
            logger.debug(f'Could not extract email: {e}')
        
        return None
    
    async def validate_states(
        self, 
        state_files: list[str] | None = None,
        max_workers: int = 1
    ) -> list[StateValidationResult]:
        """
        Kiểm tra nhiều state files với số worker được chỉ định.
        
        Args:
            state_files: Danh sách state files cần kiểm tra. None = tất cả trong states_dir
            max_workers: Số worker chạy đồng thời
            
        Returns:
            List các StateValidationResult
        """
        if state_files is None:
            # Lấy tất cả state files trong thư mục
            state_files = [
                f for f in os.listdir(self.states_dir) 
                if f.endswith('.json')
            ]
        
        if not state_files:
            logger.warning('No state files found to validate')
            return []
        
        logger.info(f'Validating {len(state_files)} states with {max_workers} worker(s)')
        
        if max_workers <= 1:
            # Chạy tuần tự
            results = []
            for state_file in state_files:
                result = await self.validate_single_state(state_file)
                results.append(result)
                self._print_result(result)
            return results
        else:
            # Chạy song song với semaphore để giới hạn concurrent workers
            semaphore = asyncio.Semaphore(max_workers)
            
            async def validate_with_semaphore(state_file: str) -> StateValidationResult:
                async with semaphore:
                    result = await self.validate_single_state(state_file)
                    self._print_result(result)
                    return result
            
            tasks = [validate_with_semaphore(sf) for sf in state_files]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Xử lý exceptions nếu có
            processed_results = []
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    processed_results.append(StateValidationResult(
                        state_file=state_files[i],
                        is_valid=False,
                        error=str(result)
                    ))
                else:
                    processed_results.append(result)
            
            return processed_results
    
    def _print_result(self, result: StateValidationResult):
        """In kết quả ra console"""
        status = "✅ VALID" if result.is_valid else "❌ INVALID"
        email_info = f" ({result.email})" if result.email else ""
        error_info = f" - {result.error}" if result.error else ""
        
        print(f"{status}: {result.state_file}{email_info}{error_info}")


async def main():
    parser = argparse.ArgumentParser(
        description='Validate browser states for AI Studio'
    )
    parser.add_argument(
        '--workers', '-w',
        type=int,
        default=config.WorkerCount,
        help=f'Number of concurrent workers (default: {config.WorkerCount} from config)'
    )
    parser.add_argument(
        '--headless',
        type=str,
        default=None,
        nargs='?',
        const='true',
        help=f'Headless mode: true/false/virtual (default: {config.Headless} from config)'
    )
    parser.add_argument(
        '--state', '-s',
        type=str,
        nargs='*',
        help='Specific state file(s) to validate (default: all states)'
    )
    parser.add_argument(
        '--states-dir',
        type=str,
        default=config.StatesDir,
        help=f'States directory (default: {config.StatesDir})'
    )
    parser.add_argument(
        '--output', '-o',
        type=str,
        help='Output file for results (optional)'
    )
    parser.add_argument(
        '--remove-invalid',
        action='store_true',
        help='Move invalid states to a separate directory'
    )
    
    args = parser.parse_args()
    
    # Xác định headless mode từ argument hoặc config
    if args.headless is None:
        # Sử dụng giá trị từ config
        headless_mode = config.Headless
    else:
        # Parse giá trị từ argument
        headless_str = args.headless.lower()
        if headless_str == 'true':
            headless_mode = True
        elif headless_str == 'false':
            headless_mode = False
        elif headless_str == 'virtual':
            headless_mode = 'virtual'
        else:
            headless_mode = config.Headless
    
    validator = StateValidator(
        states_dir=args.states_dir,
        headless=headless_mode
    )
    
    # Xác định danh sách states cần kiểm tra
    state_files = args.state if args.state else None
    
    print("=" * 60)
    print("🔍 AI Studio State Validator")
    print("=" * 60)
    print(f"States Directory: {args.states_dir}")
    print(f"Workers: {args.workers}")
    print(f"Headless: {headless_mode}")
    print("=" * 60)
    print()
    
    # Chạy validation
    results = await validator.validate_states(
        state_files=state_files,
        max_workers=args.workers
    )
    
    # Thống kê
    valid_count = sum(1 for r in results if r.is_valid)
    invalid_count = len(results) - valid_count
    
    print()
    print("=" * 60)
    print("📊 Summary")
    print("=" * 60)
    print(f"Total states:   {len(results)}")
    print(f"Valid:          {valid_count} ✅")
    print(f"Invalid:        {invalid_count} ❌")
    print("=" * 60)
    
    # Lưu kết quả nếu có output file
    if args.output:
        import json
        output_data = [
            {
                'state_file': r.state_file,
                'is_valid': r.is_valid,
                'email': r.email,
                'error': r.error,
                'redirect_url': r.redirect_url
            }
            for r in results
        ]
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False)
        print(f"\n📄 Results saved to: {args.output}")
    
    # Di chuyển invalid states nếu được yêu cầu
    if args.remove_invalid:
        invalid_dir = f"{args.states_dir}/../invalid_states"
        os.makedirs(invalid_dir, exist_ok=True)
        
        for result in results:
            if not result.is_valid:
                src = f"{args.states_dir}/{result.state_file}"
                dst = f"{invalid_dir}/{result.state_file}"
                if os.path.exists(src):
                    os.rename(src, dst)
                    print(f"📦 Moved: {result.state_file} -> invalid_states/")
    
    # In danh sách invalid states
    if invalid_count > 0:
        print("\n❌ Invalid States:")
        for result in results:
            if not result.is_valid:
                print(f"  - {result.state_file}: {result.error or 'Unknown error'}")
    
    # In danh sách valid states
    if valid_count > 0:
        print("\n✅ Valid States:")
        for result in results:
            if result.is_valid:
                email_info = f" ({result.email})" if result.email else ""
                print(f"  - {result.state_file}{email_info}")
    
    return 0 if invalid_count == 0 else 1


if __name__ == '__main__':
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
