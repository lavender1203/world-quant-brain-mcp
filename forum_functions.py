#!/usr/bin/env python3
"""
WorldQuant BRAIN Forum Functions - Python Version
Comprehensive forum functionality including glossary, search, and post viewing using Playwright.
"""

import asyncio
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

# 导入浏览器设置模块
def get_browser_path():
    """获取可用的浏览器路径"""
    try:
        # 尝试直接导入
        import browser_setup
        return browser_setup.ensure_browser_available()
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
                return browser_setup.ensure_browser_available()
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
        # The session is mainly used for the initial authentication via brain_client
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36'
        })

    async def _get_browser_context(self, p: Any, email: str, password: str):
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
            await brain_client.ensure_authenticated()
        except Exception:
            log("Authenticating with BRAIN platform...", "INFO")
            auth_result = await brain_client.authenticate(email, password)
            if auth_result.get('status') != 'authenticated':
                raise Exception("BRAIN platform authentication failed.")
            log("Successfully authenticated with BRAIN platform.", "SUCCESS")

        # 获取可用的浏览器路径
        browser_path = get_browser_path()
        
        # 设置浏览器启动参数
        browser_args = ['--headless=new', '--no-sandbox', '--disable-dev-shm-usage']
        
        if browser_path and os.path.exists(browser_path):
            log(f"使用自定义浏览器路径: {browser_path}", "INFO")
            browser = await p.chromium.launch(executable_path=browser_path, args=browser_args)
        else:
            log("使用默认Playwright浏览器", "INFO")
            browser = await p.chromium.launch(headless=self.headless, args=browser_args)
            
        context = await browser.new_context(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36')

        log("Transferring authentication session to browser...", "INFO")
        cookies = brain_client.session.cookies
        playwright_cookies = []
        for cookie in cookies:
            cookie_dict = {
                'name': cookie.name,
                'value': cookie.value,
                'domain': cookie.domain,
                'path': cookie.path,
                'secure': cookie.secure,
                'httpOnly': 'HttpOnly' in cookie._rest,
                'sameSite': 'Lax'
            }
            if cookie.expires:
                cookie_dict['expires'] = cookie.expires
            playwright_cookies.append(cookie_dict)
        
        await context.add_cookies(playwright_cookies)
        log("Session transferred.", "SUCCESS")
        
        return browser, context

    async def get_glossary_terms(self, email: str, password: str) -> List[Dict[str, str]]:
        """Extract glossary terms from the forum using Playwright."""
        if async_playwright is None:
            raise ImportError("Playwright not available. Please install it with: pip install playwright")
            
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
        """Search for posts on the forum using Playwright, with pagination."""
        if async_playwright is None:
            raise ImportError("Playwright not available. Please install it with: pip install playwright")
            
        async with async_playwright() as p:
            browser = None
            try:
                log(f"Starting forum search for '{search_query}'", "INFO")
                browser, context = await self._get_browser_context(p, email, password)

                page = await context.new_page()
                
                search_results = []
                page_num = 1
                
                while len(search_results) < max_results:
                    search_url = f"{self.base_url}/hc/{locale}/search?page={page_num}&query={search_query}#results"
                    log(f"Navigating to search page: {search_url}", "INFO")
                    
                    try:
                        response = await page.goto(search_url)
                        if response.status == 404:
                            log(f"Page {page_num} not found. End of results.", "INFO")
                            break
                        await page.wait_for_selector('ul.search-results-list', timeout=self.selector_timeout_ms)
                    except Exception as e:
                        log(f"Could not load search results on page {page_num}: {e}", "INFO")
                        break

                    content = await page.content()
                    soup = BeautifulSoup(content, 'html.parser')
                    
                    results_on_page = soup.select('li.search-result-list-item')
                    if not results_on_page:
                        log("No more search results found.", "INFO")
                        break

                    for result in results_on_page:
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
                raise
            finally:
                if browser:
                    await browser.close()

    async def read_full_forum_post(self, email: str, password: str, post_url_or_id: str, include_comments: bool = True) -> Dict[str, Any]:
        """Read a complete forum post and all its comments using Playwright."""
        if async_playwright is None:
            raise ImportError("Playwright not available. Please install it with: pip install playwright")
            
        async with async_playwright() as p:
            browser = None
            try:
                log("Starting forum post reading process with Playwright", "INFO")

                if isinstance(post_url_or_id, str) and post_url_or_id.startswith('http'):
                    initial_url = post_url_or_id
                else:
                    initial_url = f"https://support.worldquantbrain.com/hc/zh-cn/community/posts/{post_url_or_id}"

                browser, context = await self._get_browser_context(p, email, password)
                page = await context.new_page()

                # --- Get Main Post Content and Final URL ---
                log(f"Navigating to initial URL: {initial_url}", "INFO")
                await page.goto(initial_url)
                await page.wait_for_selector('.post-body, .article-body', timeout=self.selector_timeout_ms)
                
                # Get the final URL after any redirects
                base_url = re.sub(r'(\?|&)page=\d+', '', page.url).split('#')[0]
                log(f"Resolved to Base URL: {base_url}", "INFO")
                await page.wait_for_selector('.post-body, .article-body', timeout=self.selector_timeout_ms)
                content = await page.content()
                soup = BeautifulSoup(content, 'html.parser')

                post_data = {}
                title_element = soup.select_one('.post-title, h1.article-title, .article__title')
                post_data['title'] = title_element.get_text(strip=True) if title_element else 'Unknown Title'

                author_span = soup.select_one('.post-author span[title]')
                post_data['author'] = author_span['title'] if author_span else 'Unknown Author'

                body_element = soup.select_one('.post-body, .article-body')
                post_data['body'] = body_element.get_text(strip=True) if body_element else 'Body not found'
                
                votes_element = soup.select_one('.vote-sum')
                date_element = soup.select_one('.post-meta .meta-data')
                post_data['details'] = {
                    'votes': votes_element.get_text(strip=True) if votes_element else '0',
                    'date': date_element.get_text(strip=True) if date_element else 'Unknown Date'
                }

                # --- Get Comments with Pagination ---
                comments = []
                if include_comments:
                    log("Starting comment extraction...", "INFO")
                    page_num = 1
                    while True:
                        comment_url = f"{base_url}?page={page_num}#comments"
                        log(f"Navigating to comment page: {comment_url}", "INFO")
                        
                        try:
                            response = await page.goto(comment_url)
                            if response.status == 404:
                                log(f"Page {page_num} returned 404. End of comments.", "INFO")
                                break
                            await page.wait_for_selector('.comment-list', timeout=self.selector_timeout_ms)
                        except Exception as e:
                            log(f"Could not load page {page_num}: {e}. Assuming end of comments.", "INFO")
                            break

                        comment_soup = BeautifulSoup(await page.content(), 'html.parser')
                        comment_elements = comment_soup.select('.comment')

                        if not comment_elements:
                            log(f"No comments found on page {page_num}. Ending extraction.", "INFO")
                            break
                        
                        log(f"Found {len(comment_elements)} comments on page {page_num}.", "INFO")
                        
                        new_comments_found_on_page = 0
                        for comment_element in comment_elements:
                            author_span = comment_element.select_one('.comment-author span[title]')
                            author_id = author_span['title'] if author_span else 'Unknown'

                            body_element = comment_element.select_one('.comment-body')
                            date_element = comment_element.select_one('.comment-meta .meta-data')
                            
                            comment_data = {
                                'author': author_id,
                                'body': body_element.get_text(strip=True) if body_element else '',
                                'date': date_element.get_text(strip=True) if date_element else 'Unknown Date'
                            }
                            
                            if comment_data not in comments:
                                comments.append(comment_data)
                                new_comments_found_on_page += 1

                        if new_comments_found_on_page == 0 and page_num > 1:
                            log(f"No new comments detected on page {page_num}. Ending extraction.", "INFO")
                            break
                            
                        page_num += 1

                log(f"Extracted {len(comments)} comments in total.", "SUCCESS")
                return {
                    "success": True, "post": post_data, "comments": comments, "total_comments": len(comments)
                }

            except Exception as e:
                log(f"Failed to read forum post: {str(e)}", "ERROR")
                raise
            finally:
                if browser:
                    await browser.close()

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