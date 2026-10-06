#!/usr/bin/env python3
"""
WorldQuant BRAIN Forum Functions - Python Version
Comprehensive forum functionality including glossary, search, and post viewing using Playwright.
"""

import asyncio
import base64
import re
import sys
import time
from datetime import datetime
from typing import Dict, Any, List, Optional

def log(message: str, level: str = "INFO"):
    """Log message with timestamp."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {message}", file=sys.stderr)

try:
    from playwright.async_api import async_playwright
except ImportError:
    log("Playwright not installed. Please install it with: pip install playwright", "ERROR")
    async_playwright = None

from bs4 import BeautifulSoup
import requests
import os
from urllib.parse import parse_qs, quote, urlparse


_BROWSER_PATH_CACHE = None
_BROWSER_PATH_CHECKED = False


def _html_to_text(html: str) -> str:
    """Convert forum HTML fragments to readable plain text."""
    if not html:
        return ""
    soup = BeautifulSoup(html, 'html.parser')
    text = soup.get_text(separator='\n')
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return '\n'.join(lines)


def _unwrap_search_click_url(url: str) -> str:
    """Return the real target URL behind a Zendesk search click-tracking link.

    Search result titles link to ``/hc/<locale>/search/click?data=<b64>--<sig>``,
    where ``data`` is a base64 Ruby Marshal hash holding the post's actual URL.
    Anything that isn't such a link is returned unchanged.
    """
    if not isinstance(url, str) or '/search/click' not in url:
        return url
    try:
        data = parse_qs(urlparse(url).query).get('data', [''])[0].split('--', 1)[0]
        raw = base64.b64decode(data + '=' * (-len(data) % 4))
        match = re.search(rb'https?://[\x21-\x7e]+', raw)
        if match:
            return match.group().decode('ascii')
    except Exception:
        pass
    return url


def _extract_post_id(post_url_or_id: str) -> str:
    """Extract numeric forum post id from a raw id, slug, or full URL."""
    if not post_url_or_id:
        raise ValueError("Forum post id is required")
    post_url_or_id = _unwrap_search_click_url(post_url_or_id)
    match = re.search(r'/posts/(\d+)|^(\d+)', str(post_url_or_id))
    post_id = next((group for group in match.groups() if group), None) if match else None
    if not post_id:
        raise ValueError(f"Could not extract forum post id from: {post_url_or_id}")
    return post_id


def _extract_locale(post_url_or_id: str, default: str = "zh-cn") -> str:
    """Extract forum locale from URL when present."""
    if isinstance(post_url_or_id, str):
        post_url_or_id = _unwrap_search_click_url(post_url_or_id)
        match = re.search(r'/hc/([^/]+)/community/posts/', post_url_or_id)
        if match:
            return match.group(1)
    return default

# 导入浏览器设置模块
def get_browser_path():
    """获取可用的浏览器路径"""
    global _BROWSER_PATH_CACHE, _BROWSER_PATH_CHECKED
    if _BROWSER_PATH_CHECKED:
        return _BROWSER_PATH_CACHE
    try:
        # 尝试直接导入
        import browser_setup
        _BROWSER_PATH_CACHE = browser_setup.ensure_browser_available()
        _BROWSER_PATH_CHECKED = True
        return _BROWSER_PATH_CACHE
    except ImportError:
        # 如果直接导入失败，尝试从当前目录导入
        try:
            from pathlib import Path
            current_dir = Path(__file__).parent
            browser_setup_path = current_dir / "browser_setup.py"
            if browser_setup_path.exists():
                import sys
                sys.path.insert(0, str(current_dir))
                import browser_setup
                _BROWSER_PATH_CACHE = browser_setup.ensure_browser_available()
                _BROWSER_PATH_CHECKED = True
                return _BROWSER_PATH_CACHE
        except Exception:
            # Fallback: simple .env parser
            try:
                from pathlib import Path as _Path
                candidate = _Path(__file__).parent / ".env"
                if candidate.exists():
                    for _line in candidate.read_text().splitlines():
                        _line = _line.strip()
                        if not _line or _line.startswith('#') or '=' not in _line:
                            continue
                        _k, _v = _line.split('=', 1)
                        _k = _k.strip()
                        _v = _v.strip().strip('"').strip("'")
                        os.environ.setdefault(_k, _v)
            except Exception:
                pass
        
        # 如果都失败了，返回None使用默认设置
        log("未找到browser_setup模块，将使用默认Playwright浏览器", "WARNING")
        _BROWSER_PATH_CHECKED = True
        _BROWSER_PATH_CACHE = None
        return None

# --- Parsing Helper Functions (from playwright_forum_test.py) ---

def _is_navigation_or_metadata(line: str) -> bool:
    """Check if a line is navigation or metadata."""
    navigation_patterns = [
        r'^\d+ days? ago$',
        r'~\d+ minute read',
        r'^Follow',
        r'^Not yet followed',
        r'^Updated$',
        r'^AS\d+$',
        r'^[A-Z] - [A-Z] - [A-Z]',  # Letter navigation
        r'^A$',
        r'^B$',
        r'^[A-Z]$'  # Single letters
    ]
    return any(re.match(pattern, line.strip()) for pattern in navigation_patterns)

def _looks_like_term(line: str) -> bool:
    """Check if a line looks like a glossary term."""
    if len(line) > 100:
        return False
    if _is_navigation_or_metadata(line):
        return False
    definition_starters = ['the', 'a', 'an', 'this', 'that', 'it', 'is', 'are', 'was', 'were', 'for', 'to', 'in', 'on', 'at', 'by', 'with']
    first_word = line.lower().split(' ')[0] if line else ''
    if first_word and first_word in definition_starters:
        return False
    is_short = len(line) <= 80
    starts_with_capital = bool(re.match(r'^[A-Z]', line))
    has_all_caps = bool(re.match(r'^[A-Z\s\-\/\(\)]+$', line))
    has_reasonable_length = len(line) >= 2
    return is_short and has_reasonable_length and (starts_with_capital or has_all_caps)

def _parse_glossary_terms(content: str) -> List[Dict[str, str]]:
    """Parse glossary terms from HTML content."""
    soup = BeautifulSoup(content, 'html.parser')
    # Get text from the article body, which is more reliable than splitting the whole HTML
    article_body = soup.select_one('.article-body')
    if not article_body:
        return []
    
    # Use .get_text with a separator to preserve line breaks, which is key for the logic below
    lines = article_body.get_text(separator='\n').split('\n')
    
    terms = []
    current_term = None
    current_definition = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        if _looks_like_term(line):
            if current_term:
                # Save the previous term
                terms.append({
                    "term": current_term,
                    "definition": " ".join(current_definition).strip()
                })
            # Start a new term
            current_term = line
            current_definition = []
        elif current_term:
            # Add to the current definition
            current_definition.append(line)
            
    # Add the last term
    if current_term:
        terms.append({
            "term": current_term,
            "definition": " ".join(current_definition).strip()
        })
        
    # Filter out invalid terms and improve quality
    return [term for term in terms if 
            len(term["term"]) > 0 and 
            len(term["definition"]) > 10 and
            not _is_navigation_or_metadata(term["term"]) and
            "ago" not in term["definition"] and
            "minute read" not in term["definition"]]

def _brain_store():
    """The main client's permanent on-disk store, or None if unavailable.

    Imported lazily: forum_functions is imported *by* main, so a module-level
    import would be circular.
    """
    try:
        from main import brain_client
        return brain_client.store
    except Exception:
        return None


class ForumClient:
    """Forum client for WorldQuant BRAIN support site, using Playwright."""
    
    def __init__(self):
        # Load .env to allow overrides via environment
        try:
            from dotenv import load_dotenv, find_dotenv
            env_path = find_dotenv(usecwd=True)
            if env_path:
                load_dotenv(env_path, override=False)
            else:
                from pathlib import Path as _Path
                candidate = _Path(__file__).parent / ".env"
                if candidate.exists():
                    load_dotenv(candidate, override=False)
        except Exception:
            pass

        self.base_url = os.getenv("FORUM_SETTINGS_BASE_URL", "https://support.worldquantbrain.com")
        # timeouts: seconds in env -> milliseconds for playwright waits
        try:
            self.selector_timeout_ms = int(float(os.getenv("FORUM_SETTINGS_TIMEOUT", "15")) * 1000)
        except Exception:
            self.selector_timeout_ms = 15000
        # headless setting
        self.headless = str(os.getenv("FORUM_SETTINGS_HEADLESS", "true")).lower() in ("1","true","yes","on")
        try:
            self.max_concurrency = max(1, int(os.getenv("FORUM_MAX_CONCURRENCY", "1")))
        except Exception:
            self.max_concurrency = 1
        self._forum_operation_semaphore = asyncio.Semaphore(self.max_concurrency)
        # Cached Zendesk SSO handshake (see _ensure_support_session)
        try:
            self._support_sso_ttl = max(0, int(float(os.getenv("FORUM_SSO_TTL_SECONDS", "1800"))))
        except Exception:
            self._support_sso_ttl = 1800
        self._support_sso_jwt: Optional[str] = None
        self._support_sso_locale: Optional[str] = None
        self._support_sso_valid_until = 0.0
        try:
            self._post_ttl = max(0, int(float(os.getenv("FORUM_POST_TTL_SECONDS", "86400"))))
        except Exception:
            self._post_ttl = 86400
        # The session is mainly used for the initial authentication via brain_client
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36'
        })

    def _get_post_api_url(self, post_id: str) -> str:
        return f"{self.base_url}/api/v2/community/posts/{post_id}.json?include=users"

    def _get_comments_api_url(self, post_id: str, page: int) -> str:
        return f"{self.base_url}/api/v2/community/posts/{post_id}/comments.json?page={page}&include=users"

    async def _ensure_support_session(self, email: str, password: str, locale: str = "zh-cn", timeout_seconds: int = 30):
        """Establish a Zendesk support session using the authenticated BRAIN session.

        The SSO handshake is a multi-hop redirect chain that costs far more than
        the forum reads that follow it, and the cookie it plants stays valid for
        the life of the BRAIN session — so it is redone only after
        ``FORUM_SSO_TTL_SECONDS`` (default 30 min) or when the BRAIN session's
        JWT changes (i.e. after a re-authentication).
        """
        try:
            from main import brain_client
        except ImportError:
            import sys
            from pathlib import Path
            current_dir = Path(__file__).parent
            sys.path.insert(0, str(current_dir))
            from main import brain_client

        try:
            await asyncio.wait_for(brain_client.ensure_authenticated(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            log(f"Authentication check timed out after {timeout_seconds}s, proceeding anyway.", "WARNING")
        except Exception:
            log("Authenticating with BRAIN platform...", "INFO")
            auth_result = await asyncio.wait_for(
                brain_client.authenticate(email, password),
                timeout=timeout_seconds
            )
            if auth_result.get('status') != 'authenticated':
                raise Exception("BRAIN platform authentication failed.")

        # Skip the handshake while the cached one is still backed by the same JWT.
        jwt = None
        try:
            jwt = brain_client.session.cookies.get('t')
        except Exception:
            jwt = None
        now = time.time()
        if (
            jwt
            and self._support_sso_jwt == jwt
            and self._support_sso_locale == locale
            and now < self._support_sso_valid_until
        ):
            return brain_client.session

        access_url = (
            "https://worldquantbrain.zendesk.com/access"
            f"?brand_id=1500000894061&locale={locale}"
            f"&return_to={self.base_url}/hc/{locale}"
        )

        def establish_session():
            return brain_client.session.get(
                access_url,
                timeout=timeout_seconds,
                allow_redirects=True,
                headers={
                    'User-Agent': self.session.headers['User-Agent'],
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                },
            )

        response = await asyncio.to_thread(establish_session)
        log(
            f"Support SSO handshake completed with status={response.status_code}, final_url={response.url}",
            "INFO"
        )
        if response.status_code < 400:
            self._support_sso_jwt = jwt
            self._support_sso_locale = locale
            self._support_sso_valid_until = time.time() + self._support_sso_ttl
        return brain_client.session

    async def _get_support_json(self, session: requests.Session, url: str, timeout_seconds: int = 30) -> Dict[str, Any]:
        """Fetch forum JSON using the Zendesk-authenticated session."""
        def do_request():
            response = session.get(
                url,
                timeout=timeout_seconds,
                headers={
                    'User-Agent': self.session.headers['User-Agent'],
                    'Accept': 'application/json',
                },
            )
            response.raise_for_status()
            return response.json()

        return await asyncio.to_thread(do_request)

    async def _get_browser_context(self, p: Any, email: str, password: str, timeout_seconds: int = 30):
        """Authenticate and return a browser context with the session."""
        # Import brain_client here to avoid circular dependency
        try:
            from main import brain_client
        except ImportError:
            # 尝试从当前目录导入
            import sys
            from pathlib import Path
            current_dir = Path(__file__).parent
            sys.path.insert(0, str(current_dir))
            from main import brain_client
        
        # Avoid re-authenticating on every forum call: authenticate clears cookies and can
        # disrupt other concurrent tools using the same session.
        try:
            await asyncio.wait_for(brain_client.ensure_authenticated(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            log(f"Authentication check timed out after {timeout_seconds}s, proceeding anyway.", "WARNING")
        except Exception:
            log("Authenticating with BRAIN platform...", "INFO")
            try:
                auth_result = await asyncio.wait_for(
                    brain_client.authenticate(email, password),
                    timeout=timeout_seconds
                )
                if auth_result.get('status') != 'authenticated':
                    raise Exception("BRAIN platform authentication failed.")
                log("Successfully authenticated with BRAIN platform.", "SUCCESS")
            except asyncio.TimeoutError:
                log(f"Authentication timed out after {timeout_seconds}s, continuing with partial session.", "WARNING")

        # 获取可用的浏览器路径
        browser_path = get_browser_path()
        
        # 设置浏览器启动参数 - 增强稳定性
        browser_args = [
            '--headless=new',
            '--no-sandbox',
            '--disable-dev-shm-usage',
            '--disable-gpu',
            '--disable-extensions',
            '--disable-background-networking',
            '--disable-sync',
            '--no-first-run'
        ]
        
        try:
            if browser_path and os.path.exists(browser_path):
                log(f"使用自定义浏览器路径: {browser_path}", "INFO")
                browser = await asyncio.wait_for(
                    p.chromium.launch(executable_path=browser_path, args=browser_args, timeout=timeout_seconds * 1000),
                    timeout=timeout_seconds + 10
                )
            else:
                log("使用默认Playwright浏览器", "INFO")
                browser = await asyncio.wait_for(
                    p.chromium.launch(headless=self.headless, args=browser_args, timeout=timeout_seconds * 1000),
                    timeout=timeout_seconds + 10
                )
        except asyncio.TimeoutError:
            raise Exception(f"Browser launch timed out after {timeout_seconds + 10}s")
            
        try:
            context = await asyncio.wait_for(
                # 不要删这个 user_agent：--headless=new 会让 chromium 在 UA 里自报
                # HeadlessChrome/<ver>，论坛前面的 Cloudflare 见到就返回 403 挑战页。
                # 实测（2026-09-17）：去掉本行未登录访问 support.worldquantbrain.com
                # 得到 403 Just a moment...；保留则 200 正常跳登录页。
                browser.new_context(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36'),
                timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            await browser.close()
            raise Exception("Browser context creation timed out")

        log("Transferring authentication session to browser...", "INFO")
        try:
            cookies = brain_client.session.cookies
            playwright_cookies = []
            for cookie in cookies:
                cookie_dict = {
                    'name': cookie.name,
                    'value': cookie.value,
                    'domain': cookie.domain if cookie.domain else '.worldquantbrain.com',
                    'path': cookie.path if cookie.path else '/',
                    'secure': cookie.secure if hasattr(cookie, 'secure') else True,
                    'httpOnly': 'HttpOnly' in cookie._rest if hasattr(cookie, '_rest') else False,
                    'sameSite': 'Lax'
                }
                if hasattr(cookie, 'expires') and cookie.expires:
                    cookie_dict['expires'] = cookie.expires
                playwright_cookies.append(cookie_dict)
            
            await asyncio.wait_for(context.add_cookies(playwright_cookies), timeout=10)
            log("Session transferred.", "SUCCESS")
        except Exception as e:
            log(f"Cookie transfer warning (continuing): {str(e)}", "WARNING")
        
        return browser, context

    async def get_glossary_terms(self, email: str, password: str,
                                 force_refresh: bool = False) -> List[Dict[str, str]]:
        """Extract glossary terms from the forum using Playwright.

        This spins up a whole headless browser to scrape one article of platform
        terminology that changes maybe once a year, so the parsed result is kept
        permanently on disk and the browser is only launched on a miss.
        """
        store = _brain_store()
        if store is not None and not force_refresh:
            cached = await store.get('forum_glossary', 'terms')
            if cached:
                return cached

        if async_playwright is None:
            raise ImportError("Playwright not available. Please install it with: pip install playwright")

        async with self._forum_operation_semaphore:
            async with async_playwright() as p:
                browser = None
                try:
                    log("Starting glossary extraction process with Playwright", "INFO")
                    browser, context = await self._get_browser_context(p, email, password)
                    
                    page = await context.new_page()
                    log("Navigating to BRAIN support forum glossary...", "INFO")
                    await page.goto("https://support.worldquantbrain.com/hc/en-us/articles/4902349883927-Click-here-for-a-list-of-terms-and-their-definitions")
                    
                    log("Extracting glossary content...", "INFO")
                    content = await page.content()
                    
                    terms = _parse_glossary_terms(content)

                    log(f"Extracted {len(terms)} glossary terms", "SUCCESS")
                    if terms and store is not None:
                        await store.put('forum_glossary', 'terms', terms)
                    return terms

                except Exception as e:
                    log(f"Glossary extraction failed: {str(e)}", "ERROR")
                    # Re-raise to be handled by the MCP server wrapper
                    raise
                finally:
                    if browser:
                        await browser.close()
                        log("Browser closed.", "INFO")

    async def search_forum_posts(self, email: str, password: str, search_query: str, max_results: int = 50, locale: str = "zh-cn") -> Dict[str, Any]:
        """Search for posts on the forum using Playwright, with pagination and timeout protection."""
        if async_playwright is None:
            raise ImportError("Playwright not available. Please install it with: pip install playwright")
        
        timeout_seconds = 30  # Per-operation timeout
        overall_timeout = 120  # Overall timeout for entire search
        browser = None
        
        async def _do_search():
            nonlocal browser
            async with self._forum_operation_semaphore:
                async with async_playwright() as p:
                    try:
                        log(f"Starting forum search for '{search_query}'", "INFO")
                        browser, context = await asyncio.wait_for(
                            self._get_browser_context(p, email, password, timeout_seconds),
                            timeout=timeout_seconds + 15
                        )

                        page = await asyncio.wait_for(context.new_page(), timeout=timeout_seconds)
                        page.set_default_timeout(timeout_seconds * 1000)
                        page.set_default_navigation_timeout(timeout_seconds * 1000)
                        
                        search_results = []
                        page_num = 1
                        max_pages = 5  # Limit pages to prevent infinite loops
                        
                        while len(search_results) < max_results and page_num <= max_pages:
                            search_url = f"{self.base_url}/hc/{locale}/search?page={page_num}&query={quote(search_query)}#results"
                            log(f"Navigating to search page {page_num}: {search_url}", "INFO")
                            
                            try:
                                response = await asyncio.wait_for(
                                    page.goto(search_url, wait_until="domcontentloaded"),
                                    timeout=timeout_seconds
                                )
                                if response and response.status == 404:
                                    log(f"Page {page_num} not found. End of results.", "INFO")
                                    break
                                
                                await asyncio.wait_for(
                                    page.wait_for_selector('ul.search-results-list', timeout=self.selector_timeout_ms),
                                    timeout=min(timeout_seconds, 15)
                                )
                            except asyncio.TimeoutError:
                                log(f"Page {page_num} navigation timed out, stopping.", "WARNING")
                                break
                            except Exception as e:
                                log(f"Could not load search results on page {page_num}: {e}", "WARNING")
                                break

                            try:
                                content = await asyncio.wait_for(page.content(), timeout=10)
                            except asyncio.TimeoutError:
                                log(f"Page content retrieval timed out on page {page_num}", "WARNING")
                                break
                            
                            soup = BeautifulSoup(content, 'html.parser')
                            
                            results_on_page = soup.select('li.search-result-list-item')
                            if not results_on_page:
                                log("No more search results found.", "INFO")
                                break

                            for result in results_on_page:
                                try:
                                    title_element = result.select_one('h2.search-result-title a')
                                    snippet_element = result.select_one('.search-results-description')
                                    
                                    if title_element:
                                        title = title_element.get_text(strip=True)
                                        link = title_element.get('href')

                                        votes_element = result.select_one('.search-result-votes span[aria-hidden="true"]')
                                        votes_text = votes_element.get_text(strip=True) if votes_element else '0'
                                        votes_match = re.search(r'\d+', votes_text)
                                        votes = int(votes_match.group()) if votes_match else 0

                                        comments_element = result.select_one('.search-result-meta-count span[aria-hidden="true"]')
                                        comments_text = comments_element.get_text(strip=True) if comments_element else '0'
                                        comments_match = re.search(r'\d+', comments_text)
                                        comments = int(comments_match.group()) if comments_match else 0

                                        breadcrumbs_elements = result.select('ol.search-result-breadcrumbs li')
                                        breadcrumbs = [bc.get_text(strip=True) for bc in breadcrumbs_elements]
                                        
                                        meta_group = result.select_one('ul.meta-group')
                                        author = 'Unknown'
                                        post_date = 'Unknown'
                                        if meta_group:
                                            meta_data_elements = meta_group.select('li.meta-data')
                                            if len(meta_data_elements) > 0:
                                                author = meta_data_elements[0].get_text(strip=True)
                                            if len(meta_data_elements) > 1:
                                                time_element = meta_data_elements[1].select_one('time')
                                                if time_element:
                                                    post_date = time_element.get('datetime', time_element.get_text(strip=True))

                                        snippet = snippet_element.get_text(strip=True) if snippet_element else ''
                                        
                                        full_link = ''
                                        link = _unwrap_search_click_url(link)
                                        if link and isinstance(link, str):
                                            if link.startswith('http'):
                                                full_link = link
                                            else:
                                                full_link = f"{self.base_url}{link}"
                                        
                                        search_results.append({
                                            'title': title,
                                            'link': full_link,
                                            'snippet': snippet,
                                            'votes': votes,
                                            'comments': comments,
                                            'author': author,
                                            'date': post_date,
                                            'breadcrumbs': breadcrumbs
                                        })
                                    
                                    if len(search_results) >= max_results:
                                        break
                                except Exception as e:
                                    log(f"Error parsing search result: {str(e)}", "DEBUG")
                                    continue
                            
                            if len(search_results) >= max_results:
                                break

                            page_num += 1

                        log(f"Found {len(search_results)} results for '{search_query}'", "SUCCESS")
                        
                        return {
                            "success": True,
                            "results": search_results,
                            "total_found": len(search_results)
                        }

                    except Exception as e:
                        log(f"Forum search failed: {str(e)}", "ERROR")
                        return {
                            "success": False,
                            "results": [],
                            "total_found": 0,
                            "error": str(e)
                        }
                    finally:
                        if browser:
                            try:
                                await asyncio.wait_for(browser.close(), timeout=5)
                            except Exception:
                                pass
        
        try:
            return await asyncio.wait_for(_do_search(), timeout=overall_timeout)
        except asyncio.TimeoutError:
            log(f"Forum search overall timeout after {overall_timeout}s", "ERROR")
            return {
                "success": False,
                "results": [],
                "total_found": 0,
                "error": f"Forum search timed out after {overall_timeout}s"
            }

    async def read_full_forum_post(self, email: str, password: str, post_url_or_id: str,
                                   include_comments: bool = True,
                                   force_refresh: bool = False) -> Dict[str, Any]:
        """Read a complete forum post and its comments via Zendesk JSON API.

        A post's body is written once; its comment thread grows slowly. Reading a
        long thread costs one request per comment page on top of the SSO
        handshake, so the assembled result is stored on disk and re-read only
        after FORUM_POST_TTL_SECONDS (default 24h) or on force_refresh.
        """
        store = _brain_store()
        post_id = _extract_post_id(post_url_or_id)
        cache_key = f"{post_id}:{'with' if include_comments else 'no'}-comments"
        if store is not None and not force_refresh:
            envelope = await store.get_envelope('forum_post', cache_key)
            if envelope and envelope.get('payload'):
                age = time.time() - (envelope.get('fetched_at') or 0)
                if age <= self._post_ttl:
                    payload = dict(envelope['payload'])
                    payload['from_cache'] = True
                    payload['cached_age_hours'] = round(age / 3600, 1)
                    return payload

        try:
            async with self._forum_operation_semaphore:
                log("Starting forum post reading process via Zendesk API", "INFO")

                locale = _extract_locale(post_url_or_id)
                session = await self._ensure_support_session(email, password, locale=locale)

                post_payload = await self._get_support_json(session, self._get_post_api_url(post_id))
                post_record = post_payload.get('post') or {}
                user_map = {
                    user.get('id'): user.get('name')
                    for user in post_payload.get('users', [])
                    if isinstance(user, dict)
                }

                post_data = {
                    'title': post_record.get('title', 'Unknown Title'),
                    'author': user_map.get(post_record.get('author_id'), 'Unknown Author'),
                    'body': _html_to_text(post_record.get('details', '')) or 'Body not found',
                    'details': {
                        'votes': str(post_record.get('vote_sum', 0)),
                        'date': post_record.get('created_at', 'Unknown Date') or 'Unknown Date',
                        'url': post_record.get('html_url') or post_record.get('url')
                    }
                }

                comments: List[Dict[str, Any]] = []
                if include_comments:
                    page_num = 1
                    while True:
                        comments_payload = await self._get_support_json(
                            session,
                            self._get_comments_api_url(post_id, page_num)
                        )
                        comment_records = comments_payload.get('comments') or []
                        if not comment_records:
                            break

                        users = comments_payload.get('users') or []
                        user_map.update({
                            user.get('id'): user.get('name')
                            for user in users
                            if isinstance(user, dict)
                        })

                        for comment_record in comment_records:
                            comment_data = {
                                'author': user_map.get(comment_record.get('author_id'), 'Unknown'),
                                'body': _html_to_text(comment_record.get('body', '')),
                                'date': comment_record.get('created_at', 'Unknown Date') or 'Unknown Date'
                            }
                            comments.append(comment_data)

                        if not comments_payload.get('next_page'):
                            break
                        page_num += 1

                log(f"Extracted {len(comments)} comments in total.", "SUCCESS")
                result = {
                    'success': True,
                    'post': post_data,
                    'comments': comments,
                    'total_comments': len(comments)
                }
                if store is not None:
                    await store.put('forum_post', cache_key, result)
                return result

        except Exception as e:
            log(f"Failed to read forum post: {str(e)}", "ERROR")
            raise

# Initialize forum client
forum_client = ForumClient()

# The main block is for testing and won't be run by the MCP server.
if __name__ == "__main__":
    print("📚 WorldQuant BRAIN Forum Functions - This script provides the ForumClient class.", file=sys.stderr)
    # Basic local smoke test if credentials are set via env.
    email = os.getenv("CREDENTIALS_EMAIL")
    password = os.getenv("CREDENTIALS_PASSWORD")
    if email and password:
        # Example article id; adjust if needed
        article_id = "36371597455127-示例文章"
        try:
            result = asyncio.run(forum_client.read_full_forum_post(email, password, article_id))
            print(result)
        except Exception as e:
            log(f"Local test failed: {e}", "ERROR")
    else:
        log("Set CREDENTIALS_EMAIL and CREDENTIALS_PASSWORD in environment to run local test.", "INFO")