#!/usr/bin/env python3
"""
WorldQuant BRAIN MCP Server - Python Version
A comprehensive Model Context Protocol (MCP) server for WorldQuant BRAIN platform integration.
"""

import json
import time
import asyncio
import logging
from typing import Dict, List, Optional, Any, Union
import re
import base64
from bs4 import BeautifulSoup
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
import os
import sys
from pathlib import Path
from time import sleep
from urllib.parse import urljoin
import redis
import hashlib
import math
import uuid
import random

import requests
import pandas as pd
import zlib
import msgpack
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, EmailStr, model_validator

# Import the new forum client
from forum_functions import forum_client

# Import the BRAIN Labs client (Playwright sign-in + single-concurrency lock)
from labs_functions import labs_client

# Candidate-pool management (submission queue + pyramid coverage + correlation safety)
import candidate_pool as cpool

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def archive_performance_comparison(
    alpha_id: str, scope: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Atomically archive a non-empty comparison result without credentials."""
    if not payload or payload.get("available") is False or payload.get("error"):
        return payload

    root = Path(
        os.environ.get(
            "BRAIN_PERFORMANCE_COMPARISON_ARCHIVE",
            r"D:\wqb_tuzige\tracking\performance_comparison",
        )
    )
    root.mkdir(parents=True, exist_ok=True)
    saved_at = datetime.now().astimezone().isoformat(timespec="seconds")
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    safe_alpha_id = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in alpha_id
    )
    archive_path = root / f"{safe_alpha_id}-{timestamp}.json"
    temporary_path = archive_path.with_suffix(".json.tmp")
    record = {
        "schema_version": 1,
        "saved_at": saved_at,
        "alpha_id": alpha_id,
        "scope": scope,
        "result": payload,
    }
    temporary_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_path, archive_path)
    result = dict(payload)
    result["_archive"] = {
        "saved": True,
        "path": str(archive_path),
        "saved_at": saved_at,
    }
    return result

# Pydantic models for type safety
class AuthCredentials(BaseModel):
    email: EmailStr
    password: str

class SimulationSettings(BaseModel):
    instrumentType: str = "EQUITY"
    region: str = "USA"
    universe: str = "TOP3000"
    delay: int = 1
    decay: float = 0.0
    neutralization: str = "NONE"
    truncation: float = 0.0
    pasteurization: str = "ON"
    unitHandling: Optional[str] = "VERIFY"
    nanHandling: Optional[str] = "OFF"
    language: str = "FASTEXPR"
    lookback: Optional[int] = None
    visualization: bool = True
    testPeriod: str = "P0Y0M"
    selectionHandling: str = "POSITIVE"
    selectionLimit: int = 1000
    maxTrade: str = "OFF"
    componentActivation: str = "IS"

class SimulationData(BaseModel):
    type: str = "REGULAR"  # "REGULAR" or "SUPER"
    settings: SimulationSettings
    regular: Optional[str] = None
    combo: Optional[str] = None
    selection: Optional[str] = None

    @model_validator(mode="after")
    def validate_super_selection_rules(self) -> "SimulationData":
        if self.type.upper() != "SUPER":
            return self

        region = self.settings.region.upper()
        if region != "USA":
            return self

        if not self.selection:
            raise ValueError('USA SUPER simulations require selection to include (prod_correlation > 0)')

        if not re.search(r"\(\s*prod_correlation\s*>\s*0(?:\.0+)?\s*\)", self.selection):
            raise ValueError('USA SUPER simulations require selection to include (prod_correlation > 0)')

        return self

class BrainApiClient:
    """WorldQuant BRAIN API client with comprehensive functionality."""
    
    def __init__(self):
        # Best-effort: load .env early so env overrides are available here
        try:
            from dotenv import load_dotenv, find_dotenv
            env_path = find_dotenv(usecwd=True)
            if env_path:
                load_dotenv(env_path, override=False)
            else:
                candidate = Path(__file__).parent / ".env"
                if candidate.exists():
                    load_dotenv(candidate, override=False)
        except Exception:
            # Fallback: simple parser
            try:
                candidate = Path(__file__).parent / ".env"
                if candidate.exists():
                    for line in candidate.read_text().splitlines():
                        line = line.strip()
                        if not line or line.startswith('#') or '=' not in line:
                            continue
                        k, v = line.split('=', 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        os.environ.setdefault(k, v)
            except Exception:
                pass

        self.base_url = "https://api.worldquantbrain.com"
        self.session = requests.Session()
        self.auth_credentials = None
        self.is_authenticating = False
        self._request_semaphore = asyncio.Semaphore(int(os.environ.get("BRAIN_MAX_CONCURRENCY", "8")))
        self._session_lock = asyncio.Lock()
        self._auth_lock = asyncio.Lock()
        self._auth_validated_until = 0.0
        try:
            self._auth_check_ttl_seconds = max(0.0, float(os.environ.get("BRAIN_AUTH_CHECK_TTL_SECONDS", "300")))
        except Exception:
            self._auth_check_ttl_seconds = 300.0
        self._brain_correlation_local_lock = asyncio.Lock()
        self._os_pnl_pool_locks: Dict[str, asyncio.Lock] = {}
        self._os_pnl_pool_locks_guard = asyncio.Lock()
        self._os_pnl_pool_last_sync: Dict[str, Any] = {}
        try:
            self._os_pnl_pool_sync_debounce_seconds = max(
                0.0,
                float(os.environ.get("BRAIN_SC_POOL_SYNC_DEBOUNCE_SECONDS", "1")),
            )
        except Exception:
            self._os_pnl_pool_sync_debounce_seconds = 1.0
        try:
            self._brain_correlation_busy_retry_after_seconds = max(
                1,
                int(os.environ.get("BRAIN_CORRELATION_BUSY_RETRY_AFTER_SECONDS", "180")),
            )
        except Exception:
            self._brain_correlation_busy_retry_after_seconds = 180
        # Allow timeout override via env (e.g., API_SETTINGS_TIMEOUT)
        try:
            self._default_timeout_seconds = int(os.environ.get("API_SETTINGS_TIMEOUT", "30"))
        except Exception:
            self._default_timeout_seconds = 30
        self._create_simulation_semaphore = asyncio.Semaphore(int(os.environ.get("BRAIN_CREATE_SIMULATION_MAX_CONCURRENCY", "6")))
        try:
            self._forum_rate_limit_seconds = max(0, int(os.environ.get("FORUM_RATE_LIMIT_SECONDS", "0")))
        except Exception:
            self._forum_rate_limit_seconds = 0
        self._forum_rate_limit_lock = asyncio.Lock()
        self._forum_rate_limit_until = 0.0
        # Async submission tracking: platform-side submission checks can take
        # minutes up to ~1 hour, so submissions run as background tasks and
        # their progress is tracked here (keyed by alpha_id).
        self._submission_states: Dict[str, Dict[str, Any]] = {}
        self._submission_tasks: Dict[str, asyncio.Task] = {}
        # The platform processes one submission check at a time per account.
        self._submission_serialize_lock = asyncio.Lock()
        try:
            self._submit_max_seconds = max(300.0, float(os.environ.get("BRAIN_SUBMIT_MAX_SECONDS", "5400")))
        except Exception:
            self._submit_max_seconds = 5400.0
        
        # Configure session
        self.session.timeout = self._default_timeout_seconds
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        
        # Load OS/IS Sharpe ratio data for datafield quality filtering
        self._isos_data = {}
        try:
            info_data_path = Path(__file__).parent / 'config' / 'info_data.bin'
            if info_data_path.exists():
                with open(info_data_path, 'rb') as f:
                    self._isos_data = msgpack.unpackb(zlib.decompress(f.read()), raw=False)
                self.log(f"Loaded OS/IS Sharpe data: {len(self._isos_data)} region_delay entries", "INFO")
            else:
                self.log(f"OS/IS Sharpe data file not found at {info_data_path}, sharpe filtering disabled", "WARNING")
        except Exception as e:
            self.log(f"Failed to load OS/IS Sharpe data: {str(e)}, sharpe filtering disabled", "WARNING")

        # Initialize Redis connection
        try:
            redis_host = os.environ.get('REDIS_HOST', 'localhost')
            try:
                redis_port = int(os.environ.get('REDIS_PORT', str(6379)))
            except Exception:
                redis_port = 6379

            self.redis_client = redis.Redis(
                host=redis_host,
                port=redis_port,
                db=0,
                decode_responses=True,
                socket_connect_timeout=5
            )
            # Test connection
            self.redis_client.ping()
            self.log("Redis connection established", "INFO")
        except Exception as e:
            self.log(f"Redis connection failed: {str(e)}, caching disabled", "WARNING")
            self.redis_client = None
    
    def log(self, message: str, level: str = "INFO"):
        """Log messages to stderr to avoid MCP protocol interference."""
        print(f"[{level}] {message}", file=sys.stderr)
    
    def _to_absolute_url(self, url: str) -> str:
        if not url:
            return url
        if url.startswith("http://") or url.startswith("https://"):
            return url
        return urljoin(self.base_url, url)

    def _response_payload(self, response: requests.Response) -> Any:
        """Return JSON when possible, otherwise response text for diagnostics."""
        try:
            return response.json()
        except ValueError:
            return response.text

    def _simulation_error_message(self, data: Any) -> str:
        """Extract the most useful error text from a simulation progress payload."""
        if not isinstance(data, dict):
            return str(data) if data is not None else "Unknown error"

        for key in ("error", "message", "detail", "details", "statusMessage", "status"):
            value = data.get(key)
            if value:
                if isinstance(value, (dict, list)):
                    return json.dumps(value, ensure_ascii=False)
                return str(value)

        collected: list[str] = []

        def visit(node: Any) -> None:
            if len(collected) >= 8:
                return
            if isinstance(node, dict):
                for key, value in node.items():
                    lower = str(key).lower()
                    if any(token in lower for token in ("error", "message", "exception", "traceback")) and value:
                        if isinstance(value, (dict, list)):
                            collected.append(json.dumps(value, ensure_ascii=False))
                        else:
                            collected.append(str(value))
                    visit(value)
            elif isinstance(node, list):
                for item in node:
                    visit(item)

        visit(data)
        return " | ".join(collected) if collected else "Unknown error"

    def _generate_cache_key(self, prefix: str, params: dict) -> str:
        """Generate a cache key from prefix and parameters."""
        # Sort params to ensure consistent key generation
        sorted_params = sorted(params.items())
        param_str = json.dumps(sorted_params, sort_keys=True)
        hash_str = hashlib.md5(param_str.encode()).hexdigest()
        return f"{prefix}:{hash_str}"
    
    def _get_cached_data(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """Get data from Redis cache."""
        if not self.redis_client:
            return None
        try:
            cached = self.redis_client.get(cache_key)
            if cached:
                self.log(f"Cache hit for key: {cache_key}", "INFO")
                return json.loads(cached)
        except Exception as e:
            self.log(f"Cache read error: {str(e)}", "WARNING")
        return None
    
    def _set_cached_data(self, cache_key: str, data: Dict[str, Any], ttl: int = 604800):
        """Set data in Redis cache with TTL (default 1 week = 604800 seconds)."""
        if not self.redis_client:
            return
        try:
            self.redis_client.setex(cache_key, ttl, json.dumps(data))
            self.log(f"Cached data with key: {cache_key}, TTL: {ttl}s", "INFO")
        except Exception as e:
            self.log(f"Cache write error: {str(e)}", "WARNING")

    def _brain_correlation_lock_key(self) -> str:
        """Per-account lock key. BRAIN's correlation concurrency limit is per
        account, so multi-account deployments sharing one Redis must not block
        each other."""
        email = (self.auth_credentials or {}).get('email') if self.auth_credentials else None
        if email:
            digest = hashlib.md5(email.encode()).hexdigest()[:12]
            return f"lock:brain_correlation:{digest}"
        return "lock:brain_correlation"

    async def _try_acquire_brain_correlation_lock(self, op_name: str) -> Dict[str, Any]:
        """Try once to acquire the per-account platform correlation slot."""
        lock_key = self._brain_correlation_lock_key()
        try:
            lock_ttl = int(os.environ.get("BRAIN_CORRELATION_LOCK_TTL_SECONDS", "3700"))
        except Exception:
            lock_ttl = 3700
        lock_token = uuid.uuid4().hex

        if self.redis_client:
            try:
                if self.redis_client.set(lock_key, lock_token, ex=lock_ttl, nx=True):
                    self.log(
                        f"[corr-lock] Acquired {lock_key} for {op_name} (ttl={lock_ttl}s)",
                        "INFO",
                    )
                    return {
                        'acquired': True,
                        'backend': 'redis',
                        'lock_key': lock_key,
                        'lock_token': lock_token,
                    }
                ttl = self.redis_client.ttl(lock_key)
                self.log(
                    f"[corr-lock] Busy {lock_key} for {op_name} (holder_ttl={ttl}s)",
                    "INFO",
                )
                return {
                    'acquired': False,
                    'backend': 'redis',
                    'lock_key': lock_key,
                    'retry_after': ttl if ttl and ttl > 0 else None,
                }
            except Exception as e:
                self.log(
                    f"[corr-lock] Redis error acquiring lock for {op_name}: {e}. "
                    "Falling back to local fail-fast lock.",
                    "WARNING",
                )

        if self._brain_correlation_local_lock.locked():
            self.log(f"[corr-lock] Busy local correlation lock for {op_name}", "INFO")
            return {
                'acquired': False,
                'backend': 'local',
                'lock_key': lock_key,
                'retry_after': None,
            }

        await self._brain_correlation_local_lock.acquire()
        self.log(f"[corr-lock] Acquired local correlation lock for {op_name}", "INFO")
        return {
            'acquired': True,
            'backend': 'local',
            'lock_key': lock_key,
            'lock_token': lock_token,
        }

    async def _release_brain_correlation_lock(self, lock_info: Dict[str, Any], op_name: str):
        if not lock_info or not lock_info.get('acquired'):
            return
        backend = lock_info.get('backend')
        if backend == 'redis' and self.redis_client:
            try:
                self.redis_client.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] then "
                    "return redis.call('del', KEYS[1]) else return 0 end",
                    1,
                    lock_info['lock_key'],
                    lock_info['lock_token'],
                )
                self.log(f"[corr-lock] Released {lock_info['lock_key']} for {op_name}", "INFO")
            except Exception as e:
                self.log(f"[corr-lock] Lock release failed for {op_name}: {e}", "WARNING")
            return

        if backend == 'local' and self._brain_correlation_local_lock.locked():
            self._brain_correlation_local_lock.release()
            self.log(f"[corr-lock] Released local correlation lock for {op_name}", "INFO")
    
    async def _rate_limit_forum_op(self, op_name: str) -> Optional[Dict[str, Any]]:
        if self._forum_rate_limit_seconds <= 0:
            return None

        if self.redis_client:
            try:
                lock_key = "rate_limit:forum_ops"
                if not self.redis_client.set(lock_key, "locked", ex=self._forum_rate_limit_seconds, nx=True):
                    ttl = self.redis_client.ttl(lock_key)
                    if not isinstance(ttl, int) or ttl < 0:
                        ttl = self._forum_rate_limit_seconds
                    return {
                        'status': 'rate_limited',
                        'message': f"Rate limit exceeded. Please wait {ttl} seconds before trying again.",
                        'retry_after': ttl,
                    }
            except Exception as e:
                self.log(f"Rate limiting for {op_name} failed, falling back to local limiter: {str(e)}", "WARNING")

        async with self._forum_rate_limit_lock:
            now = time.time()
            until = float(self._forum_rate_limit_until)
            if now < until:
                ttl = int(until - now)
                if ttl < 0:
                    ttl = 0
                return {
                    'status': 'rate_limited',
                    'message': f"Rate limit exceeded. Please wait {ttl} seconds before trying again.",
                    'retry_after': ttl,
                }
            self._forum_rate_limit_until = now + self._forum_rate_limit_seconds
            return None
    
    async def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Run blocking requests I/O in a worker thread to avoid blocking the asyncio event loop."""
        absolute_url = self._to_absolute_url(url)
        timeout = kwargs.pop("timeout", self._default_timeout_seconds)
        # Add extra buffer for asyncio timeout to catch stuck threads
        asyncio_timeout = timeout + 10
        
        async with self._request_semaphore:
            async with self._session_lock:
                try:
                    # Wrap asyncio.to_thread with wait_for to prevent infinite hangs
                    return await asyncio.wait_for(
                        asyncio.to_thread(
                            self.session.request,
                            method,
                            absolute_url,
                            timeout=timeout,
                            **kwargs,
                        ),
                        timeout=asyncio_timeout
                    )
                except asyncio.TimeoutError:
                    self.log(f"Request asyncio timeout for {method} {absolute_url} after {asyncio_timeout}s", "ERROR")
                    raise TimeoutError(f"Request timed out after {asyncio_timeout}s")
                except asyncio.CancelledError:
                    self.log(f"Request cancelled for {method} {absolute_url}", "WARNING")
                    raise
                except requests.Timeout as e:
                    self.log(f"Request timeout for {method} {absolute_url}: {str(e)}", "ERROR")
                    raise TimeoutError(f"Request timed out after {timeout}s") from e
                except requests.ConnectionError as e:
                    self.log(f"Connection error for {method} {absolute_url}: {str(e)}", "ERROR")
                    raise ConnectionError(f"Failed to connect to {absolute_url}") from e
                except requests.HTTPError as e:
                    self.log(f"HTTP error for {method} {absolute_url}: {str(e)}", "ERROR")
                    raise
                except Exception as e:
                    # Catch other unexpected errors (e.g., RemoteDisconnected wrapped in other exceptions)
                    error_str = str(e)
                    if "RemoteDisconnected" in error_str or "Connection aborted" in error_str:
                        self.log(f"Remote disconnected for {method} {absolute_url}: {error_str}", "ERROR")
                        raise ConnectionError(f"Remote server disconnected: {absolute_url}") from e
                    raise

    def _retry_wait_seconds(self, response: Optional[requests.Response], attempt: int, base_delay: float = 2.0, max_delay: float = 60.0) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(max(float(retry_after), 0.0), max_delay)
                except (TypeError, ValueError):
                    pass
        backoff = min(base_delay * (1.6 ** attempt), max_delay)
        return backoff + random.uniform(0, min(1.0, backoff * 0.1))

    async def _request_json_with_retries(
        self,
        method: str,
        url: str,
        *,
        op_name: str,
        max_retries: int = 6,
        retry_statuses: Optional[set] = None,
        allow_empty: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """Request JSON with bounded retries for bulk/paginated endpoints."""
        retry_statuses = retry_statuses or {429, 500, 502, 503, 504}
        last_error: Optional[Exception] = None

        for attempt in range(max_retries):
            response: Optional[requests.Response] = None
            try:
                response = await self._request(method, url, **kwargs)
                if response.status_code == 401:
                    self._auth_validated_until = 0.0
                    if attempt < max_retries - 1:
                        self.log(
                            f"{op_name}: HTTP 401, refreshing authentication "
                            f"(attempt {attempt + 1}/{max_retries})",
                            "WARNING",
                        )
                        await self.ensure_authenticated()
                        continue
                    response.raise_for_status()
                if response.status_code in retry_statuses:
                    wait = self._retry_wait_seconds(response, attempt)
                    self.log(
                        f"{op_name}: HTTP {response.status_code}, retrying in {wait:.1f}s "
                        f"(attempt {attempt + 1}/{max_retries})",
                        "WARNING",
                    )
                    await asyncio.sleep(wait)
                    continue

                response.raise_for_status()
                text = (response.text or "").strip()
                if not text:
                    if allow_empty:
                        return {}
                    wait = self._retry_wait_seconds(response, attempt)
                    self.log(
                        f"{op_name}: empty response, retrying in {wait:.1f}s "
                        f"(attempt {attempt + 1}/{max_retries})",
                        "WARNING",
                    )
                    await asyncio.sleep(wait)
                    continue
                try:
                    return response.json() or {}
                except json.JSONDecodeError as e:
                    last_error = e
                    wait = self._retry_wait_seconds(response, attempt)
                    self.log(
                        f"{op_name}: JSON parse failed, retrying in {wait:.1f}s "
                        f"(attempt {attempt + 1}/{max_retries})",
                        "WARNING",
                    )
                    await asyncio.sleep(wait)
                    continue
            except requests.HTTPError:
                raise
            except (ConnectionError, TimeoutError, requests.RequestException) as e:
                last_error = e
                wait = self._retry_wait_seconds(response, attempt)
                self.log(
                    f"{op_name}: transient request failure ({e}), retrying in {wait:.1f}s "
                    f"(attempt {attempt + 1}/{max_retries})",
                    "WARNING",
                )
                await asyncio.sleep(wait)

        if last_error:
            raise last_error
        raise RuntimeError(f"{op_name}: failed after {max_retries} attempts")
    
    async def authenticate(self, email: str, password: str) -> Dict[str, Any]:
        """Authenticate with WorldQuant BRAIN platform with biometric support."""
        async with self._auth_lock:
            return await self._authenticate_unlocked(email, password)

    async def _authenticate_unlocked(self, email: str, password: str) -> Dict[str, Any]:
        """Authenticate while ``_auth_lock`` is already held."""
        self.log("🔐 Starting Authentication process...", "INFO")
        auth_timeout = self._default_timeout_seconds + 10  # Extra buffer for asyncio timeout
        
        try:
            # Store credentials for potential re-authentication
            self.auth_credentials = {'email': email, 'password': password}
            self._auth_validated_until = 0.0
            
            # Clear any existing session data (quick operation, no lock needed for this)
            self.session.cookies.clear()
            self.session.auth = None
            
            # Create Basic Authentication header (base64 encoded credentials)
            import base64
            credentials = f"{email}:{password}"
            encoded_credentials = base64.b64encode(credentials.encode()).decode()
            
            # Send POST request with Basic Authentication header
            headers = {
                'Authorization': f'Basic {encoded_credentials}'
            }
            
            # Use a direct thread call with timeout, no nested locks
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.session.request,
                        'POST',
                        'https://api.worldquantbrain.com/authentication',
                        headers=headers,
                        timeout=self._default_timeout_seconds,
                    ),
                    timeout=auth_timeout
                )
            except asyncio.TimeoutError:
                self.log(f"❌ Authentication request timed out after {auth_timeout}s", "ERROR")
                raise TimeoutError(f"Authentication timed out after {auth_timeout}s")

            # Check for successful authentication (status code 201)
            if response.status_code == 201:
                self.log("Authentication successful", "SUCCESS")
                
                # Check if JWT token was automatically stored by session
                jwt_token = self.session.cookies.get('t')
                if jwt_token:
                    self._auth_validated_until = time.time() + self._auth_check_ttl_seconds
                    self.log("JWT token automatically stored by session", "SUCCESS")
                
                # Return success response
                return {
                    'user': {'email': email},
                    'status': 'authenticated',
                    'permissions': ['read', 'write'],
                    'message': 'Authentication successful',
                    'status_code': response.status_code,
                    'has_jwt': jwt_token is not None
                }
            
            # Check if biometric authentication is required (401 with persona)
            elif response.status_code == 401:
                www_auth = response.headers.get("WWW-Authenticate")
                location = response.headers.get("Location")
                
                if www_auth == "persona" and location:
                    self.log("🔴 Biometric authentication required", "INFO")
                    
                    # Handle biometric authentication
                    from urllib.parse import urljoin
                    biometric_url = urljoin(response.url, location)
                    return await self._handle_biometric_auth(biometric_url, email)
                else:
                    raise Exception("Incorrect email or password")
            else:
                raise Exception(f"Authentication failed with status code: {response.status_code}")
                    
        except asyncio.TimeoutError:
            self.log(f"❌ Authentication timed out", "ERROR")
            raise TimeoutError("Authentication request timed out")
        except requests.HTTPError as e:
            self.log(f"❌ HTTP error during authentication: {e}", "ERROR")
            raise
        except Exception as e:
            self.log(f"❌ Authentication failed: {str(e)}", "ERROR")
            raise
    
    async def _handle_biometric_auth(self, biometric_url: str, email: str) -> Dict[str, Any]:
        """Handle biometric authentication using browser automation."""
        self.log("🌐 Starting biometric authentication...", "INFO")
        
        try:
            # Import playwright for browser automation
            from playwright.async_api import async_playwright
            import time
            
            # 尝试导入browser_setup模块来获取浏览器路径
            browser_path = None
            try:
                from browser_setup import ensure_browser_available
                browser_path = ensure_browser_available()
            except ImportError:
                # 如果导入失败，尝试从当前目录导入
                try:
                    import sys
                    from pathlib import Path
                    current_dir = Path(__file__).parent
                    sys.path.insert(0, str(current_dir))
                    from browser_setup import ensure_browser_available
                    browser_path = ensure_browser_available()
                except:
                    pass
            
            async with async_playwright() as p:
                # 设置浏览器启动参数
                browser_args = ['--headless=new', '--no-sandbox', '--disable-dev-shm-usage']
                
                if browser_path and os.path.exists(browser_path):
                    self.log(f"使用自定义浏览器路径: {browser_path}", "INFO")
                    browser = await p.chromium.launch(executable_path=browser_path, args=browser_args)
                else:
                    self.log("使用默认Playwright浏览器", "INFO")
                    browser = await p.chromium.launch(headless=True, args=browser_args)
                    
                page = await browser.new_page()

                self.log("🌐 Opening browser for biometric authentication...", "INFO")
                await page.goto(biometric_url)
                self.log("Browser page loaded successfully", "SUCCESS")

                # Print instructions
                print("\n" + "="*60, file=sys.stderr)
                print("BIOMETRIC AUTHENTICATION REQUIRED", file=sys.stderr)
                print("="*60, file=sys.stderr)
                print("Browser window is open with biometric authentication page", file=sys.stderr)
                print("Complete the biometric authentication in the browser", file=sys.stderr)
                print("The system will automatically check when you're done...", file=sys.stderr)
                print("="*60, file=sys.stderr)

                # Keep checking until authentication is complete
                max_attempts = 60  # 5 minutes maximum (60 * 5 seconds)
                attempt = 0

                while attempt < max_attempts:
                    await asyncio.sleep(5)  # Check every 5 seconds
                    attempt += 1

                    # Check if authentication completed
                    check_response = await self._request('POST', biometric_url)
                    self.log(f"🔄 Checking authentication status (attempt {attempt}/{max_attempts}): {check_response.status_code}", "INFO")

                    if check_response.status_code == 201:
                        self.log("Biometric authentication successful!", "SUCCESS")

                        await browser.close()
                        
                        # Check JWT token
                        jwt_token = self.session.cookies.get('t')
                        if jwt_token:
                            self.log("JWT token received", "SUCCESS")
                        
                        # Return success response
                        return {
                            'user': {'email': email},
                            'status': 'authenticated',
                            'permissions': ['read', 'write'],
                            'message': 'Biometric authentication successful',
                            'status_code': check_response.status_code,
                            'has_jwt': jwt_token is not None
                        }
                
                await browser.close()
                raise Exception("Biometric authentication timed out")

        except Exception as e:
            self.log(f"❌ Biometric authentication failed: {str(e)}", "ERROR")
            raise
    
    async def is_authenticated(self) -> bool:
        """Check if currently authenticated using JWT token."""
        try:
            # Check if we have a JWT token in cookies
            jwt_token = self.session.cookies.get('t')
            if not jwt_token:
                self.log("❌ No JWT token found", "INFO")
                self._auth_validated_until = 0.0
                return False

            if time.time() < self._auth_validated_until:
                return True
            
            # Test authentication with a simple API call
            response = await self._request('GET', f"{self.base_url}/authentication")
            if response.status_code == 200:
                self._auth_validated_until = time.time() + self._auth_check_ttl_seconds
                return True
            elif response.status_code == 401:
                self.log("❌ JWT token expired or invalid (401)", "INFO")
                self._auth_validated_until = 0.0
                return False
            else:
                self.log(f"⚠️ Unexpected status code during auth check: {response.status_code}", "WARNING")
                self._auth_validated_until = 0.0
                return False
        except (TimeoutError, ConnectionError) as e:
            self.log(f"❌ Network error checking authentication: {str(e)}", "ERROR")
            return False
        except Exception as e:
            self.log(f"❌ Unexpected error checking authentication: {str(e)}", "ERROR")
            return False
    
    async def ensure_authenticated(self):
        """Ensure authentication is valid, re-authenticate if needed."""
        jwt_token = self.session.cookies.get('t')
        if jwt_token and time.time() < self._auth_validated_until:
            return

        async with self._auth_lock:
            # Double-check after waiting for another coroutine's auth refresh.
            jwt_token = self.session.cookies.get('t')
            if jwt_token and time.time() < self._auth_validated_until:
                return

            if jwt_token:
                try:
                    response = await self._request('GET', f"{self.base_url}/authentication")
                    if response.status_code == 200:
                        self._auth_validated_until = time.time() + self._auth_check_ttl_seconds
                        return
                    if response.status_code == 401:
                        self.log("❌ JWT token expired or invalid (401)", "INFO")
                    else:
                        self.log(f"⚠️ Unexpected status code during auth check: {response.status_code}", "WARNING")
                except (TimeoutError, ConnectionError) as e:
                    self.log(f"❌ Network error checking authentication: {str(e)}", "ERROR")

            self._auth_validated_until = 0.0
            if not self.auth_credentials:
                self.log("No credentials in memory, loading from config...", "INFO")
                config = load_config()
                creds = config.get("credentials", {})
                email = creds.get("email")
                password = creds.get("password")
                if not email or not password:
                    raise Exception("Authentication credentials not found in config. Please authenticate first.")
                self.auth_credentials = {'email': email, 'password': password}

            self.log("🔄 Re-authenticating...", "INFO")
            await self._authenticate_unlocked(self.auth_credentials['email'], self.auth_credentials['password'])
    
    async def get_authentication_status(self) -> Optional[Dict[str, Any]]:
        """Get current authentication status and user info."""
        try:
            response = await self._request('GET', f"{self.base_url}/users/self")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get auth status: {str(e)}", "ERROR")
            return None
    
    async def create_simulation(self, simulation_data: SimulationData) -> Dict[str, str]:
        """Create a new simulation on BRAIN platform."""
        await self._create_simulation_semaphore.acquire()
        try:
            await self.ensure_authenticated()
        
            self.log("🚀 Creating simulation...", "INFO")
            
            # Prepare settings based on simulation type
            settings_dict = simulation_data.settings.model_dump()
            
            # Remove fields based on simulation type
            if simulation_data.type == "REGULAR":
                # Remove SUPER-specific fields for REGULAR
                settings_dict.pop('selectionHandling', None)
                settings_dict.pop('selectionLimit', None)
                settings_dict.pop('componentActivation', None)
            
            # Filter out None values from settings
            settings_dict = {k: v for k, v in settings_dict.items() if v is not None}
            
            # Prepare simulation payload
            payload = {
                'type': simulation_data.type,
                'settings': settings_dict
            }
            
            # Add type-specific fields
            if simulation_data.type == "REGULAR":
                if simulation_data.regular:
                    payload['regular'] = simulation_data.regular
            elif simulation_data.type == "SUPER":
                if simulation_data.combo:
                    payload['combo'] = simulation_data.combo
                if simulation_data.selection:
                    payload['selection'] = simulation_data.selection
            
            # Filter out None values from entire payload
            payload = {k: v for k, v in payload.items() if v is not None}
            
            response = await self._request('POST', f"{self.base_url}/simulations", json=payload)
            if response.status_code >= 400:
                return {
                    "error": "Failed to create simulation",
                    "status_code": response.status_code,
                    "response": self._response_payload(response),
                    "request": {
                        "type": simulation_data.type,
                        "settings": settings_dict,
                        "has_regular": bool(simulation_data.regular),
                        "has_combo": bool(simulation_data.combo),
                        "has_selection": bool(simulation_data.selection),
                    },
                }
            
            location = response.headers.get('Location', '')
            location_url = self._to_absolute_url(location)
            simulation_id = location_url.split('/')[-1] if location_url else None
            
            self.log(f"Simulation created with ID: {simulation_id}", "SUCCESS")

            start_time = time.time()
            timeout_seconds = 1800  # 10 minutes
            max_poll_retries = 5  # Max retries for transient connection errors during polling
            poll_retry_delay = 3  # Initial delay between poll retries

            simulation_progress = None
            while True:
                # Check for timeout
                if time.time() - start_time > timeout_seconds:
                    raise TimeoutError(f"Simulation {simulation_id} timed out after {timeout_seconds} seconds")

                # Poll with retry logic for transient network errors
                poll_error = None
                for poll_attempt in range(max_poll_retries):
                    try:
                        simulation_progress = await self._request('GET', location_url)
                        poll_error = None
                        break  # Success, exit retry loop
                    except (ConnectionError, TimeoutError) as e:
                        poll_error = e
                        if poll_attempt < max_poll_retries - 1:
                            retry_wait = poll_retry_delay * (1.5 ** poll_attempt)
                            self.log(f"⚠️ Polling connection error for {simulation_id} (attempt {poll_attempt + 1}/{max_poll_retries}), retrying in {retry_wait:.1f}s: {str(e)}", "WARNING")
                            await asyncio.sleep(retry_wait)
                        else:
                            self.log(f"❌ Polling failed after {max_poll_retries} attempts for {simulation_id}: {str(e)}", "ERROR")
                
                if poll_error:
                    raise poll_error
                
                # Check if we need to wait
                retry_after = simulation_progress.headers.get("Retry-After")
                
                if not retry_after or float(retry_after) == 0:
                    break
                
                wait_time = float(retry_after)
                # Use asyncio.sleep instead of time.sleep to avoid blocking
                await asyncio.sleep(wait_time)

            self.log("Alpha done simulating, getting alpha details", "INFO")
            
            progress_data = simulation_progress.json()
            if not progress_data.get("alpha"):
                return {
                    "error": "Simulation failed or returned no alpha ID",
                    "message": self._simulation_error_message(progress_data),
                    "simulation_id": simulation_id,
                    "location": location,
                    "location_url": location_url,
                    "status": progress_data.get("status"),
                    "progress": progress_data,
                    "request": {
                        "type": simulation_data.type,
                        "settings": settings_dict,
                        "has_regular": bool(simulation_data.regular),
                        "has_combo": bool(simulation_data.combo),
                        "has_selection": bool(simulation_data.selection),
                    },
                }
                
            alpha_id = progress_data["alpha"]
            
            # Fetch alpha details with retry logic
            alpha_response = None
            for alpha_attempt in range(max_poll_retries):
                try:
                    alpha_response = await self._request('GET', f"https://api.worldquantbrain.com/alphas/{alpha_id}")
                    break
                except (ConnectionError, TimeoutError) as e:
                    if alpha_attempt < max_poll_retries - 1:
                        retry_wait = poll_retry_delay * (1.5 ** alpha_attempt)
                        self.log(f"⚠️ Failed to fetch alpha details (attempt {alpha_attempt + 1}/{max_poll_retries}), retrying in {retry_wait:.1f}s: {str(e)}", "WARNING")
                        await asyncio.sleep(retry_wait)
                    else:
                        self.log(f"❌ Failed to fetch alpha details after {max_poll_retries} attempts: {str(e)}", "ERROR")
                        raise
            
            return alpha_response.json()
            
        except Exception as e:
            self.log(f"❌ Failed to create simulation: {str(e)}", "ERROR")
            raise
        finally:
            self._create_simulation_semaphore.release()
    
    async def get_alpha_details(self, alpha_id: str) -> Dict[str, Any]:
        """Get detailed information about an alpha."""
        await self.ensure_authenticated()
        
        try:
            response = await self._request('GET', f"{self.base_url}/alphas/{alpha_id}")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get alpha details: {str(e)}", "ERROR")
            raise
    
    async def get_datasets(self, category: Optional[str] = None, region: str = "USA",
                          delay: int = 1, universe: str = "TOP3000", theme: str = "false", search: Optional[str] = None) -> Dict[str, Any]:
        """Get available datasets with Redis caching (1 day TTL) and fetch all data at once."""
        await self.ensure_authenticated()
        
        try:
            # Generate cache key from parameters (excluding search for cache key)
            cache_params = {
                'category': category,
                'region': region,
                'delay': delay,
                'universe': universe,
                'theme': theme
            }
            cache_key = self._generate_cache_key('datasets', cache_params)
            
            # Try to get from cache
            cached_data = self._get_cached_data(cache_key)
            if cached_data:
                # Apply search filter if needed
                if search:
                    filtered_results = [
                        item for item in cached_data.get('results', [])
                        if search.lower() in json.dumps(item).lower()
                    ]
                    return {
                        **cached_data,
                        'results': filtered_results,
                        'count': len(filtered_results),
                        'from_cache': True
                    }
                return {**cached_data, 'from_cache': True}
            
            # Fetch all data from API (pagination loop)
            all_results = []
            offset = 0
            limit = 50
            total_count = None
            
            while True:
                params = {
                    'category': category,
                    'region': region,
                    'delay': delay,
                    'universe': universe,
                    'theme': theme,
                    'limit': limit,
                    'offset': offset
                }
                
                data = await self._request_json_with_retries(
                    'GET',
                    f"{self.base_url}/data-sets",
                    params=params,
                    op_name=f"get_datasets(offset={offset})",
                )
                
                results = data.get('results', [])
                all_results.extend(results)
                
                if total_count is None:
                    total_count = data.get('count', 0)
                
                # Break if we've fetched all data
                if len(results) < limit or len(all_results) >= total_count:
                    break
                
                offset += limit
            
            # Prepare complete response
            complete_data = {
                'results': all_results,
                'count': len(all_results),
                'extraNote': "if your returned result is 0, you may want to check your parameter by using get_platform_setting_options tool to got correct parameter",
                'from_cache': False
            }
            
            # Cache the complete data (1 week TTL). Never cache empty result
            # sets: newly launched regions (e.g. GBR) can transiently return
            # count=0 from /data-sets while the platform index catches up, and
            # caching that makes the emptiness sticky for the whole TTL.
            if all_results:
                self._set_cached_data(cache_key, complete_data, ttl=604800)

            # Apply search filter if needed
            if search:
                filtered_results = [
                    item for item in all_results
                    if search.lower() in json.dumps(item).lower()
                ]
                complete_data['results'] = filtered_results
                complete_data['count'] = len(filtered_results)
            
            return complete_data
            
        except Exception as e:
            self.log(f"Failed to get datasets: {str(e)}", "ERROR")
            raise
    
    async def get_datafields(self, instrument_type: str = "EQUITY", region: str = "USA",
                            delay: int = 1, universe: str = "TOP3000", theme: str = "false",
                            dataset_id: Optional[str] = None, data_type: str = "",
                            search: Optional[str] = None,
                            filter_sharpe: bool = True) -> Dict[str, Any]:
        """Get available data fields with Redis caching (1 day TTL) and fetch all data at once.
        
        Search supports fuzzy matching across multiple fields:
        - Searches in: name, description, dataset.name, dataset.vendor, id
        - Multiple keywords (space-separated) use AND logic
        - Case-insensitive matching
        
        OS/IS Sharpe filtering (filter_sharpe=True by default):
        - Filters out datafields whose OS/IS Sharpe ratio < 0 to improve field quality
        - Uses pre-aggregated statistics from WebDataScope info_data.bin
        - Matching is done at datafield level first, then dataset level as fallback
        
        Examples:
        - search="price" -> matches any field containing "price"
        - search="stock volume" -> matches fields containing both "stock" AND "volume"
        """
        await self.ensure_authenticated()
        
        # Redis-based distributed lock for concurrency control (limit to 1)
        lock_key = "lock:get_datafields"
        lock_acquired = False
        lock_timeout = 300  # Lock expires after 5 minutes to prevent deadlock
        max_wait_time = 600  # Maximum wait time for acquiring lock (10 minutes)
        wait_interval = 2  # Check every 2 seconds
        
        if self.redis_client:
            start_wait = time.time()
            while time.time() - start_wait < max_wait_time:
                try:
                    # Try to acquire lock with NX (only set if not exists) and EX (expiration)
                    lock_acquired = self.redis_client.set(lock_key, "locked", ex=lock_timeout, nx=True)
                    if lock_acquired:
                        self.log(f"Acquired Redis lock for get_datafields", "INFO")
                        break
                    else:
                        # Lock is held by another process, wait and retry
                        ttl = self.redis_client.ttl(lock_key)
                        self.log(f"Waiting for get_datafields lock (TTL: {ttl}s)...", "INFO")
                        await asyncio.sleep(wait_interval)
                except Exception as e:
                    self.log(f"Redis lock acquisition failed: {str(e)}, proceeding without lock", "WARNING")
                    break
            
            if not lock_acquired and self.redis_client:
                self.log(f"Could not acquire get_datafields lock after {max_wait_time}s, proceeding anyway", "WARNING")
        
        try:
            def fuzzy_search_filter(item: Dict[str, Any], search_term: str) -> bool:
                """Enhanced fuzzy search across key fields with multi-keyword support."""
                if not search_term:
                    return True
                
                # Split search term into keywords (space-separated) for AND logic
                keywords = [kw.strip().lower() for kw in search_term.split() if kw.strip()]
                if not keywords:
                    return True
                
                # Extract searchable fields
                searchable_text_parts = []
                
                # Add field name
                if item.get('name'):
                    searchable_text_parts.append(str(item['name']))
                
                # Add field description
                if item.get('description'):
                    searchable_text_parts.append(str(item['description']))
                
                # Add field ID
                if item.get('id'):
                    searchable_text_parts.append(str(item['id']))
                
                # Add dataset information
                dataset = item.get('dataset', {})
                if isinstance(dataset, dict):
                    if dataset.get('name'):
                        searchable_text_parts.append(str(dataset['name']))
                    if dataset.get('vendor'):
                        searchable_text_parts.append(str(dataset['vendor']))
                    if dataset.get('id'):
                        searchable_text_parts.append(str(dataset['id']))
                
                # Combine all searchable text
                combined_text = ' '.join(searchable_text_parts).lower()
                
                # Check if ALL keywords match (AND logic)
                return all(keyword in combined_text for keyword in keywords)
            
            # Generate cache key from parameters (excluding search for cache key)
            cache_params = {
                'instrumentType': instrument_type,
                'region': region,
                'delay': delay,
                'universe': universe,
                'theme': theme,
                'dataset_id': dataset_id,
                'data_type': data_type
            }
            cache_key = self._generate_cache_key('datafields', cache_params)
            
            def sharpe_filter(items: list, rgn: str, dly: int) -> tuple:
                """Filter out datafields with OS/IS sharpe < 0. Returns (filtered_items, removed_count, applied)."""
                if not self._isos_data:
                    return items, 0, False
                region_key = f"{rgn}_{dly}"
                isos_info = self._isos_data.get(region_key, {})
                isos_section = isos_info.get('isos', {})
                datafield_sharpe = isos_section.get('datafield', {})
                dataset_sharpe_map = isos_section.get('dataset', {})
                if not datafield_sharpe and not dataset_sharpe_map:
                    return items, 0, False
                filtered = []
                for item in items:
                    field_name = item.get('id', '') or item.get('name', '')
                    dataset_info = item.get('dataset', {})
                    ds_id = dataset_info.get('id', '') if isinstance(dataset_info, dict) else ''
                    df_stats = datafield_sharpe.get(field_name)
                    if df_stats is not None:
                        sr = df_stats.get('sharpe_ratio')
                        if sr is not None and sr < 0:
                            continue
                    if df_stats is None and ds_id:
                        ds_stats = dataset_sharpe_map.get(ds_id)
                        if ds_stats is not None:
                            sr = ds_stats.get('sharpe_ratio')
                            if sr is not None and sr < 0:
                                continue
                    filtered.append(item)
                return filtered, len(items) - len(filtered), True

            # Try to get from cache
            cached_data = self._get_cached_data(cache_key)
            if cached_data:
                result = {**cached_data, 'from_cache': True}
                results = result.get('results', [])
                # Apply fuzzy search filter if needed
                if search:
                    results = [item for item in results if fuzzy_search_filter(item, search)]
                # Apply OS/IS Sharpe filtering
                if filter_sharpe:
                    results, removed, applied = sharpe_filter(results, region, delay)
                    result['sharpe_filter_applied'] = applied
                    result['sharpe_filter_removed'] = removed
                result['results'] = results
                result['count'] = len(results)
                return result
            
            # Fetch all data from API (pagination loop)
            all_results = []
            offset = 0
            limit = 50
            total_count = None
            
            while True:
                params = {
                    'instrumentType': instrument_type,
                    'region': region,
                    'delay': delay,
                    'universe': universe,
                    'limit': limit,
                    'offset': offset
                }
                
                if data_type != 'ALL' and data_type:
                    params['type'] = data_type
                
                if dataset_id:
                    params['dataset.id'] = dataset_id
                
                data = await self._request_json_with_retries(
                    'GET',
                    f"{self.base_url}/data-fields",
                    params=params,
                    op_name=f"get_datafields(offset={offset})",
                )
                
                results = data.get('results', [])
                # 等待2秒
                await asyncio.sleep(2)
                all_results.extend(results)
                
                if total_count is None:
                    total_count = data.get('count', 0)
                
                # Break if we've fetched all data
                if len(results) < limit or len(all_results) >= total_count:
                    break
                
                offset += limit
            
            # Prepare complete response
            complete_data = {
                'results': all_results,
                'count': len(all_results),
                'extraNote': "if your returned result is 0, you may want to check your parameter by using get_platform_setting_options tool to got correct parameter. Search supports fuzzy matching with multiple keywords (space-separated, AND logic).",
                'from_cache': False
            }
            
            # Cache the complete data (1 week TTL). Never cache empty result
            # sets (new-region index lag would otherwise stick for the TTL).
            if all_results:
                self._set_cached_data(cache_key, complete_data, ttl=604800)

            # Apply fuzzy search filter if needed
            if search:
                filtered_results = [
                    item for item in all_results
                    if fuzzy_search_filter(item, search)
                ]
                complete_data['results'] = filtered_results
                complete_data['count'] = len(filtered_results)
            
            # Apply OS/IS Sharpe ratio filtering
            if filter_sharpe:
                results, removed, applied = sharpe_filter(complete_data['results'], region, delay)
                complete_data['results'] = results
                complete_data['count'] = len(results)
                complete_data['sharpe_filter_applied'] = applied
                complete_data['sharpe_filter_removed'] = removed
                if applied:
                    self.log(f"Sharpe filter ({region}_{delay}): removed {removed}/{removed + len(results)} fields with OS/IS sharpe < 0", "INFO")
            
            return complete_data
            
        except Exception as e:
            self.log(f"Failed to get datafields: {str(e)}", "ERROR")
            raise
        finally:
            # Release Redis lock if acquired
            if lock_acquired and self.redis_client:
                try:
                    self.redis_client.delete(lock_key)
                    self.log(f"Released Redis lock for get_datafields", "INFO")
                except Exception as e:
                    self.log(f"Failed to release Redis lock: {str(e)}", "WARNING")
    
    async def get_alpha_pnl(self, alpha_id: str) -> Dict[str, Any]:
        """Get PnL data for an alpha with retry logic."""
        await self.ensure_authenticated()
        
        max_retries = 5
        retry_delay = 2  # seconds
        
        for attempt in range(max_retries):
            try:
                self.log(f"Attempting to get PnL for alpha {alpha_id} (attempt {attempt + 1}/{max_retries})", "INFO")
                
                response = await self._request('GET', f"{self.base_url}/alphas/{alpha_id}/recordsets/pnl")
                response.raise_for_status()
                
                text = (response.text or "").strip()
                if not text:
                    if attempt < max_retries - 1:
                        self.log(f"Empty PnL response for {alpha_id}, retrying in {retry_delay} seconds...", "WARNING")
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 1.5
                        continue
                    else:
                        self.log(f"Empty PnL response after {max_retries} attempts for {alpha_id}", "WARNING")
                        return {}
                
                try:
                    pnl_data = response.json()
                    if pnl_data:
                        self.log(f"Successfully retrieved PnL data for alpha {alpha_id}", "SUCCESS")
                        return pnl_data
                    else:
                        if attempt < max_retries - 1:
                            self.log(f"Empty PnL JSON for {alpha_id}, retrying in {retry_delay} seconds...", "WARNING")
                            await asyncio.sleep(retry_delay)
                            retry_delay *= 1.5
                            continue
                        else:
                            self.log(f"Empty PnL JSON after {max_retries} attempts for {alpha_id}", "WARNING")
                            return {}
                            
                except json.JSONDecodeError as parse_err:
                    if attempt < max_retries - 1:
                        self.log(f"PnL JSON parse failed for {alpha_id} (attempt {attempt + 1}), retrying in {retry_delay} seconds...", "WARNING")
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 1.5
                        continue
                    else:
                        self.log(f"PnL JSON parse failed for {alpha_id} after {max_retries} attempts: {parse_err}", "WARNING")
                        return {}
                        
            except requests.RequestException as e:
                if attempt < max_retries - 1:
                    self.log(f"Failed to get alpha PnL for {alpha_id} (attempt {attempt + 1}), retrying in {retry_delay} seconds: {str(e)}", "WARNING")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 1.5
                    continue
                else:
                    self.log(f"Failed to get alpha PnL for {alpha_id} after {max_retries} attempts: {str(e)}", "ERROR")
                    raise
        
        return {}
    
    @staticmethod
    def _normalize_brain_datetime(value: str) -> str:
        """BRAIN date filters require ISO 8601 datetimes WITH timezone.

        The API returns 400 ['Expected ISO 8601 datetime with timezone'] for
        naive values, so date-only strings get 'T00:00:00' appended and
        timezone-less datetimes get 'Z' appended.
        """
        v = (value or '').strip()
        if not v:
            return v
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            v += "T00:00:00"
        if re.search(r"(Z|[+-]\d{2}:?\d{2})$", v):
            return v
        return v + "Z"

    async def get_user_alphas(
        self,
        stage: str = "OS",
        limit: int = 30,
        offset: int = 0,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        submission_start_date: Optional[str] = None,
        submission_end_date: Optional[str] = None,
        order: Optional[str] = None,
        hidden: Optional[bool] = None,
        region: Optional[str] = None,
        status: Optional[str] = None,
        alpha_type: Optional[str] = None,
        is_super: Optional[bool] = None,
        color: Optional[str] = None,
        name: Optional[str] = None,
        tag: Optional[str] = None,
        language: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get user's alphas with server-side filtering and Redis caching (1 day TTL).

        All filters are applied server-side by the BRAIN API (verified by live
        probes against /users/self/alphas):
        - region      -> settings.region=<REGION>  (a bare 'region' param is silently ignored)
        - status      -> status=<STATUS>
        - alpha_type  -> type=<TYPE>
        - is_super    -> type=SUPER (True) / type!=SUPER (False)
        - color       -> color=<COLOR>  (case-sensitive uppercase: RED/GREEN/BLUE/YELLOW/PURPLE)
        - name        -> name=<NAME>    (exact match only; substring operators don't exist)
        - tag         -> tag=<TAG>      (singular 'tag'; a 'tags' param is silently ignored)
        - dates       -> dateCreated>/dateCreated</dateSubmitted>/dateSubmitted<
                         (values MUST be ISO 8601 with timezone; naive values are
                         normalized via _normalize_brain_datetime; '>=' does not exist)
        - order supports nested fields too (e.g. '-is.sharpe'), hidden is true/false
        """
        await self.ensure_authenticated()

        try:
            api_params: Dict[str, Any] = {"stage": stage}
            if start_date:
                api_params["dateCreated>"] = self._normalize_brain_datetime(start_date)
            if end_date:
                api_params["dateCreated<"] = self._normalize_brain_datetime(end_date)
            if submission_start_date:
                api_params["dateSubmitted>"] = self._normalize_brain_datetime(submission_start_date)
            if submission_end_date:
                api_params["dateSubmitted<"] = self._normalize_brain_datetime(submission_end_date)
            if order:
                api_params["order"] = order
            if hidden is not None:
                api_params["hidden"] = str(hidden).lower()
            if region:
                api_params["settings.region"] = region.upper()
            if status:
                api_params["status"] = status.upper()
            if color:
                api_params["color"] = color.upper()
            if name:
                api_params["name"] = name
            if tag:
                api_params["tag"] = tag
            if language:
                api_params["settings.language"] = language.upper()
            if alpha_type:
                api_params["type"] = alpha_type.upper()
            if is_super is not None:
                if alpha_type and (alpha_type.upper() == 'SUPER') != is_super:
                    # Contradictory filters can never match anything
                    return {
                        'count': 0, 'next': None, 'previous': None, 'results': [],
                        'from_cache': False,
                        'note': f"type={alpha_type} contradicts is_super={is_super}",
                    }
                if is_super:
                    api_params["type"] = "SUPER"
                elif not alpha_type:
                    api_params["type!"] = "SUPER"

            api_params["limit"] = limit
            api_params["offset"] = offset

            cache_key = self._generate_cache_key('user_alphas', api_params)
            cached_data = self._get_cached_data(cache_key)
            if cached_data:
                return {**cached_data, 'from_cache': True}

            data = await self._request_json_with_retries(
                'GET',
                f"{self.base_url}/users/self/alphas",
                params=api_params,
                op_name=f"get_user_alphas(stage={stage}, offset={offset})",
            )

            data['from_cache'] = False
            self._set_cached_data(cache_key, data, ttl=86400)
            return data

        except requests.HTTPError as e:
            # Surface the API's error body (e.g. ['Expected ISO 8601 datetime with timezone'])
            body = ''
            try:
                body = (e.response.text or '')[:300]
            except Exception:
                pass
            self.log(f"Failed to get user alphas: {e} body={body}", "ERROR")
            raise requests.HTTPError(f"{e} — {body}", response=e.response) from e
        except Exception as e:
            self.log(f"Failed to get user alphas: {str(e)}", "ERROR")
            raise
    
    def pre_submit_check(self, alpha_details: Dict[str, Any]) -> Dict[str, Any]:
        """Check IS metrics against submission thresholds before submitting.

        Criteria:
        - Sharpe > 1.3 and Fitness > 0.75 (relaxed thresholds for pre-submission check)
        - Margin > 0.05% for USA, otherwise > 0.15% (hard floor 0.08%)
        - Turnover between 4% and 40%
        - Returns > 4%
        - All other IS checks must PASS (no FAIL)
        """
        is_data = alpha_details.get('is')
        if not is_data:
            return {'passed': False, 'reason': 'No IS data available for this alpha. Simulation may not be complete.', 'details': []}

        failures = []
        warnings = []

        sharpe = is_data.get('sharpe', 0)
        fitness = is_data.get('fitness', 0)
        margin = is_data.get('margin', 0)
        turnover = is_data.get('turnover', 0)
        returns = is_data.get('returns', 0)
        drawdown = is_data.get('drawdown', 0)
        settings = alpha_details.get('settings') or {}
        region = (settings.get('region') or alpha_details.get('region') or '').upper()

        # Sharpe > 1.3
        if sharpe <= 1.3:
            failures.append(f'Sharpe {sharpe} <= 1.3 (required > 1.3)')

        # Fitness > 0.75
        if fitness <= 0.75:
            failures.append(f'Fitness {fitness} <= 0.75 (required > 0.75)')

        # USA margin rule is relaxed to >5bp. Other regions keep the >15bp target with a 8bp hard floor.
        if region == 'USA':
            if margin <= 0.0005:
                failures.append(f'Margin {margin*100:.4f}% <= 5bp (required > 5bp for USA)')
        else:
            if margin <= 0.0008:
                failures.append(f'Margin {margin*100:.4f}% <= 8bp (hard floor, required > 15bp)')
            elif margin <= 0.0015:
                warnings.append(f'Margin {margin*100:.4f}% <= 15bp (recommended > 15bp, current above 8bp hard floor)')

        # Turnover between 4% and 40%
        if turnover < 0.04:
            failures.append(f'Turnover {turnover*100:.2f}% < 4% (required 4%-40%)')
        elif turnover > 0.40:
            failures.append(f'Turnover {turnover*100:.2f}% > 40% (required 4%-40%)')

        # Returns > 4%
        if returns <= 0.04:
            failures.append(f'Returns {returns*100:.2f}% <= 4% (required > 4%)')

        # # Returns > drawdown
        # if returns <= drawdown:
        #     failures.append(f'Returns {returns*100:.2f}% <= Drawdown {drawdown*100:.2f}% (required Returns > Drawdown)')

        # All other IS checks must not be FAIL
        checks = is_data.get('checks', [])
        for chk in checks:
            result = chk.get('result', '')
            name = chk.get('name', 'UNKNOWN')
            if result == 'FAIL':
                value = chk.get('value', 'N/A')
                limit = chk.get('limit', 'N/A')
                failures.append(f'IS check {name} FAILED (value={value}, limit={limit})')

        passed = len(failures) == 0
        return {
            'passed': passed,
            'failures': failures,
            'warnings': warnings,
            'metrics': {
                'region': region or None,
                'sharpe': sharpe,
                'fitness': fitness,
                'margin': margin,
                'margin_bp': round(margin * 10000, 2),
                'turnover': turnover,
                'returns': returns,
                'drawdown': drawdown,
            },
            'is_checks_summary': [
                {'name': c.get('name'), 'result': c.get('result'), 'value': c.get('value'), 'limit': c.get('limit')}
                for c in checks
            ],
        }

    async def submit_alpha(self, alpha_id: str, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Submit an alpha for production and wait for the platform-side check to finish.

        Implements the correct submit flow from submit.py:
        1. POST to /alphas/{alpha_id}/submit
        2. If response has Retry-After header, switch to GET polling until no more retry-after
        3. Non-200/403 responses retry after 2 minutes
        4. Parses response JSON to check IS checks for ALREADY_SUBMITTED and FAILs

        The whole flow can take from a few minutes up to ~1 hour, bounded by
        BRAIN_SUBMIT_MAX_SECONDS (default 5400s). Progress is written into the
        optional `state` dict so callers can observe it concurrently.

        Returns a dict with at least {'status': 'SUCCESS' | 'ALREADY_SUBMITTED' |
        'FAILED' | 'TIMEOUT', 'message': str}.
        """
        await self.ensure_authenticated()

        submit_url = f"{self.base_url}/alphas/{alpha_id}/submit"
        attempt = 0
        deadline = time.monotonic() + self._submit_max_seconds

        def _update(phase: str, **extra: Any) -> None:
            if state is not None:
                state['phase'] = phase
                state['updated_at'] = time.time()
                state.update(extra)

        while True:
            if time.monotonic() > deadline:
                msg = f"Submission for {alpha_id} exceeded {self._submit_max_seconds:.0f}s without a final result"
                self.log(msg, "ERROR")
                return {'status': 'TIMEOUT', 'message': msg, 'attempts': attempt}

            attempt += 1
            self.log(f"Submit attempt {attempt} for alpha {alpha_id}", "INFO")
            _update('posting', attempts=attempt)

            try:
                response = await self._request('POST', submit_url)
            except Exception as e:
                self.log(f"Submit POST failed for {alpha_id}: {e}", "ERROR")
                raise

            self.log(f"Alpha submit, alpha_id={alpha_id}, status_code={response.status_code}", "INFO")
            _update('posted', last_status_code=response.status_code)

            # Handle Retry-After header: switch to GET polling
            polls = 0
            while 'retry-after' in {k.lower() for k in response.headers}:
                if time.monotonic() > deadline:
                    msg = f"Submission polling for {alpha_id} exceeded {self._submit_max_seconds:.0f}s"
                    self.log(msg, "ERROR")
                    return {'status': 'TIMEOUT', 'message': msg, 'attempts': attempt}
                retry_after_raw = response.headers.get('Retry-After') or response.headers.get('retry-after', '5')
                try:
                    wait_time = float(retry_after_raw)
                except ValueError:
                    wait_time = 5.0
                # Match reference: 5x multiplier for short waits
                actual_wait = 5 * wait_time if wait_time < 60 else wait_time
                polls += 1
                self.log(f"Submission processing (Retry-After={retry_after_raw}s), waiting {actual_wait:.0f}s then GET polling...", "INFO")
                _update('polling', polls=polls, retry_after_seconds=actual_wait)
                await asyncio.sleep(actual_wait)
                try:
                    response = await self._request('GET', submit_url)
                    self.log(f"GET poll response, alpha_id={alpha_id}, status_code={response.status_code}", "INFO")
                    _update('polling', last_status_code=response.status_code)
                except Exception as e:
                    self.log(f"Submit GET poll failed for {alpha_id}: {e}", "ERROR")
                    raise

            if response.status_code == 200:
                # Parse response JSON to validate IS checks
                try:
                    res_json = response.json()
                except (json.JSONDecodeError, ValueError):
                    msg = f"Submit response for {alpha_id} is not valid JSON: {(response.text or '')[:200]}"
                    self.log(msg, "ERROR")
                    return {'status': 'FAILED', 'message': msg, 'attempts': attempt}

                if not res_json:
                    return {'status': 'FAILED', 'message': 'Empty submit response body', 'attempts': attempt}

                if 'detail' in res_json and res_json['detail'] == 'Not found.':
                    msg = f"Submit failed: alpha {alpha_id} not found"
                    self.log(msg, "ERROR")
                    return {'status': 'FAILED', 'message': msg, 'attempts': attempt}

                # Check IS checks in response
                if 'is' in res_json and 'checks' in res_json['is']:
                    failed_checks = []
                    for item in res_json['is']['checks']:
                        if item.get('name') == 'ALREADY_SUBMITTED':
                            self.log(f"Alpha {alpha_id} already submitted", "WARNING")
                            return {'status': 'ALREADY_SUBMITTED',
                                    'message': f'Alpha {alpha_id} was already submitted',
                                    'attempts': attempt}
                        if item.get('result') == 'FAIL':
                            self.log(f"Alpha {alpha_id} IS check failed: {item.get('name')} limit={item.get('limit')} value={item.get('value')}", "ERROR")
                            failed_checks.append({'name': item.get('name'), 'limit': item.get('limit'), 'value': item.get('value')})
                    if failed_checks:
                        return {'status': 'FAILED',
                                'message': f"Submission checks failed for {alpha_id}",
                                'failed_checks': failed_checks,
                                'attempts': attempt}

                self.log(f"Alpha {alpha_id} submission successful!", "INFO")
                return {'status': 'SUCCESS',
                        'message': f'Alpha {alpha_id} submitted successfully',
                        'attempts': attempt}

            elif response.status_code == 403:
                self.log(f"Submit forbidden (403) for alpha {alpha_id}", "ERROR")
                return {'status': 'FAILED',
                        'message': f'Submit forbidden (403) for alpha {alpha_id}',
                        'attempts': attempt}

            else:
                self.log(f"Submit failed status={response.status_code} for {alpha_id}, waiting 2 minutes before retry...", "WARNING")
                _update('retry_wait', last_status_code=response.status_code)
                await asyncio.sleep(120)

    # --- Asynchronous submission management ---

    _SUBMISSION_TERMINAL_STATUSES = {'SUCCESS', 'ALREADY_SUBMITTED', 'FAILED', 'TIMEOUT', 'ERROR'}

    def _submission_state_snapshot(self, alpha_id: str) -> Optional[Dict[str, Any]]:
        state = self._submission_states.get(alpha_id)
        if state is None:
            return None
        snap = dict(state)
        started = snap.get('started_at')
        if started:
            snap['elapsed_seconds'] = round(time.time() - started, 1)
        return snap

    def start_submission(self, alpha_id: str, pre_check: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Start a background submission task for an alpha; returns immediately.

        Submissions are serialized (the platform runs one submission check at a
        time), so a second submission started while another is running waits in
        QUEUED state.
        """
        existing = self._submission_tasks.get(alpha_id)
        if existing is not None and not existing.done():
            return {
                'status': 'ALREADY_RUNNING',
                'message': f'A submission for {alpha_id} is already in progress',
                'state': self._submission_state_snapshot(alpha_id),
            }

        self._submission_states[alpha_id] = {
            'alpha_id': alpha_id,
            'status': 'QUEUED',
            'phase': 'queued',
            'attempts': 0,
            'polls': 0,
            'started_at': time.time(),
            'updated_at': time.time(),
            'finished_at': None,
            'result': None,
            'error': None,
            'pre_check': pre_check,
        }
        task = asyncio.create_task(self._run_submission(alpha_id))
        self._submission_tasks[alpha_id] = task
        return {'status': 'STARTED', 'state': self._submission_state_snapshot(alpha_id)}

    async def _run_submission(self, alpha_id: str) -> None:
        state = self._submission_states[alpha_id]
        try:
            async with self._submission_serialize_lock:
                state['status'] = 'RUNNING'
                state['updated_at'] = time.time()
                result = await self.submit_alpha(alpha_id, state=state)
            state['result'] = result
            state['status'] = result.get('status', 'ERROR')
        except Exception as e:
            state['status'] = 'ERROR'
            state['error'] = str(e)
            self.log(f"Background submission for {alpha_id} crashed: {e}", "ERROR")
        finally:
            state['finished_at'] = time.time()
            state['updated_at'] = time.time()

    async def get_submission_status(self, alpha_id: str) -> Dict[str, Any]:
        """Report the status of a submission started via start_submission.

        Falls back to a live platform probe (alpha stage / dateSubmitted) when
        there is no in-memory record, e.g. after a server restart.
        """
        snap = self._submission_state_snapshot(alpha_id)
        if snap is not None:
            done = snap.get('status') in self._SUBMISSION_TERMINAL_STATUSES
            return {'source': 'tracker', 'done': done, **snap}

        # No in-memory record — probe the platform directly
        details = await self.get_alpha_details(alpha_id)
        stage = details.get('stage')
        date_submitted = details.get('dateSubmitted')
        submitted = stage == 'OS' or bool(date_submitted)
        return {
            'source': 'live',
            'done': submitted,
            'alpha_id': alpha_id,
            'status': 'SUCCESS' if submitted else 'UNKNOWN',
            'stage': stage,
            'date_submitted': date_submitted,
            'message': (
                f'Alpha {alpha_id} is submitted (stage={stage})' if submitted
                else f'No tracked submission for {alpha_id} in this server session and the alpha is not submitted (stage={stage}). '
                     f'Call submit_alpha to start a submission.'
            ),
        }
    
    async def get_events(self) -> Dict[str, Any]:
        """Get available events and competitions."""
        await self.ensure_authenticated()
        
        try:
            response = await self._request('GET', f"{self.base_url}/events")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get events: {str(e)}", "ERROR")
            raise
    
    async def get_leaderboard(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """Get leaderboard data."""
        await self.ensure_authenticated()
        
        try:
            params = {}
            
            if user_id:
                params['user'] = user_id
            else:
                # Get current user ID if not specified
                user_response = await self._request('GET', f"{self.base_url}/users/self")
                if user_response.status_code == 200:
                    user_data = user_response.json()
                    params['user'] = user_data.get('id')
            
            response = await self._request('GET', f"{self.base_url}/consultant/boards/leader", params=params)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get leaderboard: {str(e)}", "ERROR")
            raise

    # --- SPC (Systematic Predictions Challenge) ---

    SPC_MODELS = ("gpt", "claude", "gemini", "deepseek", "kimi", "qwen", "glm", "llama", "minimax", "mistral")
    SPC_FREQUENCIES = ("daily", "weekly", "monthly", "quarterly")
    _SPC_ISIN_MIC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]\|[A-Z0-9]{4}$")

    @staticmethod
    def _spc_isin_checksum_valid(isin: str) -> bool:
        expanded = ""
        for char in isin:
            if char.isdigit():
                expanded += char
            elif "A" <= char <= "Z":
                expanded += str(ord(char) - ord("A") + 10)
            else:
                return False
        total = 0
        double = False
        for digit_char in reversed(expanded):
            digit = int(digit_char)
            if double:
                digit *= 2
            total += digit // 10 + digit % 10
            double = not double
        return total % 10 == 0

    def _validate_spc_sample_output(self, sample_output: str) -> List[str]:
        """Validate an SPC sample output string against the competition contract.

        Checks: parseable JSON object, ISIN|MIC key format, ISIN checksum,
        numeric confidence scores within [-1, 1]. Returns a list of error strings.
        """
        errors: List[str] = []
        stripped = (sample_output or "").strip()
        if not stripped:
            return ["sample_output is empty"]
        if stripped.startswith("```") or stripped.endswith("```"):
            errors.append("sample_output contains markdown code fences; must be pure JSON")
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            errors.append(f"sample_output is not valid JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}")
            return errors
        if not isinstance(data, dict):
            errors.append("sample_output top-level value must be a JSON object")
            return errors
        if not data:
            errors.append("sample_output object must not be empty")
        for key, value in data.items():
            if not self._SPC_ISIN_MIC_RE.match(str(key)):
                errors.append(f"invalid key format, expected ISIN|MIC: {key!r}")
                continue
            isin = str(key).split("|", 1)[0]
            if not self._spc_isin_checksum_valid(isin):
                errors.append(f"invalid ISIN checksum: {isin}")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(f"confidence score must be numeric for {key!r}")
            elif not math.isfinite(value):
                errors.append(f"confidence score must be finite for {key!r}")
            elif value < -1.0 or value > 1.0:
                errors.append(f"confidence score out of [-1, 1] for {key!r}: {value}")
        return errors

    def _validate_spc_fields(
        self,
        name: Optional[str] = None,
        prompt: Optional[str] = None,
        model: Optional[str] = None,
        weight: Optional[float] = None,
        update_frequency: Optional[str] = None,
    ) -> List[str]:
        """Validate SPC submission metadata fields. Only non-None fields are checked."""
        errors: List[str] = []
        if name is not None and len(name) > 200:
            errors.append(f"name exceeds 200 characters ({len(name)})")
        if prompt is not None:
            if not prompt.strip():
                errors.append("prompt is empty")
            if len(prompt) > 10000:
                errors.append(f"prompt exceeds 10000 characters ({len(prompt)})")
        if model is not None and model not in self.SPC_MODELS:
            errors.append(f"model must be one of {list(self.SPC_MODELS)}, got {model!r}")
        if update_frequency is not None and update_frequency not in self.SPC_FREQUENCIES:
            errors.append(f"update_frequency must be one of {list(self.SPC_FREQUENCIES)}, got {update_frequency!r}")
        if weight is not None and not (0.0 <= float(weight) <= 1.0):
            errors.append(f"weight must be between 0 and 1, got {weight}")
        return errors

    async def get_spc_submissions(self, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        """List the current user's SPC prompt submissions."""
        await self.ensure_authenticated()
        try:
            response = await self._request(
                'GET',
                f"{self.base_url}/competitions/spc/submissions",
                params={"limit": limit, "offset": offset},
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get SPC submissions: {str(e)}", "ERROR")
            raise

    async def create_spc_submission(
        self,
        name: str,
        prompt: str,
        sample_output: str,
        model: str,
        model_version: str,
        weight: float,
        update_frequency: str,
        skip_validation: bool = False,
    ) -> Dict[str, Any]:
        """Create a new SPC prompt submission."""
        await self.ensure_authenticated()
        if not skip_validation:
            errors = self._validate_spc_fields(name, prompt, model, weight, update_frequency)
            errors += self._validate_spc_sample_output(sample_output)
            if errors:
                return {
                    "error": "Local validation failed; nothing was submitted",
                    "validation_errors": errors,
                    "hint": "Fix the errors or pass skip_validation=true to submit anyway",
                }
        payload = {
            "name": name,
            "prompt": prompt,
            "sampleOutput": sample_output,
            "model": model,
            "modelVersion": model_version,
            "weight": round(float(weight), 2),
            "updateFrequency": update_frequency,
        }
        try:
            response = await self._request(
                'POST', f"{self.base_url}/competitions/spc/submissions", json=payload
            )
            if response.status_code == 400:
                return {"error": "Server rejected the submission", "details": self._response_payload(response)}
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to create SPC submission: {str(e)}", "ERROR")
            raise

    async def set_spc_submission_weight(self, submission_id: str, weight: float) -> Dict[str, Any]:
        """Set the weight of an existing SPC submission. weight=0 withdraws the prompt.

        The API only allows changing weight after creation; all other fields are
        immutable. To change a prompt's content, create a new submission and set
        the old one's weight to 0.
        """
        await self.ensure_authenticated()
        errors = self._validate_spc_fields(weight=weight)
        if errors:
            return {"error": "Local validation failed; nothing was updated", "validation_errors": errors}
        try:
            response = await self._request(
                'PATCH',
                f"{self.base_url}/competitions/spc/submissions/{submission_id}",
                json={"weight": round(float(weight), 2)},
            )
            if response.status_code == 400:
                return {"error": "Server rejected the update", "details": self._response_payload(response)}
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to update SPC submission {submission_id}: {str(e)}", "ERROR")
            raise

    async def get_spc_leaderboard(
        self,
        board: Optional[str] = None,
        limit: int = 30,
        offset: int = 0,
        aggregate: str = "user",
    ) -> Dict[str, Any]:
        """Get the SPC leaderboard. board is a month key like '202607' (defaults to current month server-side)."""
        await self.ensure_authenticated()
        params: Dict[str, Any] = {"limit": limit, "offset": offset, "aggregate": aggregate}
        if board:
            params["board"] = board
        try:
            response = await self._request(
                'GET', f"{self.base_url}/consultant/boards/spc", params=params
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get SPC leaderboard: {str(e)}", "ERROR")
            raise

    def _is_atom(self, detail: Optional[Dict[str, Any]]) -> bool:
        """Match atom detection used in extract_regular_alphas.py:
        - Primary signal: 'classifications' entries containing 'SINGLE_DATA_SET'
        - Fallbacks: tags list contains 'atom' or classification id/name contains 'ATOM'
        """
        if not detail or not isinstance(detail, dict):
            return False

        classifications = detail.get('classifications') or []
        for c in classifications:
            cid = (c.get('id') or c.get('name') or '')
            if isinstance(cid, str) and 'SINGLE_DATA_SET' in cid:
                return True

        # Fallbacks
        tags = detail.get('tags') or []
        if isinstance(tags, list):
            for t in tags:
                if isinstance(t, str) and t.strip().lower() == 'atom':
                    return True

        for c in classifications:
            cid = (c.get('id') or c.get('name') or '')
            if isinstance(cid, str) and 'ATOM' in cid.upper():
                return True

        return False

    async def value_factor_trendScore(self, start_date: str, end_date: str) -> Dict[str, Any]:
        """Compute diversity score for regular alphas in a date range.

        Description:
        This function calculate the diversity of the users' submission, by checking the diversity, we can have a good understanding on the valuefactor's trend.
        value factor of a user is defiend by This diversity score, which measures three key aspects of work output: the proportion of works
        with the "Atom" tag (S_A), atom proportion, the breadth of pyramids covered (S_P), and how evenly works
        are distributed across those pyramids (S_H). Calculated as their product, it rewards
        strong performance across all three dimensions—encouraging more Atom-tagged works,
        wider pyramid coverage, and balanced distribution—with weaknesses in any area lowering
        the total score significantly.

        Inputs (hints for AI callers):
        - start_date (str): ISO UTC start datetime, e.g. '2025-08-14T00:00:00Z'
        - end_date (str): ISO UTC end datetime, e.g. '2025-08-18T23:59:59Z'
        - Note: this tool always uses 'OS' (submission dates) to define the window; callers do not need to supply a stage.
                - Note: P_max (total number of possible pyramids) is derived from the platform
                    pyramid-multipliers endpoint and not supplied by callers.

        Returns (compact JSON): {
            'diversity_score': float,
            'N': int,  # total regular alphas in window
            'A': int,  # number of Atom-tagged works (is_single_data_set)
            'P': int,  # pyramid coverage count in the sample
            'P_max': int, # used max for normalization
            'S_A': float, 'S_P': float, 'S_H': float,
            'per_pyramid_counts': {pyramid_name: count}
        }
        """
        # Fetch user alphas (always use OS / submission dates per product policy)
        await self.ensure_authenticated()
        alphas_resp = await self.get_user_alphas(stage='OS', limit=500, submission_start_date=start_date, submission_end_date=end_date)

        if not isinstance(alphas_resp, dict) or 'results' not in alphas_resp:
            return {'error': 'Unexpected response from get_user_alphas', 'raw': alphas_resp}

        alphas = alphas_resp['results']
        regular = [a for a in alphas if a.get('type') == 'REGULAR']

        # Fetch details for each regular alpha
        pyramid_list = []
        atom_count = 0
        per_pyramid = {}
        for a in regular:
            try:
                detail = await self.get_alpha_details(a.get('id'))
            except Exception:
                continue

            is_atom = self._is_atom(detail)
            if is_atom:
                atom_count += 1

            # Extract pyramids
            ps = []
            if isinstance(detail.get('pyramids'), list):
                ps = [p.get('name') for p in detail.get('pyramids') if p.get('name')]
            else:
                pt = detail.get('pyramidThemes') or {}
                pss = pt.get('pyramids') if isinstance(pt, dict) else None
                if pss and isinstance(pss, list):
                    ps = [p.get('name') for p in pss if p.get('name')]

            for p in ps:
                pyramid_list.append(p)
                per_pyramid[p] = per_pyramid.get(p, 0) + 1

        N = len(regular)
        A = atom_count
        P = len(per_pyramid)

        # Determine P_max similarly to the script: use pyramid multipliers if available
        P_max = None
        try:
            pm = await self.get_pyramid_multipliers()
            if isinstance(pm, dict) and 'pyramids' in pm:
                pyramids_list = pm.get('pyramids') or []
                P_max = len(pyramids_list)
        except Exception:
            P_max = None

        if not P_max or P_max <= 0:
            P_max = max(P, 1)

        # Component scores
        S_A = (A / N) if N > 0 else 0.0
        S_P = (P / P_max) if P_max > 0 else 0.0

        # Entropy
        S_H = 0.0
        if P <= 1 or not per_pyramid:
            S_H = 0.0
        else:
            total_occ = sum(per_pyramid.values())
            H = 0.0
            for cnt in per_pyramid.values():
                q = cnt / total_occ if total_occ > 0 else 0
                if q > 0:
                    H -= q * math.log2(q)
            max_H = math.log2(P) if P > 0 else 1
            S_H = (H / max_H) if max_H > 0 else 0.0

        diversity_score = S_A * S_P * S_H

        return {
            'diversity_score': diversity_score,
            'N': N,
            'A': A,
            'P': P,
            'P_max': P_max,
            'S_A': S_A,
            'S_P': S_P,
            'S_H': S_H,
            'per_pyramid_counts': per_pyramid
        }

    async def get_operators(self) -> Dict[str, Any]:
        """Get available operators for alpha creation."""
        await self.ensure_authenticated()
        
        try:
            response = await self._request('GET', f"{self.base_url}/operators")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get operators: {str(e)}", "ERROR")
            raise

    async def recommend_datasets(self, region: str = "USA", delay: int = 1,
                                  universe: str = "TOP3000", top_n: int = 20) -> Dict[str, Any]:
        """Recommend datasets with unlit pyramid priority and in-pyramid quality ranking.

        The ranking favors datasets from unlit pyramids first. Within those
        pyramids it prefers high OS/IS Sharpe, high dataset userCount, and high
        dataset alphaCount, with a small random component to avoid returning the
        exact same list on every call.
        """
        await self.ensure_authenticated()

        def _category_id(value: Any) -> str:
            if isinstance(value, dict):
                return str(value.get('id') or '')
            return str(value or '')

        def _category_name(value: Any) -> str:
            if isinstance(value, dict):
                return str(value.get('name') or value.get('id') or '')
            return str(value or '')

        def _as_float(value: Any, default: float = 0.0) -> float:
            try:
                if value is None:
                    return default
                return float(value)
            except (TypeError, ValueError):
                return default

        def _as_int(value: Any, default: int = 0) -> int:
            try:
                if value is None:
                    return default
                return int(float(value))
            except (TypeError, ValueError):
                return default

        def _rank_score(value: Optional[float], values: List[float], points: float) -> float:
            """Return 0..points based on value's rank in the supplied sample."""
            if value is None or not values:
                return 0.0
            sorted_values = sorted(values)
            if len(sorted_values) == 1:
                return points
            below_or_equal = sum(1 for item in sorted_values if item <= value)
            percentile = (below_or_equal - 1) / (len(sorted_values) - 1)
            return points * max(0.0, min(1.0, percentile))

        def _log_score(value: int, values: List[int], points: float) -> float:
            if not values:
                return 0.0
            log_values = [math.log1p(max(0, item)) for item in values]
            return _rank_score(math.log1p(max(0, value)), log_values, points)

        def _region_delay_match(item: Dict[str, Any]) -> bool:
            return item.get('region') == region and _as_int(item.get('delay'), -1) == delay

        def _platform_row_match(item: Dict[str, Any], require_universe: bool = True) -> bool:
            if item.get('InstrumentType') != 'EQUITY':
                return False
            if item.get('Region') != region:
                return False
            if _as_int(item.get('Delay'), -1) != delay:
                return False
            if not require_universe:
                return True
            return universe in (item.get('Universe') or [])

        # ---- 1. Fetch pyramid status from the platform's pyramid endpoints ----
        pyramid_alphas: Dict[str, Any] = {}
        pyramid_multipliers: Dict[str, Any] = {}
        try:
            pyramid_alphas, pyramid_multipliers = await asyncio.gather(
                self.get_pyramid_alphas(),
                self.get_pyramid_multipliers(),
            )
        except Exception as e:
            self.log(f"Failed to fetch pyramid status: {e}", "WARNING")

        pyramid_summary: Dict[str, Dict[str, Any]] = {}
        for item in pyramid_multipliers.get('pyramids', []):
            if not isinstance(item, dict) or not _region_delay_match(item):
                continue
            cat_obj = item.get('category', {})
            cat_id = _category_id(cat_obj)
            if not cat_id:
                continue
            pyramid_summary[cat_id] = {
                'category_id': cat_id,
                'category_name': _category_name(cat_obj),
                'alpha_count': 0,
                'need_to_light': 3,
                'lit': False,
                'multiplier': _as_float(item.get('multiplier'), 1.0),
            }

        for item in pyramid_alphas.get('pyramids', []):
            if not isinstance(item, dict) or not _region_delay_match(item):
                continue
            cat_obj = item.get('category', {})
            cat_id = _category_id(cat_obj)
            if not cat_id:
                continue
            alpha_count = _as_int(item.get('alphaCount'), 0)
            pyramid_summary.setdefault(cat_id, {
                'category_id': cat_id,
                'category_name': _category_name(cat_obj),
                'multiplier': 1.0,
            })
            pyramid_summary[cat_id].update({
                'category_name': pyramid_summary[cat_id].get('category_name') or _category_name(cat_obj),
                'alpha_count': alpha_count,
                'need_to_light': max(0, 3 - alpha_count),
                'lit': alpha_count >= 3,
            })

        # ---- 2. Fetch available datasets for this region/delay ----
        datasets_resp = await self.get_datasets(region=region, delay=delay, universe=universe)
        all_datasets = datasets_resp.get('results', [])
        if not all_datasets:
            return {'error': 'No datasets available for the given region/delay/universe'}

        # ---- 2.1. Fetch neutralization options for the same simulation settings ----
        neutralization_options: List[str] = []
        neutralization_info: Dict[str, Any] = {
            'instrument_type': 'EQUITY',
            'region': region,
            'delay': delay,
            'universe': universe,
            'options': neutralization_options,
            'available': False,
            'source': 'platform_setting_options',
        }
        try:
            platform_options = await self.get_platform_setting_options()
            setting_rows = platform_options.get('instrument_options', [])
            matching_rows = [
                item for item in setting_rows
                if isinstance(item, dict) and _platform_row_match(item)
            ]
            universe_matched = True
            if not matching_rows:
                matching_rows = [
                    item for item in setting_rows
                    if isinstance(item, dict) and _platform_row_match(item, require_universe=False)
                ]
                universe_matched = False

            neutralization_options = sorted({
                str(option)
                for row in matching_rows
                for option in (row.get('Neutralization') or [])
                if option
            })
            available_universes = sorted({
                str(option)
                for row in matching_rows
                for option in (row.get('Universe') or [])
                if option
            })
            neutralization_info.update({
                'options': neutralization_options,
                'available': bool(neutralization_options),
                'universe_matched': universe_matched,
                'available_universes': available_universes,
            })
        except Exception as e:
            self.log(f"Failed to fetch neutralization options for dataset recommendations: {e}", "WARNING")
            neutralization_info.update({
                'error': str(e),
                'available': False,
            })

        for ds in all_datasets:
            cat_obj = ds.get('category', {})
            cat_id = _category_id(cat_obj)
            if not cat_id:
                continue
            pyramid_summary.setdefault(cat_id, {
                'category_id': cat_id,
                'category_name': _category_name(cat_obj),
                'alpha_count': 0,
                'need_to_light': 3,
                'lit': False,
                'multiplier': _as_float(ds.get('pyramidMultiplier'), 1.0),
            })

        # ---- 3. Load dataset quality from OS/IS Sharpe (info_data.bin) ----
        region_key = f"{region}_{delay}"
        isos_info = self._isos_data.get(region_key, {}).get('isos', {}) if self._isos_data else {}
        dataset_sharpe_map = isos_info.get('dataset', {})

        datasets_by_category: Dict[str, List[Dict[str, Any]]] = {}
        for ds in all_datasets:
            cat_id = _category_id(ds.get('category', {}))
            datasets_by_category.setdefault(cat_id, []).append(ds)

        sharpe_values_by_category: Dict[str, List[float]] = {}
        user_counts_by_category: Dict[str, List[int]] = {}
        alpha_counts_by_category: Dict[str, List[int]] = {}
        for cat_id, datasets in datasets_by_category.items():
            for ds in datasets:
                ds_id = ds.get('id', '')
                ds_sharpe_info = dataset_sharpe_map.get(ds_id, {})
                ds_sharpe = ds_sharpe_info.get('sharpe_ratio') if isinstance(ds_sharpe_info, dict) else None
                if ds_sharpe is not None:
                    sharpe_values_by_category.setdefault(cat_id, []).append(_as_float(ds_sharpe))
                user_counts_by_category.setdefault(cat_id, []).append(_as_int(ds.get('userCount'), 0))
                alpha_counts_by_category.setdefault(cat_id, []).append(_as_int(ds.get('alphaCount'), 0))

        unlit_categories = {cat_id for cat_id, item in pyramid_summary.items() if not item.get('lit')}
        restrict_to_unlit = any(
            _category_id(ds.get('category', {})) in unlit_categories
            for ds in all_datasets
        )
        candidate_datasets = [
            ds for ds in all_datasets
            if not restrict_to_unlit or _category_id(ds.get('category', {})) in unlit_categories
        ]

        # ---- 4. Score each dataset ----
        scored_datasets = []
        max_multiplier = max(
            [_as_float(item.get('multiplier'), 1.0) for item in pyramid_summary.values()] or [1.0]
        )
        for ds in candidate_datasets:
            ds_id = ds.get('id', '')
            ds_name = ds.get('name', ds_id)
            cat_obj = ds.get('category', {})
            ds_category = _category_id(cat_obj)
            ds_category_name = _category_name(cat_obj)
            sub_obj = ds.get('subcategory', {})
            ds_subcategory = _category_id(sub_obj)
            pyramid = pyramid_summary.get(ds_category, {})

            # --- Pyramid lighting score (0~40 points) ---
            cat_lit = bool(pyramid.get('lit', False))
            cat_count = _as_int(pyramid.get('alpha_count'), 0)
            need = _as_int(pyramid.get('need_to_light'), max(0, 3 - cat_count))
            multiplier = _as_float(pyramid.get('multiplier'), _as_float(ds.get('pyramidMultiplier'), 1.0))
            if not cat_lit:
                need_score = 24.0 * (need / 3.0)
                multiplier_score = 16.0 * (multiplier / max(max_multiplier, 1.0))
                pyramid_score = min(40.0, need_score + multiplier_score)
            else:
                pyramid_score = 5.0 * (multiplier / max(max_multiplier, 1.0))

            # --- Quality score from OS/IS Sharpe (0~30 points) ---
            ds_sharpe_info = dataset_sharpe_map.get(ds_id, {})
            ds_sharpe = ds_sharpe_info.get('sharpe_ratio') if isinstance(ds_sharpe_info, dict) else None
            ds_sharpe_float = _as_float(ds_sharpe) if ds_sharpe is not None else None
            ds_os_count = _as_int(ds_sharpe_info.get('count'), 0) if isinstance(ds_sharpe_info, dict) else 0
            quality_score = _rank_score(
                ds_sharpe_float,
                sharpe_values_by_category.get(ds_category, []),
                30.0,
            )

            # --- Dataset popularity: prefer more users and more submitted alphas (0~20 points) ---
            ds_user_count = _as_int(ds.get('userCount'), 0)
            ds_alpha_count = _as_int(ds.get('alphaCount'), 0)
            usage_score = _log_score(
                ds_user_count,
                user_counts_by_category.get(ds_category, []),
                10.0,
            )
            submission_score = _log_score(
                ds_alpha_count,
                alpha_counts_by_category.get(ds_category, []),
                10.0,
            )

            # --- Controlled randomness (0~5 points) ---
            random_score = random.uniform(0.0, 5.0)
            total_score = pyramid_score + quality_score + usage_score + submission_score + random_score

            scored_datasets.append({
                'dataset_id': ds_id,
                'dataset_name': ds_name,
                'category': ds_category,
                'category_name': ds_category_name,
                'subcategory': ds_subcategory,
                'total_score': round(total_score, 2),
                'pyramid_score': round(pyramid_score, 2),
                'quality_score': round(quality_score, 2),
                'usage_score': round(usage_score, 2),
                'submission_score': round(submission_score, 2),
                'random_score': round(random_score, 2),
                'distribution_score': round(usage_score + submission_score, 2),
                'category_lit': cat_lit,
                'category_alpha_count': cat_count,
                'category_need_to_light': need,
                'pyramid_multiplier': multiplier,
                'dataset_user_count': ds_user_count,
                'dataset_alpha_count': ds_alpha_count,
                'dataset_submissions_this_quarter': None,
                'os_is_sharpe': round(ds_sharpe_float, 4) if ds_sharpe_float is not None else None,
                'os_is_count': ds_os_count,
                'neutralization_options': neutralization_options,
                'neutralization_info': neutralization_info,
            })

        # Sort by total_score descending
        scored_datasets.sort(key=lambda x: x['total_score'], reverse=True)

        lit_count = sum(1 for item in pyramid_summary.values() if item.get('lit'))
        unlit_count = sum(1 for item in pyramid_summary.values() if not item.get('lit'))
        unlit_category_ids = sorted([cat_id for cat_id, item in pyramid_summary.items() if not item.get('lit')])

        return {
            'region': region,
            'delay': delay,
            'universe': universe,
            'neutralization_options': neutralization_options,
            'neutralization_info': neutralization_info,
            'recommendations': scored_datasets[:top_n],
            'total_datasets_scored': len(scored_datasets),
            'total_available_datasets': len(all_datasets),
            'total_candidate_datasets': len(candidate_datasets),
            'restricted_to_unlit_categories': restrict_to_unlit,
            'category_summary': pyramid_summary,
            'pyramid_summary': pyramid_summary,
            'pyramid_status': {
                'lit_categories': lit_count,
                'unlit_categories': unlit_count,
                'total_categories': lit_count + unlit_count,
                'unlit_category_ids': unlit_category_ids,
            },
            'scoring_weights': {
                'pyramid_lighting': '0~40 pts (unlit pyramids get priority; higher multiplier helps)',
                'dataset_quality': '0~30 pts (higher OS/IS Sharpe rank within the same pyramid)',
                'dataset_usage': '0~10 pts (higher dataset userCount rank within the same pyramid)',
                'dataset_submissions': '0~10 pts (higher dataset alphaCount rank within the same pyramid)',
                'randomness': '0~5 pts (small random jitter for exploration)',
            }
        }
            
    async def run_selection(
        self,
        selection: str,
        instrument_type: str = "EQUITY",
        region: str = "USA",
        delay: int = 1,
        selection_limit: int = 1000,
        selection_handling: str = "POSITIVE"
    ) -> Dict[str, Any]:
        """Run a selection query to filter instruments."""
        await self.ensure_authenticated()
        
        try:
            selection_data = {
                "selection": selection,
                "instrumentType": instrument_type,
                "region": region,
                "delay": delay,
                "selectionLimit": selection_limit,
                "selectionHandling": selection_handling
            }
            
            response = await self._request('GET', f"{self.base_url}/simulations/super-selection", params=selection_data)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to run selection: {str(e)}", "ERROR")
            raise

    async def get_user_profile(self, user_id: str = "self") -> Dict[str, Any]:
        """Get user profile information."""
        await self.ensure_authenticated()
        
        try:
            response = await self._request('GET', f"{self.base_url}/users/{user_id}")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get user profile: {str(e)}", "ERROR")
            raise
            
    async def get_documentations(self) -> Dict[str, Any]:
        """Get available documentations and learning materials."""
        await self.ensure_authenticated()
        
        try:
            response = await self._request('GET', f"{self.base_url}/tutorials")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get documentations: {str(e)}", "ERROR")
            raise
            
    async def get_messages(self, limit: Optional[int] = None, offset: int = 0) -> Dict[str, Any]:
        """Get messages for the current user with optional pagination.
        
        This function retrieves messages, processes their descriptions to extract
        and format embedded JSON, and handles file attachments by saving them locally.
        """
        from typing import Tuple
        
        def process_description(desc: str, message_id: str) -> Tuple[str, List[str]]:
            """
            Processes message description to handle HTML, embedded images, and JSON.
            """
            attachments = []
            
            # Handle embedded images
            soup = BeautifulSoup(desc, 'html.parser')
            for idx, img_tag in enumerate(soup.find_all('img')):
                src = img_tag.get('src', '')
                if src.startswith('data:image'):
                    try:
                        # Extract image data
                        header, encoded = src.split(',', 1)
                        ext = header.split(';')[0].split('/')[1]
                        safe_ext = re.sub(r'[^a-zA-Z0-9]', '', ext)
                        
                        # Decode and save image
                        content = base64.b64decode(encoded)
                        file_name = f"{message_id}_img_{idx}.{safe_ext}"
                        with open(file_name, "wb") as f:
                            f.write(content)
                        
                        # Update HTML and add attachment info
                        img_tag['src'] = file_name
                        attachments.append(f"Saved embedded image to ./{file_name}")
                        
                    except Exception as e:
                        attachments.append(f"Could not process embedded image: {e}")
            
            desc = str(soup)

            # Handle JSON content
            try:
                json_part_match = re.search(r'```json\n({.*?})\n```', desc, re.DOTALL)
                if json_part_match:
                    json_str = json_part_match.group(1)
                    desc = desc.replace(json_part_match.group(0), "").strip()
                    
                    try:
                        data = json.loads(json_str)
                        formatted_json = json.dumps(data, indent=2)
                        desc += f"\n\n---\n**Details**\n```json\n{formatted_json}\n```"
                    except json.JSONDecodeError:
                        desc += f"\n\n---\n**Details (raw)**\n{json_str}"
            except Exception:
                pass
                
            return desc, attachments

        await self.ensure_authenticated()
        
        try:
            params = {"limit": limit, "offset": offset}
            params = {k: v for k, v in params.items() if v is not None}
            
            response = await self._request('GET', f"{self.base_url}/users/self/messages", params=params)
            response.raise_for_status()
            messages_data = response.json()
            
            # Process descriptions and attachments
            for msg in messages_data.get("results", []):
                try:
                    msg_id = msg.get("id", "unknown_id")
                    new_desc, attachments = process_description(msg.get("description", ""), msg_id)
                    msg["description"] = new_desc
                    if attachments:
                        msg["attachments_info"] = attachments
                except Exception as e:
                    self.log(f"Error processing message {msg.get('id')}: {e}", "ERROR")

            return messages_data
            
        except Exception as e:
            self.log(f"Failed to get messages: {str(e)}", "ERROR")
            raise

    async def get_glossary_terms(self, email: str, password: str) -> List[Dict[str, str]]:
        """Get glossary terms from forum."""
        try:
            return await forum_client.get_glossary_terms(email, password)
        except Exception as e:
            self.log(f"Failed to get glossary terms: {str(e)}", "ERROR")
            raise

    async def search_forum_posts(self, email: str, password: str, search_query: str, 
                                 max_results: int = 50) -> Dict[str, Any]:
        """Search forum posts."""
        try:
            rate_limited = await self._rate_limit_forum_op("search_forum_posts")
            if rate_limited:
                return {
                    **rate_limited,
                    'operation': 'search_forum_posts',
                    'search_query': search_query,
                    'max_results': max_results,
                }
            return await forum_client.search_forum_posts(email, password, search_query, max_results)
        except Exception as e:
            self.log(f"Failed to search forum posts: {str(e)}", "ERROR")
            raise


    async def read_forum_post(self, email: str, password: str, article_id: str, 
                              include_comments: bool = True) -> Dict[str, Any]:
        """Get forum post."""
        try:
            rate_limited = await self._rate_limit_forum_op("read_forum_post")
            if rate_limited:
                return {
                    **rate_limited,
                    'operation': 'read_forum_post',
                    'article_id': article_id,
                    'include_comments': include_comments,
                }
            return await forum_client.read_full_forum_post(email, password, article_id, include_comments)
        except Exception as e:
            self.log(f"Failed to read forum post: {str(e)}", "ERROR")
            raise
    
    async def get_alpha_yearly_stats(self, alpha_id: str) -> Dict[str, Any]:
        """Get yearly statistics for an alpha."""
        await self.ensure_authenticated()
        
        max_retries = 5
        retry_delay = 2
        
        for attempt in range(max_retries):
            try:
                self.log(f"Attempting to get yearly stats for alpha {alpha_id} (attempt {attempt + 1}/{max_retries})", "INFO")
                
                response = await self._request('GET', f"{self.base_url}/alphas/{alpha_id}/recordsets/yearly-stats")
                response.raise_for_status()
                
                text = (response.text or "").strip()
                if not text:
                    if attempt < max_retries - 1:
                        self.log(f"Empty yearly stats response for {alpha_id}, retrying...", "WARNING")
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 1.5
                        continue
                    else:
                        return {}
                
                try:
                    stats_data = response.json()
                    if stats_data:
                        return stats_data
                    else:
                        if attempt < max_retries - 1:
                            self.log(f"Empty yearly stats JSON for {alpha_id}, retrying...", "WARNING")
                            await asyncio.sleep(retry_delay)
                            retry_delay *= 1.5
                            continue
                        else:
                            return {}
                            
                except json.JSONDecodeError as parse_err:
                    if attempt < max_retries - 1:
                        self.log(f"Yearly stats JSON parse failed for {alpha_id}, retrying...", "WARNING")
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 1.5
                        continue
                    else:
                        raise
                        
            except requests.RequestException as e:
                if attempt < max_retries - 1:
                    self.log(f"Failed to get yearly stats for {alpha_id}, retrying: {e}", "WARNING")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 1.5
                    continue
                else:
                    raise
        
        return {}
        
    async def get_production_correlation(self, alpha_id: str) -> Dict[str, Any]:
        """Get production correlation data for an alpha.

        Polls every 30 seconds for up to 1 hour to handle platform rate-limiting.
        For super alphas, the platform may return an empty body (HTTP 200) for a few
        minutes after simulation completes while it computes the correlation data.
        The polling loop handles this by retrying until data is available.
        Returns {'status': 'pending', ...} after max_wait_seconds if data never arrives.

        BRAIN allows only one in-flight correlation computation per account. If
        another request is already polling, this returns ``correlation_busy``
        immediately instead of queueing behind it.
        """
        await self.ensure_authenticated()

        op_name = f"get_production_correlation({alpha_id})"
        lock_info = await self._try_acquire_brain_correlation_lock(op_name)
        if not lock_info.get('acquired'):
            retry_after = self._brain_correlation_busy_retry_after_seconds
            return {
                'status': 'correlation_busy',
                'message': (
                    'Another production correlation check is already running for this account. '
                    'The BRAIN platform supports only one in-flight correlation computation. '
                    f'Please retry in {retry_after} seconds.'
                ),
                'retry_after': retry_after,
                'lock_retry_after': lock_info.get('retry_after'),
                'max': None,
                'records': [],
            }

        try:
            return await self._poll_production_correlation(alpha_id)
        finally:
            await self._release_brain_correlation_lock(lock_info, op_name)

    @staticmethod
    def _correlation_retry_after(response, default: float) -> float:
        """Seconds to wait before re-polling, from the Retry-After header when present."""
        try:
            value = float(response.headers.get('Retry-After'))
        except (TypeError, ValueError):
            return default
        # Clamp: the header is authoritative but must not stall or busy-spin.
        return max(0.5, min(value, default))

    @staticmethod
    def _ensure_correlation_extrema(corr_data: Dict[str, Any]) -> Dict[str, Any]:
        """Derive scalar 'max'/'min' from BRAIN's correlation HISTOGRAM payload.

        BRAIN returns {"schema": {"properties": [{"name": "min"}, {"name": "max"},
        {"name": "alphas"}]}, "records": [[lo, hi, count], ...]} — a histogram of how
        many production alphas fall in each 0.1-wide correlation bucket. There is no
        top-level scalar. The max correlation is the upper edge of the highest bucket
        with a NON-ZERO count (empty trailing buckets must be ignored, otherwise every
        alpha reports max == 1.0).

        Mutates and returns corr_data. A payload that already carries scalars, or that
        is not a histogram (e.g. the local self-correlation shape, whose records are
        dicts), is left untouched.
        """
        if not isinstance(corr_data, dict):
            return corr_data
        if corr_data.get('max') is not None and corr_data.get('min') is not None:
            return corr_data

        records = corr_data.get('records')
        if not isinstance(records, list) or not records:
            return corr_data

        props = [p.get('name') for p in (corr_data.get('schema') or {}).get('properties', [])
                 if isinstance(p, dict)]
        try:
            i_lo, i_hi, i_n = props.index('min'), props.index('max'), props.index('alphas')
        except ValueError:
            # Unknown schema — assume the documented [lo, hi, count] ordering.
            i_lo, i_hi, i_n = 0, 1, 2

        los, his = [], []
        for rec in records:
            if not isinstance(rec, (list, tuple)) or len(rec) <= max(i_lo, i_hi, i_n):
                continue
            try:
                count = float(rec[i_n])
            except (TypeError, ValueError):
                continue
            if count <= 0:
                continue                      # empty bucket carries no alpha
            try:
                los.append(float(rec[i_lo]))
                his.append(float(rec[i_hi]))
            except (TypeError, ValueError):
                continue

        if not his:
            return corr_data
        if corr_data.get('max') is None:
            corr_data['max'] = max(his)
        if corr_data.get('min') is None:
            corr_data['min'] = min(los)
        return corr_data

    async def _poll_production_correlation(self, alpha_id: str) -> Dict[str, Any]:
        max_wait_seconds = 3600  # 1 hour total
        poll_interval = 30       # 30 seconds per attempt (matches reference implementation)
        start_time = time.time()
        attempt = 0
        consecutive_empty = 0    # track consecutive empty-body responses
        consecutive_network_failures = 0

        while True:
            elapsed = time.time() - start_time
            if elapsed >= max_wait_seconds:
                self.log(f"Production correlation timeout after {int(elapsed)}s for {alpha_id}", "WARNING")
                return {
                    'status': 'pending',
                    'message': (
                        f"Production correlation data for alpha {alpha_id} was not available "
                        f"after {int(elapsed)}s of polling. The platform may still be computing "
                        "it. Please retry check_correlation in a few minutes."
                    ),
                    'max': None,
                    'records': [],
                }
            
            attempt += 1
            try:
                if attempt % 5 == 1:
                    self.log(f"[PC等待] 正在等待 Alpha {alpha_id} 的 PC 数据 (第 {attempt} 次查询, 已等待 {int(elapsed)}s)", "INFO")
                
                response = await self._request('GET', f"{self.base_url}/alphas/{alpha_id}/correlations/prod")
                response.raise_for_status()
                
                text = (response.text or "").strip()
                if not text:
                    consecutive_empty += 1
                    if consecutive_empty == 3:
                        # Platform is still computing — log once so users understand the wait
                        self.log(
                            f"[PC计算中] Alpha {alpha_id} 的生产相关性数据尚未就绪 "
                            f"(已收到 {consecutive_empty} 次空响应). "
                            "平台正在计算中，通常需要 1-5 分钟，请耐心等待...",
                            "INFO"
                        )
                    # BRAIN answers 200 + EMPTY BODY while it computes, and tells us how
                    # long to wait via Retry-After (typically 1s). Honour it instead of
                    # sleeping a flat 30s — that alone turns a multi-minute poll into
                    # a few seconds and makes bulk screening practical.
                    await asyncio.sleep(self._correlation_retry_after(response, poll_interval))
                    continue

                # Got a non-empty response — reset empty counter
                consecutive_empty = 0
                try:
                    corr_data = response.json()
                except json.JSONDecodeError:
                    corr_data = None
                if corr_data:
                    # The payload is a HISTOGRAM, not a scalar: schema properties are
                    # [min, max, alphas] and each record is [bucket_lo, bucket_hi, count].
                    # There is NO top-level 'max' key, so deriving it here is what makes
                    # the poller terminate at all.
                    self._ensure_correlation_extrema(corr_data)
                    if corr_data.get('max') is not None:
                        self.log(f"[PC成功] Alpha {alpha_id} PC={corr_data['max']} (第 {attempt} 次查询, 耗时 {int(elapsed)}s)", "INFO")
                        return corr_data
                    
            except (requests.RequestException, ConnectionError, TimeoutError) as e:
                consecutive_network_failures += 1
                retry_delay = min(5 * consecutive_network_failures, poll_interval)
                self.log(
                    f"Failed to get production correlation for {alpha_id} "
                    f"(network failure {consecutive_network_failures}): {e}. "
                    f"Retrying in {retry_delay}s",
                    "WARNING"
                )
                await asyncio.sleep(retry_delay)
                continue

            consecutive_network_failures = 0
            
            await asyncio.sleep(poll_interval)

    @staticmethod
    def _pnl_response_to_series(aid: str, pnl_data: dict) -> Optional[pd.Series]:
        """Convert a raw PnL API response dict to a pandas Series indexed by date."""
        try:
            if not pnl_data:
                return None
            records = pnl_data.get('records', [])
            schema = pnl_data.get('schema', {}).get('properties', [])
            if not records or not schema:
                return None
            cols = [p['name'] for p in schema]
            df = pd.DataFrame(records, columns=cols)
            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)
            if 'pnl' not in df.columns:
                return None
            return df['pnl'].rename(aid)
        except Exception:
            return None

    def _os_pnl_pool_path(
        self,
        instrument_type: str,
        region: str,
        universe: str,
        delay: Union[int, str],
    ) -> Path:
        """Return the on-disk cache path for a configuration-specific OS PnL pool."""
        cache_dir = Path(__file__).parent / 'downloads'
        cache_dir.mkdir(parents=True, exist_ok=True)
        safe_parts = [
            str(instrument_type).strip().lower() or 'unknown',
            str(region).strip().lower() or 'unknown',
            str(universe).strip().lower() or 'unknown',
            f"delay{delay}",
        ]
        return cache_dir / f"os_pnl_pool_{'_'.join(safe_parts)}.pkl"

    def _os_ppac_ids_path(
        self,
        instrument_type: str,
        region: str,
        universe: str,
        delay: Union[int, str],
    ) -> Path:
        """Sidecar file holding the Power-Pool-Alpha id set for a configuration.

        BRAIN reports two distinct correlations that partition the same OS pool
        by each alpha's ``classifications``:
          * "Self Correlation"       -> pool EXCLUDING Power Pool Alphas
          * "Power Pool Correlation" -> pool of ONLY Power Pool Alphas
        We persist which OS ids are classified 'Power Pool Alpha' so the local
        self-correlation can exclude them (matching the platform).
        """
        return self._os_pnl_pool_path(
            instrument_type, region, universe, delay
        ).with_suffix('.ppac.json')

    def _load_ppac_ids(
        self,
        instrument_type: str,
        region: str,
        universe: str,
        delay: Union[int, str],
    ) -> set:
        """Load the cached Power-Pool-Alpha id set (empty set if unavailable)."""
        try:
            p = self._os_ppac_ids_path(instrument_type, region, universe, delay)
            if p.exists():
                return set(json.loads(p.read_text()))
        except Exception as e:
            self.log(f"[SC cache] Failed to load ppac ids sidecar: {e}", "WARNING")
        return set()

    async def _get_os_pnl_pool_lock(self, pool_path: Path) -> asyncio.Lock:
        """Return the in-process lock for one configuration-specific OS PnL cache."""
        pool_key = str(pool_path)
        async with self._os_pnl_pool_locks_guard:
            lock = self._os_pnl_pool_locks.get(pool_key)
            if lock is None:
                lock = asyncio.Lock()
                self._os_pnl_pool_locks[pool_key] = lock
            return lock

    @staticmethod
    def _exclude_os_pnl_target(pool: pd.DataFrame, exclude_id: Optional[str]) -> pd.DataFrame:
        if exclude_id and isinstance(pool, pd.DataFrame) and exclude_id in pool.columns:
            return pool.drop(columns=[exclude_id], errors='ignore')
        return pool

    async def _list_matching_os_alpha_ids(
        self,
        instrument_type: str,
        region: str,
        universe: str,
        delay: Union[int, str],
    ) -> List[str]:
        """Fetch OS alpha IDs that match the target alpha's market configuration.

        BRAIN self-correlation semantics compare against the user's self alpha
        pool for the same instrument/region/universe/delay, and can include
        both REGULAR and SUPER alphas. Filtering locally on the same
        configuration keeps the local calculation aligned with those semantics.
        """
        all_ids: List[str] = []
        ppac_ids: List[str] = []
        offset = 0
        page_size = 100
        while True:
            params = {
                'stage': 'OS',
                'limit': page_size,
                'offset': offset,
                'order': '-dateSubmitted',
            }
            try:
                data = await self._request_json_with_retries(
                    'GET',
                    f"{self.base_url}/users/self/alphas",
                    params=params,
                    op_name=f"list_matching_os_alphas(offset={offset})",
                )
            except Exception as e:
                self.log(f"Failed to page OS alpha list at offset={offset}: {e}", "WARNING")
                break
            results = data.get('results') or []
            if not results:
                break
            for alpha in results:
                if not alpha.get('id'):
                    continue
                settings = alpha.get('settings', {})
                if settings.get('instrumentType') != instrument_type:
                    continue
                if settings.get('region') != region:
                    continue
                if settings.get('universe') != universe:
                    continue
                if str(settings.get('delay')) != str(delay):
                    continue
                all_ids.append(alpha['id'])
                # A Power Pool Alpha is identified by its classifications, e.g.
                # {"id": "POWER_POOL_ALPHA", "name": "Power Pool Alpha"}. The
                # platform's "Self Correlation" EXCLUDES these; "Power Pool
                # Correlation" uses only these. Match on id OR name (as the atom
                # detector does) to be robust to the key the API returns.
                classifications = alpha.get('classifications') or []
                for c in classifications:
                    if not isinstance(c, dict):
                        continue
                    cid = c.get('id') or c.get('name') or ''
                    cname = c.get('name') or ''
                    if (isinstance(cid, str) and 'POWER_POOL' in cid.upper()) or \
                       (isinstance(cname, str) and cname.strip() == 'Power Pool Alpha'):
                        ppac_ids.append(alpha['id'])
                        break
            if len(results) < page_size:
                break
            offset += page_size

        # Persist the Power-Pool-Alpha id set so get_self_correlation can
        # exclude/select them without re-listing (survives the sync debounce
        # cache and process restarts).
        try:
            sidecar = self._os_ppac_ids_path(instrument_type, region, universe, delay)
            sidecar.write_text(json.dumps(sorted(set(ppac_ids))))
        except Exception as e:
            self.log(f"[SC cache] Failed to persist ppac ids sidecar: {e}", "WARNING")

        return all_ids

    async def sync_os_pnl_pool(
        self,
        instrument_type: str,
        region: str,
        universe: str,
        delay: Union[int, str],
        exclude_id: Optional[str] = None,
    ) -> pd.DataFrame:
        """Incrementally sync the matching OS alpha PnL pool cache on disk.

        Closed-loop logic (mirrors the reference implementation):
        - Fetch the current server-side list of matching OS alpha IDs.
        - Load the local pickle cache (if any) and drop any columns whose alpha
          is no longer present on the server (handles deletions).
        - Download PnL only for IDs that are on the server but missing locally
          (OS alpha PnL is effectively static, so old columns are reused).
        - Persist the merged pool back to disk and return it.
        """
        pool_path = self._os_pnl_pool_path(instrument_type, region, universe, delay)
        pool_lock = await self._get_os_pnl_pool_lock(pool_path)
        async with pool_lock:
            pool_key = str(pool_path)
            debounce = self._os_pnl_pool_sync_debounce_seconds
            cached_sync = self._os_pnl_pool_last_sync.get(pool_key)
            if cached_sync and debounce > 0:
                synced_at, synced_pool = cached_sync
                if time.time() - synced_at <= debounce:
                    return self._exclude_os_pnl_target(synced_pool, exclude_id)

            synced_pool = await self._sync_os_pnl_pool_unlocked(
                pool_path=pool_path,
                instrument_type=instrument_type,
                region=region,
                universe=universe,
                delay=delay,
            )
            self._os_pnl_pool_last_sync[pool_key] = (time.time(), synced_pool)
            return self._exclude_os_pnl_target(synced_pool, exclude_id)

    async def _sync_os_pnl_pool_unlocked(
        self,
        pool_path: Path,
        instrument_type: str,
        region: str,
        universe: str,
        delay: Union[int, str],
    ) -> pd.DataFrame:
        server_ids = await self._list_matching_os_alpha_ids(
            instrument_type, region, universe, delay
        )

        # Load existing cache and drop removed alphas (closed-loop cleanup)
        local_pool = pd.DataFrame()
        if pool_path.exists():
            try:
                local_pool = await asyncio.to_thread(pd.read_pickle, pool_path)
                if isinstance(local_pool, pd.DataFrame) and not local_pool.empty:
                    keep_cols = [c for c in local_pool.columns if c in set(server_ids)]
                    dropped = local_pool.shape[1] - len(keep_cols)
                    if dropped > 0:
                        self.log(f"[SC cache] Dropping {dropped} alpha(s) removed from OS list", "INFO")
                    local_pool = local_pool[keep_cols]
                else:
                    local_pool = pd.DataFrame()
            except Exception as e:
                self.log(f"[SC cache] Failed to read pool pickle, rebuilding: {e}", "WARNING")
                local_pool = pd.DataFrame()

        need_download = [aid for aid in server_ids if aid not in local_pool.columns]

        if not need_download:
            self.log(f"[SC cache] Pool up-to-date: {local_pool.shape[1]} OS alphas", "INFO")
            return local_pool

        self.log(f"[SC cache] Incremental download: {len(need_download)} new OS alpha(s)", "INFO")

        fetch_sem = asyncio.Semaphore(5)

        async def fetch_one(oid: str):
            async with fetch_sem:
                try:
                    data = await self.get_alpha_pnl(oid)
                    return self._pnl_response_to_series(oid, data)
                except Exception as e:
                    self.log(f"[SC cache] Skip {oid}: PnL fetch failed ({e})", "WARNING")
                    return None

        fetched = await asyncio.gather(*[fetch_one(oid) for oid in need_download])
        new_series = [s for s in fetched if s is not None]

        if new_series:
            new_df = pd.concat(new_series, axis=1)
            full_pool = new_df if local_pool.empty else pd.concat([local_pool, new_df], axis=1)
            full_pool = full_pool.sort_index()
            try:
                await asyncio.to_thread(full_pool.to_pickle, pool_path)
            except Exception as e:
                self.log(f"[SC cache] Failed to persist pool pickle: {e}", "WARNING")
            local_pool = full_pool
            self.log(f"[SC cache] Pool now has {local_pool.shape[1]} OS alphas", "INFO")
        else:
            self.log(f"[SC cache] No new PnL captured (all fetches failed?); keeping {local_pool.shape[1]} cached", "WARNING")

        return local_pool

    async def get_self_correlation(self, alpha_id: str, correlation_type: str = 'self') -> Dict[str, Any]:
        """Calculate self-correlation locally using an incrementally-cached OS PnL pool.

        - OS alpha PnL is considered static and cached on disk
          (``downloads/os_pnl_pool.pkl``); only newly-submitted OS alphas are
          downloaded on each call and stale entries are pruned.
        - The target alpha's PnL is always fetched fresh (it is typically still
          IS and may change between calls).
        - Correlation is computed on the last 4 years of daily returns, matching
          the reference ``calculate_sc_locally`` semantics.

        correlation_type partitions the OS pool the same way the BRAIN platform
        does, via each alpha's ``classifications``:
          * 'self'      -> "Self Correlation": pool EXCLUDING Power Pool Alphas.
          * 'powerpool' -> "Power Pool Correlation": ONLY Power Pool Alphas.
          * 'all'       -> legacy behaviour (whole OS pool, no partition).
        Default is 'self' so the number matches the platform's Self Correlation
        (previously the whole pool was used, which mixed in Power Pool Alphas and
        over-reported the max).
        """
        await self.ensure_authenticated()

        try:
            # Target alpha PnL is always fresh; details are needed only for the
            # market-configuration key. Fetch both independent endpoints at once.
            target_pnl_data, target_details = await asyncio.gather(
                self.get_alpha_pnl(alpha_id),
                self.get_alpha_details(alpha_id),
            )
            target_series = self._pnl_response_to_series(alpha_id, target_pnl_data)
            if target_series is None:
                self.log(f"Could not parse PnL for target alpha {alpha_id}", "WARNING")
                return {}

            target_settings = target_details.get('settings', {})
            instrument_type = target_settings.get('instrumentType')
            region = target_settings.get('region')
            universe = target_settings.get('universe')
            delay = target_settings.get('delay')
            if not all([instrument_type, region, universe]) or delay is None:
                self.log(
                    f"Missing target settings for self-correlation on {alpha_id}: {target_settings}",
                    "WARNING",
                )
                return {}

            # Sync only the OS pool matching the target alpha's market configuration.
            os_pool = await self.sync_os_pnl_pool(
                instrument_type=instrument_type,
                region=region,
                universe=universe,
                delay=delay,
                exclude_id=alpha_id,
            )

            if os_pool is None or os_pool.empty:
                self.log(f"No OS alphas available; self-correlation for {alpha_id} is 0", "INFO")
                return {'max': 0.0, 'records': [], 'local_calculation': True, 'pool_size': 0}

            # Combine target with pool, forward-fill gaps, diff -> daily returns.
            # Use a synthetic target column so any stale cached column with the
            # same alpha id cannot create duplicate labels.
            target_col = f"__target__{alpha_id}"
            combined = pd.concat([os_pool, target_series.rename(target_col).to_frame()], axis=1).ffill()
            rets = combined.diff()
            if rets.empty:
                return {'max': 0.0, 'records': [], 'local_calculation': True, 'pool_size': os_pool.shape[1]}

            last_date = rets.index.max()
            rets = rets[rets.index > last_date - pd.DateOffset(years=4)]

            if target_col not in rets.columns:
                return {'max': 0.0, 'records': [], 'local_calculation': True, 'pool_size': os_pool.shape[1]}

            target_rets = rets[target_col]
            pool_rets = rets.drop(columns=[target_col], errors='ignore')

            # Partition the OS pool by Power-Pool-Alpha classification to match
            # the platform's Self vs Power Pool correlation semantics. Excluding
            # Power Pool Alphas is what makes the local number line up with the
            # platform's "Self Correlation" (the previous code used the whole
            # pool and could over-report the max).
            ppac_ids = self._load_ppac_ids(instrument_type, region, universe, delay)
            ctype = (correlation_type or 'self').lower()
            full_pool_size = int(pool_rets.shape[1])
            if ctype in ('self', 'selfcorr'):
                drop_cols = [c for c in pool_rets.columns if c in ppac_ids]
                pool_rets = pool_rets.drop(columns=drop_cols, errors='ignore')
            elif ctype in ('powerpool', 'ppac', 'ppa'):
                keep_cols = [c for c in pool_rets.columns if c in ppac_ids]
                pool_rets = pool_rets[keep_cols]
            # ctype == 'all' -> legacy whole-pool behaviour (no partition)
            partitioned_pool_size = int(pool_rets.shape[1])

            if pool_rets.empty:
                return {
                    'max': 0.0,
                    'records': [],
                    'local_calculation': True,
                    'pool_size': partitioned_pool_size,
                    'correlation_type': ctype,
                    'full_os_pool_size': full_pool_size,
                    'ppac_ids_cached': len(ppac_ids),
                    'excluded_power_pool_count': full_pool_size - partitioned_pool_size if ctype in ('self', 'selfcorr') else None,
                }

            # Compute only target-vs-pool correlations instead of the full N x N
            # matrix; this is the hot path when the OS pool is large.
            sc_series = pool_rets.corrwith(target_rets).dropna()
            max_corr = float(sc_series.max()) if not sc_series.empty else 0.0

            records = [
                {'id': oid, 'correlation': float(val)}
                for oid, val in sc_series.nlargest(10).items()
            ]

            self.log(
                f"[SC本地] Alpha {alpha_id}: max_{ctype}_corr={max_corr:.4f} "
                f"(pool={partitioned_pool_size}/{full_pool_size} OS alphas after "
                f"'{ctype}' partition; {len(ppac_ids)} power-pool ids cached)",
                "INFO",
            )
            return {
                'max': max_corr,
                'records': records,
                'local_calculation': True,
                'pool_size': partitioned_pool_size,
                'correlation_type': ctype,
                'full_os_pool_size': full_pool_size,
                'ppac_ids_cached': len(ppac_ids),
                'excluded_power_pool_count': (full_pool_size - partitioned_pool_size) if ctype in ('self', 'selfcorr') else None,
            }

        except Exception as e:
            self.log(f"Failed to calculate self-correlation locally: {str(e)}", "ERROR")
            raise

    async def get_mutual_correlation(
        self,
        alpha_ids: List[str],
        threshold: float = 0.5,
        years: int = 4,
    ) -> Dict[str, Any]:
        """Pairwise ("mutual") correlation AMONG a caller-supplied set of alphas.

        Unlike check_self_correlation (target-vs-OS-pool) and check_correlation
        (target-vs-production), this computes the full NxN correlation matrix
        among the given alphas' own daily returns — the check needed when
        selecting a set of alphas that must be mutually decorrelated (e.g. a
        submission basket with a max-pairwise-correlation rule).

        Correlation convention matches the local self-correlation: daily returns
        = diff of cumulative PnL (ffill gaps), restricted to the last ``years``.

        Returns the matrix, the single most-correlated pair, every pair at/above
        ``threshold``, whether all pairs are below it, and a greedy maximal
        subset whose members are all mutually below ``threshold``.
        """
        await self.ensure_authenticated()

        # De-duplicate while preserving order.
        ids = list(dict.fromkeys([a for a in (alpha_ids or []) if a]))
        if len(ids) < 2:
            return {'error': 'Provide at least 2 distinct alpha ids.', 'alpha_ids': ids}

        fetch_sem = asyncio.Semaphore(5)

        async def fetch_one(oid: str):
            async with fetch_sem:
                try:
                    data = await self.get_alpha_pnl(oid)
                    return oid, self._pnl_response_to_series(oid, data)
                except Exception as e:
                    self.log(f"[mutual-corr] Skip {oid}: PnL fetch failed ({e})", "WARNING")
                    return oid, None

        fetched = await asyncio.gather(*[fetch_one(o) for o in ids])
        series = {oid: s for oid, s in fetched if s is not None}
        missing = [oid for oid, s in fetched if s is None]
        if len(series) < 2:
            return {
                'error': 'Fewer than 2 alphas had usable PnL.',
                'missing_pnl': missing,
                'alpha_ids': ids,
            }

        present = [oid for oid in ids if oid in series]
        combined = pd.concat([series[oid].rename(oid).to_frame() for oid in present], axis=1).ffill()
        rets = combined.diff()
        if not rets.empty:
            last_date = rets.index.max()
            rets = rets[rets.index > last_date - pd.DateOffset(years=years)]
        rets = rets.dropna(how='all')
        if rets.shape[0] < 2:
            return {'error': 'Insufficient overlapping PnL history.', 'missing_pnl': missing, 'alpha_ids': ids}

        corr = rets.corr()
        cols = [c for c in present if c in corr.columns]

        def cval(a: str, b: str) -> float:
            try:
                v = float(corr.loc[a, b])
                return v if v == v else 0.0  # NaN -> 0
            except Exception:
                return 0.0

        pairs = []
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                pairs.append((cols[i], cols[j], cval(cols[i], cols[j])))
        pairs.sort(key=lambda x: -abs(x[2]))

        over = [{'a': a, 'b': b, 'correlation': round(c, 4)} for a, b, c in pairs if abs(c) >= threshold]
        max_pair = (
            {'a': pairs[0][0], 'b': pairs[0][1], 'correlation': round(pairs[0][2], 4)}
            if pairs else None
        )

        # Greedy maximal mutually-below-threshold subset: consider nodes in
        # ascending order of average |correlation| (least entangled first), keep
        # a node only if it is < threshold vs every already-kept node. This is a
        # heuristic (max independent set is NP-hard) but gives a good basket.
        if len(cols) > 1:
            avg_abs = {
                o: sum(abs(cval(o, p)) for p in cols if p != o) / (len(cols) - 1)
                for o in cols
            }
        else:
            avg_abs = {cols[0]: 0.0} if cols else {}
        order = sorted(cols, key=lambda o: avg_abs.get(o, 0.0))
        kept: List[str] = []
        for oid in order:
            if all(abs(cval(oid, k)) < threshold for k in kept):
                kept.append(oid)

        matrix = {a: {b: round(cval(a, b), 4) for b in cols} for a in cols}

        self.log(
            f"[mutual-corr] {len(cols)} alphas: max pair "
            f"{max_pair['correlation'] if max_pair else 'n/a'}, "
            f"{len(over)} pair(s) >= {threshold}, max mutually-<{threshold} subset size {len(kept)}",
            "INFO",
        )

        return {
            'alpha_ids': cols,
            'threshold': threshold,
            'years': years,
            'num_points': int(rets.shape[0]),
            'matrix': matrix,
            'max_pair': max_pair,
            'pairs_over_threshold': over,
            'all_below_threshold': len(over) == 0,
            'max_mutually_below_subset': kept,
            'max_mutually_below_subset_size': len(kept),
            'missing_pnl': missing,
            'local_calculation': True,
        }

    async def check_self_correlation(
        self,
        alpha_id: str,
        threshold: float = 0.7,
        correlation_type: str = 'self',
    ) -> Dict[str, Any]:
        """Compute self-correlation locally using the cached OS PnL pool.

        Args:
            alpha_id: Target alpha ID.
            threshold: Max-correlation threshold used for the pass/fail check.
            correlation_type: 'self' (default; pool EXCLUDES Power Pool Alphas,
                matching the platform's "Self Correlation"), 'powerpool' (only
                Power Pool Alphas, matching "Power Pool Correlation"), or 'all'
                (legacy whole-pool behaviour).
        """
        await self.ensure_authenticated()

        correlation_data = await self.get_self_correlation(alpha_id, correlation_type=correlation_type)
        if not isinstance(correlation_data, dict) or not correlation_data:
            return {
                'alpha_id': alpha_id,
                'threshold': threshold,
                'correlation_type': correlation_type,
                'max_correlation': None,
                'passes_check': None,
                'status': 'data_unavailable',
                'message': 'Local self-correlation data is unavailable for this alpha.',
                'correlation_data': correlation_data,
            }

        try:
            max_correlation = float(correlation_data.get('max'))
        except (TypeError, ValueError):
            max_correlation = None

        passes_check = max_correlation < threshold if max_correlation is not None else None

        return {
            'alpha_id': alpha_id,
            'threshold': threshold,
            'correlation_type': correlation_type,
            'max_correlation': max_correlation,
            'passes_check': passes_check,
            'local_calculation': True,
            'correlation_data': correlation_data,
        }

    async def check_correlation(self, alpha_id: str, correlation_type: str = "production", threshold: float = 0.7) -> Dict[str, Any]:
        """ Only where all IS metrics PASS to Check alpha correlation, Check alpha correlation against production alphas, self alphas, or both.

        Concurrency: production correlation hits BRAIN's per-account
        single-concurrency endpoint. ``get_production_correlation`` uses a
        fail-fast lock, so concurrent production checks return busy instead of
        waiting. The ``self`` path is computed locally and is not gated here.
        """
        await self.ensure_authenticated()

        try:
            results = {
                'alpha_id': alpha_id,
                'threshold': threshold,
                'correlation_type': correlation_type,
                'checks': {}
            }
            
            # Determine which correlations to check
            check_types = []
            if correlation_type == "both":
                check_types = ["production", "self"]
            else:
                check_types = [correlation_type]
            
            all_passed = True
            
            for check_type in check_types:
                if check_type == "production":
                    correlation_data = await self.get_production_correlation(alpha_id)
                    
                    # Handle pending/data-not-yet-available case (super alphas, fresh simulations)
                    if correlation_data and correlation_data.get('status') == 'pending':
                        results['checks'][check_type] = {
                            'max_correlation': None,
                            'passes_check': None,
                            'status': 'pending',
                            'message': correlation_data.get('message', ''),
                            'correlation_data': correlation_data,
                        }
                        results['all_passed'] = None
                        results['status'] = 'pending'
                        results['message'] = correlation_data.get('message', '')
                        return results

                    if correlation_data and correlation_data.get('status') == 'correlation_busy':
                        results['checks'][check_type] = {
                            'max_correlation': None,
                            'passes_check': None,
                            'status': 'correlation_busy',
                            'message': correlation_data.get('message', ''),
                            'retry_after': correlation_data.get('retry_after'),
                            'correlation_data': correlation_data,
                        }
                        results['all_passed'] = None
                        results['status'] = 'correlation_busy'
                        results['message'] = correlation_data.get('message', '')
                        results['retry_after'] = correlation_data.get('retry_after')
                        return results

                    if (
                        correlation_data
                        and isinstance(correlation_data.get('records'), list)
                        and len(correlation_data['records']) > 0
                        and correlation_data.get('max') is not None
                    ):
                        max_correlation = correlation_data['max']
                        passes_check = max_correlation < threshold
                        results['checks'][check_type] = {
                            'max_correlation': max_correlation,
                            'passes_check': passes_check,
                            'correlation_data': correlation_data
                        }
                        if not passes_check:
                            all_passed = False
                            results["all_passed"] = all_passed
                            return results
                    else:
                        # Data returned but has no usable records/max (empty or malformed).
                        # Return None to signal "data unavailable" rather than faking max=0.
                        results['checks'][check_type] = {
                            'max_correlation': None,
                            'passes_check': None,
                            'status': 'data_unavailable',
                            'message': (
                                'Production correlation data is unavailable for this alpha. '
                                'This may be a newly-created super alpha where the platform '
                                'has not yet computed the correlation. Please retry in a few minutes.'
                            ),
                            'correlation_data': correlation_data,
                        }
                        results['all_passed'] = None
                        results['status'] = 'data_unavailable'
                        return results
                elif check_type == "self":
                    correlation_data = await self.get_self_correlation(alpha_id)
                else:
                    continue
                
                # Analyze correlation data (self-correlation path)
                if correlation_data and correlation_data.get('max') is not None:
                    max_correlation = correlation_data['max']
                    passes_check = max_correlation < threshold
                else:
                    max_correlation = None
                    passes_check = None
                
                results['checks'][check_type] = {
                    'max_correlation': max_correlation,
                    'passes_check': passes_check,
                    'correlation_data': correlation_data
                }
                
                if passes_check is not True:
                    all_passed = False
            
            results['all_passed'] = all_passed
            
            return results
            
        except Exception as e:
            self.log(f"Failed to check correlation: {str(e)}", "ERROR")
            raise

    async def get_submission_check(self, alpha_id: str) -> Dict[str, Any]:
        """Comprehensive pre-submission check."""
        await self.ensure_authenticated()
        
        try:
            # This endpoint might not exist, so we simulate it by calling other functions
            # In a real scenario, this would be a single API call
            
            pnl_data = await self.get_alpha_pnl(alpha_id)
            yearly_stats = await self.get_alpha_yearly_stats(alpha_id)
            correlation = await self.check_correlation(alpha_id)
            
            return {
                "pnl_summary": pnl_data.get("pnlSummary", {}),
                "yearly_stats": yearly_stats,
                "correlation": correlation
            }
        except Exception as e:
            self.log(f"Failed submission check: {str(e)}", "ERROR")
            raise

    async def set_alpha_properties(self, alpha_id: str, name: Optional[str] = None, 
                                   color: Optional[str] = None, tags: Optional[List[str]] = None,
                                   descriptions: str = "None",
                                   selection_description: Optional[str] = None,
                                   combo_description: Optional[str] = None
                                   ) -> Dict[str, Any]:
        """Update alpha properties (name, color, tags, descriptions)."""
        await self.ensure_authenticated()
        
        try:
            # todo: ra_failed_count 是否为0
            payload = {
                "color": color,
                "name": name,
                "tags": tags if tags is not None else [],
                "regular": {"description": descriptions}
            }
            if selection_description is not None:
                payload["selection"] = {"description": selection_description}
            if combo_description is not None:
                payload["combo"] = {"description": combo_description}
            
            response = await self._request('PATCH', f"{self.base_url}/alphas/{alpha_id}", json=payload)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to set alpha properties: {str(e)}", "ERROR")
            raise

    async def get_record_sets(self, alpha_id: str) -> Dict[str, Any]:
        """List available record sets for an alpha."""
        await self.ensure_authenticated()
        
        try:
            response = await self._request('GET', f"{self.base_url}/alphas/{alpha_id}/recordsets")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get record sets: {str(e)}", "ERROR")
            raise

    async def get_record_set_data(self, alpha_id: str, record_set_name: str) -> Dict[str, Any]:
        """Get data from a specific record set."""
        await self.ensure_authenticated()
        
        try:
            response = await self._request('GET', f"{self.base_url}/alphas/{alpha_id}/recordsets/{record_set_name}")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get record set data: {str(e)}", "ERROR")
            raise

    async def get_user_activities(self, user_id: str, grouping: Optional[str] = None) -> Dict[str, Any]:
        """Get user activity diversity data."""
        await self.ensure_authenticated()
        
        try:
            params = {}
            if grouping:
                params['grouping'] = grouping
            
            response = await self._request('GET', f"{self.base_url}/users/{user_id}/activities", params=params)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get user activities: {str(e)}", "ERROR")
            raise

    async def get_pyramid_multipliers(self) -> Dict[str, Any]:
        """Get current pyramid multipliers showing BRAIN's encouragement levels."""
        await self.ensure_authenticated()
        
        try:
            response = await self._request('GET', f"{self.base_url}/users/self/activities/pyramid-multipliers")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get pyramid multipliers: {str(e)}", "ERROR")
            raise

    async def get_pyramid_alphas(self, start_date: Optional[str] = None,
                               end_date: Optional[str] = None) -> Dict[str, Any]:
        """Get user's current alpha distribution across pyramid categories.
        Defaults to the current quarter if no dates are provided."""
        await self.ensure_authenticated()
        
        try:
            # Default to current quarter boundaries
            if not start_date or not end_date:
                now = datetime.utcnow()
                q_start_month = (now.month - 1) // 3 * 3 + 1
                quarter_start = datetime(now.year, q_start_month, 1)
                if q_start_month + 3 > 12:
                    quarter_end = datetime(now.year + 1, 1, 1)
                else:
                    quarter_end = datetime(now.year, q_start_month + 3, 1)
                if not start_date:
                    start_date = quarter_start.strftime("%Y-%m-%d")
                if not end_date:
                    end_date = quarter_end.strftime("%Y-%m-%d")

            params = {}
            if start_date:
                params["startDate"] = start_date
            if end_date:
                params["endDate"] = end_date

            cache_key = self._generate_cache_key('pyramid_alphas', params)
            cached_data = self._get_cached_data(cache_key)
            if cached_data:
                return {**cached_data, 'from_cache': True}

            try:
                timeout_seconds = max(
                    5,
                    int(os.environ.get("BRAIN_PYRAMID_ALPHAS_TIMEOUT_SECONDS", "15")),
                )
            except Exception:
                timeout_seconds = 15

            response = await self._request(
                'GET',
                f"{self.base_url}/users/self/activities/pyramid-alphas",
                params=params,
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            data = response.json() if response.text else {}
            if isinstance(data, dict):
                data['from_cache'] = False
                self._set_cached_data(cache_key, data, ttl=3600)
            return data
        except Exception as e:
            self.log(f"Failed to get pyramid alphas: {str(e)}", "ERROR")
            raise
            
    async def get_user_competitions(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """Get list of competitions that the user is participating in."""
        await self.ensure_authenticated()
        
        try:
            if not user_id:
                # Get current user ID if not specified
                user_response = await self._request('GET', f"{self.base_url}/users/self")
                if user_response.status_code == 200:
                    user_data = user_response.json()
                    user_id = user_data.get('id')
                else:
                    user_id = 'self'
            
            response = await self._request('GET', f"{self.base_url}/users/{user_id}/competitions")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get user competitions: {str(e)}", "ERROR")
            raise
            
    async def get_competition_details(self, competition_id: str) -> Dict[str, Any]:
        """Get detailed information about a specific competition."""
        await self.ensure_authenticated()
        
        try:
            response = await self._request('GET', f"{self.base_url}/competitions/{competition_id}")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get competition details: {str(e)}", "ERROR")
            raise
            
    async def get_competition_agreement(self, competition_id: str) -> Dict[str, Any]:
        """Get the rules, terms, and agreement for a specific competition."""
        await self.ensure_authenticated()
        
        try:
            response = await self._request('GET', f"{self.base_url}/competitions/{competition_id}/agreement")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get competition agreement: {str(e)}", "ERROR")
            raise

    async def get_platform_setting_options(self) -> Dict[str, Any]:
        """Get available instrument types, regions, delays, and universes with Redis caching (1 day TTL)."""
        await self.ensure_authenticated()
        
        try:
            # Generate cache key (no parameters needed as this endpoint returns fixed platform settings)
            cache_key = self._generate_cache_key('platform_settings', {})
            
            # Try to get from cache
            cached_data = self._get_cached_data(cache_key)
            if cached_data:
                return {**cached_data, 'from_cache': True}
            
            # Use OPTIONS method on simulations endpoint to get configuration options
            response = await self._request('OPTIONS', f"{self.base_url}/simulations")
            response.raise_for_status()
            
            # Parse the settings structure from the response
            settings_data = response.json()
            settings_options = settings_data['actions']['POST']['settings']['children']
            
            # Extract instrument configuration options
            instrument_type_data = {}
            region_data = {}
            universe_data = {}
            delay_data = {}
            neutralization_data = {}
            
            # Parse each setting type
            for key, setting in settings_options.items():
                if setting['type'] == 'choice':
                    if setting['label'] == 'Instrument type':
                        instrument_type_data = setting['choices']
                    elif setting['label'] == 'Region':
                        region_data = setting['choices']['instrumentType']
                    elif setting['label'] == 'Universe':
                        universe_data = setting['choices']['instrumentType']
                    elif setting['label'] == 'Delay':
                        delay_data = setting['choices']['instrumentType']
                    elif setting['label'] == 'Neutralization':
                        neutralization_data = setting['choices']['instrumentType']
            
            # Build comprehensive instrument options
            data_list = []
            
            for instrument_type in instrument_type_data:
                for region in region_data[instrument_type['value']]:
                    for delay in delay_data[instrument_type['value']]['region'][region['value']]:
                        row = {
                            'InstrumentType': instrument_type['value'],
                            'Region': region['value'],
                            'Delay': delay['value']
                        }
                        row['Universe'] = [
                            item['value'] for item in universe_data[instrument_type['value']]['region'][region['value']]
                        ]
                        row['Neutralization'] = [
                            item['value'] for item in neutralization_data[instrument_type['value']]['region'][region['value']]
                        ]
                        data_list.append(row)
            
            # Return structured data
            result = {
                'instrument_options': data_list,
                'total_combinations': len(data_list),
                'instrument_types': [item['value'] for item in instrument_type_data],
                'regions_by_type': {
                    item['value']: [r['value'] for r in region_data[item['value']]]
                    for item in instrument_type_data
                },
                'from_cache': False
            }
            
            # Cache the data (1 day TTL)
            self._set_cached_data(cache_key, result, ttl=604800)
            
            return result
            
        except Exception as e:
            self.log(f"Failed to get instrument options: {str(e)}", "ERROR")
            raise
            
    async def performance_comparison(self, alpha_id: str, competition: Optional[str] = None,
                                     team_id: Optional[str] = None) -> Dict[str, Any]:
        """Get before-and-after performance comparison data for an alpha.

        BRAIN exposes this resource below the alpha's owning scope.  The old
        ``/alphas/{id}/performance-comparison`` route now returns 404, so keep
        the scope selection explicit instead of passing a team as a query
        parameter to the personal endpoint.

        Scope precedence is competition, team, then the authenticated user.
        """
        await self.ensure_authenticated()

        try:
            if competition:
                owner_scope = f"competitions/{competition}"
            elif team_id:
                owner_scope = f"teams/{team_id}"
            else:
                owner_scope = "users/self"

            url = (
                f"{self.base_url}/{owner_scope}/alphas/{alpha_id}/"
                "before-and-after-performance"
            )

            # The endpoint returns an empty body with a Retry-After header while
            # the comparison is being computed, then JSON once it is ready.
            try:
                payload = await self._request_json_with_retries(
                    'GET', url, op_name="performance_comparison"
                )
                return archive_performance_comparison(
                    alpha_id, owner_scope, payload
                )
            except requests.HTTPError as error:
                response = error.response
                if response is None or response.status_code != 400:
                    raise

                # BRAIN drops this pre-submission report after an alpha enters
                # OS.  Translate only that known case; retain unexpected 400s.
                details_response = await self._request(
                    "GET", f"{self.base_url}/alphas/{alpha_id}"
                )
                details_response.raise_for_status()
                details = details_response.json()
                if details.get("stage") != "OS":
                    raise
                return {
                    "available": False,
                    "reason": "performance_comparison_is_pre_submission_only",
                    "alpha_id": alpha_id,
                    "stage": "OS",
                    "status": details.get("status"),
                    "dateSubmitted": details.get("dateSubmitted"),
                }
        except Exception as e:
            self.log(f"Failed to get performance comparison: {str(e)}", "ERROR")
            raise
            
    # --- Helper function for data flattening ---
    
    async def expand_nested_data(self, data: List[Dict[str, Any]], preserve_original: bool = True) -> List[Dict[str, Any]]:
        """Flatten complex nested data structures into tabular format."""
        try:
            df = pd.json_normalize(data, sep='_')
            if preserve_original:
                original_df = pd.DataFrame(data)
                df = pd.concat([original_df, df], axis=1)
                df = df.loc[:,~df.columns.duplicated()]
            return df.to_dict(orient='records')
        except Exception as e:
            self.log(f"Failed to expand nested data: {str(e)}", "ERROR")
            raise
            
    # --- New documentation endpoint ---
    
    async def get_documentation_page(self, page_id: str) -> Dict[str, Any]:
        """Retrieve detailed content of a specific documentation page/article."""
        await self.ensure_authenticated()
        
        try:
            response = await self._request('GET', f"{self.base_url}/tutorial-pages/{page_id}")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.log(f"Failed to get documentation page: {str(e)}", "ERROR")
            raise

brain_client = BrainApiClient()

# --- Configuration Management ---

def _resolve_config_path(for_write: bool = False) -> str:
    """
    Resolve the configuration file path.
    
    Checks for a file specified by the MCP_CONFIG_FILE environment variable,
    then falls back to ~/.brain_mcp_config.json. If for_write is True,
    it ensures the directory exists.
    """
    if 'MCP_CONFIG_FILE' in os.environ:
        return os.environ['MCP_CONFIG_FILE']
    
    config_path = Path(__file__).parent / "user_config.json"
    
    if for_write:
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
        except (IOError, OSError) as e:
            logger.warning(f"Could not create config directory {config_path.parent}: {e}")
            # Fallback to a temporary file if home is not writable
            import tempfile
            return tempfile.NamedTemporaryFile(delete=False).name
            
    return str(config_path)

def _load_dotenv_into_environ():
    """Load .env into environment using python-dotenv if available; fallback to simple parser."""
    try:
        from dotenv import load_dotenv, find_dotenv
        env_path = find_dotenv(usecwd=True)
        if env_path:
            load_dotenv(env_path, override=False)
        else:
            # Try repo root relative to this file
            candidate = Path(__file__).parent / ".env"
            if candidate.exists():
                load_dotenv(candidate, override=False)
    except Exception:
        # Fallback: very simple .env parser (KEY=VALUE, no export, ignores quotes)
        try:
            candidate = Path(__file__).parent / ".env"
            if candidate.exists():
                for line in candidate.read_text().splitlines():
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' not in line:
                        continue
                    k, v = line.split('=', 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    os.environ.setdefault(k, v)
        except Exception:
            pass

def load_config() -> Dict[str, Any]:
    """Load configuration from file and overlay environment variables (from .env if present)."""
    config: Dict[str, Any] = {}
    config_file = _resolve_config_path()
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r') as f:
                config = json.load(f) or {}
        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"Error loading config file {config_file}: {e}")

    # Load .env into environment (no override of already-set env)
    _load_dotenv_into_environ()

    # Overlay credentials from env if available
    env_email = os.getenv("CREDENTIALS_EMAIL")
    env_password = os.getenv("CREDENTIALS_PASSWORD")
    if env_email or env_password:
        creds = dict(config.get("credentials", {}))
        if env_email:
            creds["email"] = env_email
        if env_password:
            creds["password"] = env_password
        config["credentials"] = creds

    return config

def save_config(config: Dict[str, Any]):
    """Save configuration to file using the resolved config path.
    
    This function now uses the write-enabled path resolver to handle
    cases where the default home directory is not writable.
    """
    config_file = _resolve_config_path(for_write=True)
    try:
        with open(config_file, 'w') as f:
            json.dump(config, f, indent=2)
    except IOError as e:
        logger.error(f"Error saving config file to {config_file}: {e}")

# --- MCP Tool Definitions ---

_MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
try:
    _MCP_PORT = int(os.getenv("MCP_PORT", "8000"))
except Exception:
    _MCP_PORT = 8000
_MCP_STREAMABLE_HTTP_PATH = os.getenv("MCP_STREAMABLE_HTTP_PATH", "/mcp")

mcp = FastMCP(
    "brain-platform-mcp",
    "A server for interacting with the WorldQuant BRAIN platform",
    host=_MCP_HOST,
    port=_MCP_PORT,
    streamable_http_path=_MCP_STREAMABLE_HTTP_PATH,
)

# Add health check endpoint for container monitoring
from mcp.server.fastmcp import Context
from starlette.responses import JSONResponse

@mcp.custom_route('/health', methods=['GET'])
async def health_check(context: Context):
    """Health check endpoint for Docker container monitoring."""
    return JSONResponse({
        "status": "healthy",
        "service": "brain-platform-mcp",
        "timestamp": datetime.utcnow().isoformat(),
        "redis_connected": brain_client.redis_client is not None
    })

# ============================================================================
# Response-slimming helpers
# ----------------------------------------------------------------------------
# Keep MCP tool outputs compact so long agent sessions (and any hook /
# transcript evaluators that re-read the conversation) don't blow the context
# window. These ONLY strip noise: fixed help strings, null sub-objects,
# redundant repeated fields, oversized free text, and full daily PnL series.
# The essential ids / metrics / checks / pyramid info are preserved (often in a
# clearer shape). Every helper is defensive: on an unexpected shape or an
# {"error": ...} payload it returns the input unchanged.
# ============================================================================

_RA_2Y_NAMES = ("LOW_2Y_SHARPE", "IS_LADDER_SHARPE")

# WebDataScope-0.10.20/src/scripts/background.js :: getAlphaCheckStates — canonical RA / PPA check names.
_RA_CHECK_NAMES = frozenset([
    "HIGH_TURNOVER", "LOW_TURNOVER", "LOW_FITNESS", "LOW_RETURNS", "LOW_SHARPE",
    "LOW_GLB_AMER_SHARPE", "LOW_GLB_APAC_SHARPE", "LOW_GLB_EMEA_SHARPE", "LOW_ASI_JPN_SHARPE",
    "IS_LADDER_SHARPE",  # ATOM-exempt but still counted in the RA gate
    "LOW_2Y_SHARPE", "LOW_SUB_UNIVERSE_SHARPE", "LOW_ROBUST_UNIVERSE_SHARPE",
    "LOW_AFTER_COST_ILLIQUID_UNIVERSE_SHARPE", "LOW_INVESTABILITY_CONSTRAINED_SHARPE",
    "LOW_ROBUST_UNIVERSE_RETURNS", "CONCENTRATED_WEIGHT",
])
_PPA_CHECK_NAMES = frozenset([
    "LOW_TURNOVER", "HIGH_TURNOVER", "LOW_SUB_UNIVERSE_SHARPE", "LOW_ROBUST_UNIVERSE_SHARPE",
    "LOW_ROBUST_UNIVERSE_SHARPE.WITH_RATIO", "LOW_ROBUST_UNIVERSE_RETURNS",
    "LOW_INVESTABILITY_CONSTRAINED_SHARPE",
])


def _ra_bad(result):
    # WebDataScope rule: a check counts as failing the RA/PPA gate iff result != "PASS" and result != "PENDING"
    return result != "PASS" and result != "PENDING"


def _truncate(s, n=160):
    if not isinstance(s, str):
        return s
    s2 = s.strip()
    return s2 if len(s2) <= n else s2[:n].rstrip() + "…"


def _unwrap_result(obj):
    """brain_client methods usually return {"result": <payload>}; some return the payload directly."""
    if isinstance(obj, dict) and list(obj.keys()) == ["result"]:
        return obj["result"], True
    return obj, False


def _rewrap(payload, was_wrapped):
    return {"result": payload} if was_wrapped else payload


def _is_error(payload):
    return isinstance(payload, dict) and "error" in payload


def _slim_checks(checks):
    """Compress an is.checks[] array into fail/warning/pass/pending buckets + pyramid info + headline values
    + precomputed RA/PPA failure counts (WebDataScope getAlphaCheckStates). Returns (buckets, pyramids, extracted, ra)."""
    out = {"fail": [], "warning": [], "pass": [], "pending": []}
    pyramids = None
    extracted = {}
    rename = {"LOW_ROBUST_UNIVERSE_SHARPE": "robust_universe_sharpe",
              "LOW_SUB_UNIVERSE_SHARPE": "sub_universe_sharpe"}
    failed_ra = 0
    failed_ppa = 0
    ra_failed_names = []
    ppa_failed_names = []
    for c in checks or []:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        res = c.get("result")
        val = c.get("value")
        if name == "MATCHES_PYRAMID":
            pyramids = {"effective": c.get("effective"),
                        "list": [{"name": p.get("name"), "multiplier": p.get("multiplier")}
                                 for p in (c.get("pyramids") or []) if isinstance(p, dict)]}
        if name in rename and val is not None:
            extracted[rename[name]] = val
        if name in _RA_2Y_NAMES and val is not None:
            extracted["two_year_sharpe"] = val
            if c.get("year") is not None:
                extracted["two_year_ladder_window"] = c.get("year")
        # --- RA / PPA failure counting (verbatim port of background.js getAlphaCheckStates) ---
        if name in _RA_CHECK_NAMES and _ra_bad(res):
            failed_ra += 1
            ra_failed_names.append(name)
        if (name in _PPA_CHECK_NAMES and _ra_bad(res)) or (name == "LOW_SHARPE" and isinstance(val, (int, float)) and val < 1):
            failed_ppa += 1
            ppa_failed_names.append(name)
        # --- buckets ---
        if res == "FAIL":
            out["fail"].append({k: c.get(k) for k in ("name", "value", "limit", "year", "message", "date")
                                if c.get(k) is not None})
        elif res == "WARNING":
            d = {k: c.get(k) for k in ("name", "value", "limit", "year", "message") if c.get(k) is not None}
            out["warning"].append(d if d else {"name": name})
        elif res == "PENDING":
            out["pending"].append(name)
        elif res in (None, "PASS", "OK"):
            out["pass"].append(name)
        else:
            out["pass"].append(f"{name}:{res}")
    ra = {"failed_ra_count": failed_ra, "failed_ppa_count": failed_ppa,
          "ra_failed": failed_ra > 0, "ppa_failed": failed_ppa > 0}
    if ra_failed_names:
        ra["ra_failed_checks"] = ra_failed_names
    if ppa_failed_names:
        ra["ppa_failed_checks"] = ppa_failed_names
    if pyramids and pyramids.get("list"):
        # WQPPYS: the pyramid leaf names joined, e.g. "sentiment/analyst"
        ra["pyramid_short"] = "/".join((p.get("name") or "").split("/")[-1].lower()
                                       for p in pyramids["list"] if p.get("name"))
    return out, pyramids, extracted, ra


def _slim_alpha(a):
    """Reduce a full alpha object to id / code / settings / key-metrics / checks / pyramids."""
    if not isinstance(a, dict):
        return a
    isd = a.get("is") or {}
    inv = isd.get("investabilityConstrained") or {}
    rn = isd.get("riskNeutralized") or {}
    checks, pyramids, extracted, ra = _slim_checks(isd.get("checks"))
    metrics = {k: isd.get(k) for k in ("sharpe", "fitness", "turnover", "returns", "drawdown",
                                       "margin", "longCount", "shortCount", "pnl", "bookSize", "startDate",
                                       "sharpe_se", "sharpe_t_stat", "selfCorrelation", "prodCorrelation")
               if isd.get(k) is not None}
    # also keep any other small scalar metric the platform may add later (excludes the big sub-dicts/checks)
    for k, v in isd.items():
        if k not in metrics and k not in ("checks", "investabilityConstrained", "riskNeutralized") and isinstance(v, (int, float)):
            metrics[k] = v
    metrics.update(extracted)
    if inv.get("sharpe") is not None:
        metrics["investability_sharpe"] = inv.get("sharpe")
        if inv.get("fitness") is not None:
            metrics["investability_fitness"] = inv.get("fitness")
    if rn.get("sharpe") is not None:
        metrics["risk_neutralized_sharpe"] = rn.get("sharpe")
    reg = a.get("regular")
    code = reg.get("code") if isinstance(reg, dict) else reg
    out = {
        "id": a.get("id"),
        "code": code,
        "status": a.get("status"),
        "stage": a.get("stage"),
        "dateSubmitted": a.get("dateSubmitted"),
        "settings": a.get("settings"),
        "metrics": metrics or None,
        "ra": ra,                 # precomputed Failed RA / Failed PPA (WebDataScope getAlphaCheckStates) — read this instead of recounting checks
        "checks": checks,
        "pyramids": pyramids,
    }
    for k in ("name", "color", "tags"):
        v = a.get(k)
        if v not in (None, "", []):
            out[k] = v
    for k in ("osmosisPoints",):
        v = a.get(k)
        if v is not None:
            out[k] = v
    return {k: v for k, v in out.items() if v is not None}


def _slim_alpha_response(obj):
    payload, w = _unwrap_result(obj)
    if _is_error(payload) or not isinstance(payload, dict):
        return obj
    return _rewrap(_slim_alpha(payload), w)


def _slim_alpha_list(obj):
    payload, w = _unwrap_result(obj)
    if not isinstance(payload, dict) or "results" not in payload:
        return obj
    out = {k: v for k, v in payload.items() if k != "results"}
    out["results"] = [_slim_alpha(a) if isinstance(a, dict) else a for a in payload.get("results", [])]
    return _rewrap(out, w)


def _slim_multisim(obj):
    payload, w = _unwrap_result(obj)
    if not isinstance(payload, dict) or "alpha_results" not in payload:
        return obj
    new_results = []
    for r in payload.get("alpha_results", []):
        if isinstance(r, dict) and isinstance(r.get("details"), dict):
            d = r["details"]
            if list(d.keys()) == ["result"]:
                d = d["result"]
            slim = _slim_alpha(d)
            new_results.append({"alpha_id": r.get("alpha_id"), "location": r.get("location"), **slim})
        else:
            new_results.append(r)
    out = {k: payload.get(k) for k in ("success", "message", "total_requested", "total_created",
                                       "multisimulation_id") if k in payload}
    out["alpha_results"] = new_results
    return _rewrap(out, w)


def _slim_datafields(obj):
    payload, w = _unwrap_result(obj)
    if not isinstance(payload, dict) or "results" not in payload:
        return obj
    fields = []
    for f in payload.get("results", []):
        if not isinstance(f, dict):
            fields.append(f)
            continue
        fields.append({"id": f.get("id"), "type": f.get("type"), "coverage": f.get("coverage"),
                       "userCount": f.get("userCount"), "alphaCount": f.get("alphaCount"),
                       "description": _truncate(f.get("description"), 160)})
    out = {"results": fields, "count": payload.get("count")}
    for k in ("sharpe_filter_applied", "sharpe_filter_removed"):
        if k in payload:
            out[k] = payload[k]
    return _rewrap(out, w)


def _slim_datasets(obj):
    payload, w = _unwrap_result(obj)
    if not isinstance(payload, dict) or "results" not in payload:
        return obj
    ds = []
    for d in payload.get("results", []):
        if not isinstance(d, dict):
            ds.append(d)
            continue
        cat = d.get("category")
        ds.append({"id": d.get("id"), "name": d.get("name"),
                   "category": cat.get("id") if isinstance(cat, dict) else cat,
                   "coverage": d.get("coverage"), "fieldCount": d.get("fieldCount"),
                   "userCount": d.get("userCount"), "alphaCount": d.get("alphaCount"),
                   "valueScore": d.get("valueScore"), "pyramidMultiplier": d.get("pyramidMultiplier"),
                   "description": _truncate(d.get("description"), 200)})
    return _rewrap({"results": ds, "count": payload.get("count")}, w)


def _records_to_dicts(payload):
    schema = payload.get("schema") or {}
    props = [p.get("name") for p in (schema.get("properties") or []) if isinstance(p, dict)]
    recs = payload.get("records") or []
    if props and recs and isinstance(recs[0], list):
        return [dict(zip(props, r)) for r in recs]
    return recs


def _slim_yearly(obj):
    payload, w = _unwrap_result(obj)
    if not isinstance(payload, dict) or "records" not in payload:
        return obj
    return _rewrap({"records": _records_to_dicts(payload)}, w)


def _slim_pnl(obj, max_rows=160):
    payload, w = _unwrap_result(obj)
    if not isinstance(payload, dict) or "records" not in payload:
        return obj
    schema = payload.get("schema") or {}
    props = [p.get("name") for p in (schema.get("properties") or []) if isinstance(p, dict)]
    recs = payload.get("records") or []
    n = len(recs)
    kept = recs
    if n > max_rows:
        stride = max(1, n // max_rows)
        kept = recs[::stride]
        if kept and recs and kept[-1] is not recs[-1]:
            kept = kept + [recs[-1]]
    out = {"properties": props, "records": kept, "num_records_original": n,
           "downsampled": len(kept) != n}
    return _rewrap(out, w)


def _slim_correlation_block(b):
    if not isinstance(b, dict):
        return b
    out = {}
    for k in ("max_correlation", "passes_check"):
        if k in b:
            out[k] = b[k]
    cd = b.get("correlation_data") or {}
    recs = cd.get("records")
    if isinstance(recs, list) and recs and isinstance(recs[0], list) and len(recs[0]) >= 3:
        out["histogram_nonzero"] = [{"range": [r[0], r[1]], "n": r[2]} for r in recs if len(r) >= 3 and r[2]]
        for k in ("max", "min"):
            if cd.get(k) is not None:
                out[k] = cd.get(k)
    elif isinstance(recs, list) and recs and isinstance(recs[0], dict):
        out["top_correlated"] = recs[:5]
        if cd.get("pool_size") is not None:
            out["pool_size"] = cd.get("pool_size")
    # Surface the Self/PowerPool pool-partition metadata (local self-correlation).
    for k in ("correlation_type", "full_os_pool_size", "excluded_power_pool_count", "ppac_ids_cached"):
        if cd.get(k) is not None:
            out[k] = cd.get(k)
    return out


def _slim_check_correlation(obj):
    payload, w = _unwrap_result(obj)
    if _is_error(payload) or not isinstance(payload, dict):
        return obj
    # check_self_correlation top-level shape: {alpha_id, threshold, max_correlation, passes_check, correlation_data, ...}
    if "max_correlation" in payload and "checks" not in payload:
        out = {k: payload.get(k) for k in ("alpha_id", "threshold", "correlation_type", "passes_check", "local_calculation")
               if k in payload}
        out.update(_slim_correlation_block(payload))
        return _rewrap(out, w)
    # check_correlation shape: {alpha_id, threshold, correlation_type, checks: {production:{...}, self:{...}}, all_passed}
    out = {k: payload.get(k) for k in ("alpha_id", "threshold", "correlation_type") if k in payload}
    checks = payload.get("checks")
    if isinstance(checks, dict):
        out["checks"] = {k: _slim_correlation_block(v) for k, v in checks.items()}
    if "all_passed" in payload:
        out["all_passed"] = payload["all_passed"]
    return _rewrap(out, w)


def _slim_pyramids(obj, kind):
    """kind: 'alphas' -> alphaCount, 'multipliers' -> multiplier. Reshape list to {region: {Dn: {cat: val}}}."""
    payload, w = _unwrap_result(obj)
    if not isinstance(payload, dict) or "pyramids" not in payload:
        return obj
    val_key = "alphaCount" if kind == "alphas" else "multiplier"
    nested = {}
    for p in payload.get("pyramids", []):
        if not isinstance(p, dict):
            continue
        cat = p.get("category")
        cat_id = cat.get("id") if isinstance(cat, dict) else cat
        nested.setdefault(p.get("region"), {}).setdefault(f"D{p.get('delay')}", {})[cat_id] = p.get(val_key)
    return _rewrap({"pyramids": nested}, w)


def _slim_text_lookup(obj, fields=("description", "content"), n=4000):
    """Recursively truncate big free-text / raw fields in nested responses (operators, docs, lookINTO, ...)."""
    trunc_keys = set(fields) | {"raw"}
    def fix(o):
        if isinstance(o, dict):
            r = {}
            for k, v in o.items():
                if k in trunc_keys and isinstance(v, str):
                    r[k] = _truncate(v, n)
                else:
                    r[k] = fix(v)
            return r
        if isinstance(o, list):
            return [fix(x) for x in o]
        return o
    return fix(obj)


@mcp.tool()
async def authenticate() -> Dict[str, Any]:
    """
    Authenticate with WorldQuant BRAIN platform.
    
    This is the first step in any BRAIN workflow. You must authenticate before using any other tools.
    
    Args:
        None
    Returns:
        Authentication result with user info and permissions
    """
    try:
        # Load config to get credentials if not provided
        config = load_config()
        credentials = config.get("credentials", {})
        email = credentials.get("email")
        password = credentials.get("password")
        
        auth_result = await brain_client.authenticate(email, password)
        
        # # Save successful credentials
        # if auth_result.get('status') == 'authenticated':
        #     if 'credentials' not in config:
        #         config['credentials'] = {}
        #     config['credentials']['email'] = email
        #     config['credentials']['password'] = password
        #     save_config(config)
            
        return auth_result
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def authenticate_brainlabs() -> Dict[str, Any]:
    """
    Sign in to BRAIN Labs and return the live AWS WorkSpaces deepLink session URL.

    BRAIN Labs is delivered as an AWS WorkSpaces Web pixel-stream, so it cannot be
    code-driven headlessly; this tool performs the two-step sign-in (platform +
    Labs password) via Playwright and hands back the WorkSpaces URL to open, plus
    the decoded internal labs URL/token. Serialized through a single-concurrency
    lock (LABS_MAX_CONCURRENCY, default 1) because a Labs account has exactly one
    interactive session.

    Returns:
        {status, workspaces_url, labs_url, token, note} or {error}.
    """
    try:
        config = load_config()
        credentials = config.get("credentials", {})
        email = credentials.get("email")
        password = credentials.get("password")
        if not email or not password:
            return {"error": "No BRAIN credentials configured (CREDENTIALS_EMAIL / CREDENTIALS_PASSWORD)."}
        return await labs_client.open_labs_session(email, password)
    except Exception as e:
        return {"error": f"BRAIN Labs sign-in failed: {str(e)}"}

@mcp.tool()
async def emit_labs_script(
    dataset_id: str,
    fields: List[str],
    region: str = "USA",
    universe: str = "TOP3000",
    delay: int = 1,
    labs_output: str = "/tmp/labs_data_analysis_result.json",
) -> Dict[str, Any]:
    """
    Generate the pasteable BRAIN Labs data-analysis script for a dataset's MATRIX fields.

    Raw panel data is only available inside Labs (`from brain import Brain`), so the
    emitted script must be run in the Labs JupyterLab. Requires the LABS_AGENT_SCRIPT
    env var to point at labs_data_analysis_agent.py. Serialized by the Labs lock.

    Args:
        dataset_id: Dataset id to analyze.
        fields: MATRIX field ids (at most two for downstream Python alpha design).
        region/universe/delay: Simulation target context.
        labs_output: Path the in-Labs script writes its JSON result to.
    """
    try:
        return await labs_client.emit_labs_script(
            dataset_id=dataset_id,
            fields=fields,
            region=region,
            universe=universe,
            delay=delay,
            labs_output=labs_output,
        )
    except Exception as e:
        return {"error": f"emit_labs_script failed: {str(e)}"}

@mcp.tool()
async def ingest_labs_result(result_json: str) -> Dict[str, Any]:
    """
    Parse a BRAIN Labs data-analysis result (a JSON string or a file path) and return it.

    Use after running the emit_labs_script output inside Labs. Serialized by the Labs lock.
    """
    try:
        return await labs_client.ingest_labs_result(result_json)
    except Exception as e:
        return {"error": f"ingest_labs_result failed: {str(e)}"}

@mcp.tool()
async def manage_config(action: str = "get", settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Manage configuration settings - get or update configuration.
    
    Args:
        action: Action to perform ("get" to retrieve config, "set" to update config)
        settings: Configuration settings to update (required when action="set")
    
    Returns:
        Current or updated configuration including authentication status
    """
    config = load_config()
    
    if action == "set" and settings:
        config.update(settings)
        save_config(config)
        
    is_authed = await brain_client.is_authenticated()
    config['isAuthenticated'] = is_authed
    
    # Mask password for security
    if 'password' in config:
        config['password'] = '********'
        
    return config

# --- Simulation Tools ---

@mcp.tool()
async def create_simulation(
    type: str = "REGULAR",
    region: str = "USA",
    universe: str = "TOP3000",
    delay: int = 1,
    decay: int = 4,
    neutralization: str = "SUBINDUSTRY",
    truncation: float = 0.08,
    test_period: str = "P0Y0M",
    language: str = "FASTEXPR",
    unit_handling: str = "VERIFY",
    nan_handling: str = "ON",
    lookback: Optional[int] = None,
    alpha_expression: Optional[str] = None,
    combo: Optional[str] = None,
    selection: Optional[str] = None,
    pasteurization: str = "ON",
    max_trade: str = "OFF",
    selection_handling: str = "POSITIVE",
    selection_limit: int = 1000,
    component_activation: str = "IS",
) -> Dict[str, Any]:
    """
    Create a new simulation on BRAIN platform.
    
    This tool creates and starts a simulation with your alpha code. Use this after you have your alpha formula ready.
    if field type=VECTOR should deal with vec_ suffer vec_*(FIELD)
    Args:
        type: Simulation type ("REGULAR" or "SUPER")
        region: Market region (e.g., "USA")
        universe: Universe of stocks (e.g., "TOP3000")
        delay: Data delay (0 or 1)
        decay: Decay value for the simulation
        neutralization: Neutralization method
        truncation: Truncation value
        test_period: Test period (e.g., "P0Y0M" for 1 year 6 months)
        language: Expression language ("FASTEXPR" or "PYTHON")
        unit_handling: Unit handling method. Used for FASTEXPR simulations.
        nan_handling: NaN handling method
        lookback: Historical lookback window. Only used for PYTHON simulations; defaults to 256 for PYTHON.
        alpha_expression: Alpha expression code (for REGULAR type)
        combo: Combo code (for SUPER type)
        selection: Selection code (for SUPER type). For USA SUPER simulations,
            this must include (prod_correlation > 0)
    
    Returns:
        Simulation creation result with ID and location
    """
    instrument_type = "EQUITY"
    visualization = False
    try:
        normalized_language = language.upper()
        settings_kwargs = {
            "instrumentType": instrument_type,
            "region": region,
            "universe": universe,
            "delay": delay,
            "decay": decay,
            "neutralization": neutralization,
            "truncation": truncation,
            "testPeriod": test_period,
            "language": normalized_language,
            "visualization": visualization,
            "pasteurization": pasteurization,
            "maxTrade": max_trade,
            "selectionHandling": selection_handling,
            "selectionLimit": selection_limit,
            "componentActivation": component_activation,
        }

        if normalized_language == "PYTHON":
            settings_kwargs["lookback"] = 256 if lookback is None else lookback
            settings_kwargs["unitHandling"] = None
            settings_kwargs["nanHandling"] = None
        else:
            settings_kwargs["unitHandling"] = unit_handling
            settings_kwargs["nanHandling"] = nan_handling

        settings = SimulationSettings(
            **settings_kwargs
        )
        
        sim_data = SimulationData(
            type=type,
            settings=settings,
            regular=alpha_expression,
            combo=combo,
            selection=selection
        )
        
        return _slim_alpha_response(await brain_client.create_simulation(sim_data))
    except Exception as e:
        extra_info = ""
        error_msg = str(e)
        if error_msg and "does not support event inputs" in error_msg:
            extra_info = "If fields is vector type  should use vec_* operator with event input"
            return {"error": f"An unexpected error occurred: {str(e)}. {extra_info}"}
        return {"error": f"An unexpected error occurred: {str(e)}"}

# --- Alpha and Data Retrieval Tools ---

@mcp.tool()
async def get_alpha_details(alpha_id: str) -> Dict[str, Any]:
    """
    Get detailed information about an alpha.
    
    Args:
        alpha_id: The ID of the alpha to retrieve
    
    Returns:
        Detailed alpha information
    """
    try:
        return _slim_alpha_response(await brain_client.get_alpha_details(alpha_id))
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_datasets(
    category: Optional[str] = None,
    region: str = "USA",
    delay: int = 1,
    universe: str = "TOP3000",
    theme: str = "false",
    search: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Get available datasets for research.
    
    Use this to discover what data is available for your alpha research.
    
    Args:
        category: Type of datesets (e.g., "news","sentiment","option")
        region: Market region (e.g., "USA")
        delay: Data delay (0 or 1)
        universe: Universe of stocks (e.g., "TOP3000")
        theme: Theme filter
    
    Returns:
        Available datasets
    """
    try:
        return _slim_datasets(await brain_client.get_datasets(category, region, delay, universe, theme, search))
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_datafields(
    region: str,
    dataset_id: Optional[str],
    universe: str,
    delay: int = 1,
    data_type: str = "",
    search: Optional[str] = None,
    filter_sharpe: bool = True,
) -> Dict[str, Any]:
    """
    Get available data fields for alpha construction.
    
    Use this to find specific data fields you can use in your alpha formulas.
    By default, fields with OS/IS Sharpe ratio < 0 are filtered out to improve quality.
    
    Args:
        region: Market region (e.g., "USA"、"GLB"、"IND"、"ASI"、"CHN")
        delay: Data delay (0 or 1)
        universe: Universe of stocks (e.g., USA和GLB默认"TOP3000"、IND默认"TOP500"、ASI默认"MINVOL1M"、CHN默认"TOP2000U")
        dataset_id: Specific dataset ID to filter by
        data_type: Type of data (e.g., "MATRIX",'VECTOR','GROUP')
        search: Search term to filter fields
        filter_sharpe: Filter out fields with OS/IS Sharpe < 0 (default: True)
    
    Returns:
        Available data fields
    """
    instrument_type = "EQUITY"
    theme = "false"
    try:
        return _slim_datafields(await brain_client.get_datafields(instrument_type, region, delay, universe, theme, dataset_id, data_type, search, filter_sharpe))
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_alpha_pnl(alpha_id: str) -> Dict[str, Any]:
    """
    Get PnL (Profit and Loss) data for an alpha.
    
    Args:
        alpha_id: The ID of the alpha
    
    Returns:
        PnL data for the alpha
    """
    try:
        return _slim_pnl(await brain_client.get_alpha_pnl(alpha_id))
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_user_alphas(
    stage: str = "IS",
    limit: int = 30,
    offset: int = 0,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    submission_start_date: Optional[str] = None,
    submission_end_date: Optional[str] = None,
    order: Optional[str] = None,
    hidden: Optional[bool] = None,
    region: Optional[str] = None,
    status: Optional[str] = None,
    type: Optional[str] = None,
    is_super: Optional[bool] = None,
    color: Optional[str] = None,
    name: Optional[str] = None,
    tag: Optional[str] = None,
    language: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Get user's alphas with advanced filtering, pagination, and sorting.

    This tool retrieves a list of your alphas, allowing for detailed filtering based on stage,
    creation date, submission date, visibility, region, status, type, and super alpha flag.
    It also supports pagination and custom sorting.

    Args:
        stage (str): The stage of the alphas to retrieve.
            - "IS": In-Sample (alphas that have not been submitted).
            - "OS": Out-of-Sample (alphas that have been submitted).
            Defaults to "IS".
        limit (int): The maximum number of alphas to return in a single request.
            For example, `limit=50` will return at most 50 alphas. Defaults to 30.
        offset (int): The number of alphas to skip from the beginning of the list.
            Used for pagination. For example, `limit=50, offset=50` will retrieve alphas 51-100.
            Defaults to 0.
        start_date (Optional[str]): The earliest creation date for the alphas to be included.
            Filters for alphas created after this date (strictly greater).
            Example format: "2023-01-01T00:00:00Z". Date-only or timezone-less values
            are accepted and normalized to UTC automatically.
        end_date (Optional[str]): The latest creation date for the alphas to be included.
            Filters for alphas created before this date.
            Example format: "2023-12-31T23:59:59Z". Normalized like start_date.
        submission_start_date (Optional[str]): The earliest submission date for the alphas.
            Only applies to "OS" alphas. Filters for alphas submitted after this date.
            Example format: "2024-01-01T00:00:00Z". Normalized like start_date.
        submission_end_date (Optional[str]): The latest submission date for the alphas.
            Only applies to "OS" alphas. Filters for alphas submitted before this date.
            Example format: "2024-06-30T23:59:59Z". Normalized like start_date.
        order (Optional[str]): The sorting order for the returned alphas.
            Prefix with a hyphen (-) for descending order. Nested fields are supported.
            Examples: "name", "-dateSubmitted", "-is.sharpe", "dateCreated".
        hidden (Optional[bool]): Filter alphas based on their visibility.
            - `True`: Only return hidden alphas.
            - `False`: Only return non-hidden alphas.
            If not provided, both hidden and non-hidden alphas are returned.
        region (Optional[str]): Filter alphas by region (server-side, settings.region).
            Common values: "USA", "EUR", "ASI", "GLB", "CHN", "GBR", etc.
            If not provided, alphas from all regions are returned.
        status (Optional[str]): Filter alphas by status (server-side).
            Common values: "ACTIVE", "UNSUBMITTED", "DECOMMISSIONED", etc.
            If not provided, alphas with any status are returned.
        type (Optional[str]): Filter alphas by their expression type (server-side).
            Common values: "REGULAR", "SUPER", etc.
            If not provided, alphas of all types are returned.
        is_super (Optional[bool]): Filter to only super alphas (True) or non-super alphas (False).
            Applied server-side as type=SUPER / type!=SUPER. If not provided, both are returned.
        color (Optional[str]): Filter alphas by their color label (server-side).
            Values: "RED", "GREEN", "BLUE", "YELLOW", "PURPLE" (case-insensitive here,
            normalized to uppercase). If not provided, alphas of any color are returned.
        name (Optional[str]): Filter alphas by name (server-side, EXACT match only —
            the API has no substring/fuzzy name matching).
        tag (Optional[str]): Filter alphas that carry this tag (server-side, exact tag
            value, e.g. "PowerPoolSelected"). One tag per query.
        language (Optional[str]): Filter alphas by expression language (server-side,
            settings.language). Values: "FASTEXPR", "PYTHON" (case-insensitive,
            normalized to uppercase). If not provided, alphas of all languages
            are returned.

    All filters are applied server-side by the BRAIN API, so pagination (count/limit/offset)
    reflects the filtered set directly.

    Returns:
        Dict[str, Any]: A dictionary containing a list of alpha details under the 'results' key,
        along with pagination information. If an error occurs, it returns a dictionary with an 'error' key.
    """
    try:
        return _slim_alpha_list(await brain_client.get_user_alphas(
            stage=stage, limit=limit, offset=offset, start_date=start_date,
            end_date=end_date, submission_start_date=submission_start_date,
            submission_end_date=submission_end_date, order=order, hidden=hidden,
            region=region, status=status, alpha_type=type, is_super=is_super,
            color=color, name=name, tag=tag, language=language,
        ))
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def submit_alpha(alpha_id: str) -> Dict[str, Any]:
    """
    Start an ASYNCHRONOUS submission of an alpha, with pre-submission IS metrics check.

    The platform-side submission check takes from a few minutes up to ~1 hour, so this
    tool does NOT wait for the result. It validates the alpha, starts a background
    submission task, and returns immediately. Use get_submission_status(alpha_id) to
    poll the outcome (polling every 1-5 minutes is enough; do not busy-wait).

    Before starting, this tool automatically checks the alpha's IS metrics against
    the submission thresholds (Sharpe, Fitness, Margin, Turnover, Returns, and all
    IS checks must not FAIL). If the check fails, submission is blocked and failure
    details are returned.

    Args:
        alpha_id: The ID of the alpha to submit
    Returns:
        Immediate acknowledgement that the background submission started (or the
        reason it was blocked). The final result must be read via
        get_submission_status(alpha_id).
    """
    try:
        # Fetch alpha details for IS metrics check
        alpha_details = await brain_client.get_alpha_details(alpha_id)

        stage = alpha_details.get('stage')
        if stage == 'OS' or alpha_details.get('dateSubmitted'):
            return {
                "success": False,
                "blocked": True,
                "reason": f"Alpha {alpha_id} is already submitted (stage={stage}).",
            }

        check_result = brain_client.pre_submit_check(alpha_details)
        if not check_result['passed']:
            return {
                "success": False,
                "blocked": True,
                "reason": "Pre-submission IS metrics check failed. Alpha does not meet submission thresholds.",
                "check_result": check_result,
            }

        # Passed check — start background submission and return immediately
        start = brain_client.start_submission(alpha_id, pre_check=check_result)
        return {
            "async": True,
            "blocked": False,
            "submission": start,
            "message": (
                f"Submission of {alpha_id} started in the background; the platform check "
                f"may take from a few minutes up to ~1 hour. Poll get_submission_status('{alpha_id}') "
                f"every few minutes for the final result."
            ),
            "check_result": check_result,
        }
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_submission_status(alpha_id: str) -> Dict[str, Any]:
    """
    Check the status of an asynchronous alpha submission started via submit_alpha.

    Returns the tracked background-task state: status is one of QUEUED, RUNNING
    (with phase/attempts/polls progress), or a terminal status — SUCCESS,
    ALREADY_SUBMITTED, FAILED (with failed_checks), TIMEOUT, ERROR. The 'done'
    field is true once the submission reached a terminal status.

    If the server was restarted and no in-memory record exists, it falls back to
    probing the platform (alpha stage / dateSubmitted) to tell whether the alpha
    is submitted.

    Args:
        alpha_id: The ID of the alpha whose submission to check
    Returns:
        Submission status snapshot
    """
    try:
        return await brain_client.get_submission_status(alpha_id)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def value_factor_trendScore(start_date: str, end_date: str) -> Dict[str, Any]:
    """Compute and return the diversity score for REGULAR alphas in a submission-date window.
    This function calculate the diversity of the users' submission, by checking the diversity, we can have a good understanding on the valuefactor's trend.
    This MCP tool wraps BrainApiClient.value_factor_trendScore and always uses submission dates (OS).

    Inputs:
        - start_date: ISO UTC start datetime (e.g. '2025-08-14T00:00:00Z')
        - end_date: ISO UTC end datetime (e.g. '2025-08-18T23:59:59Z')
        - p_max: optional integer total number of pyramid categories for normalization

    Returns: compact JSON with diversity_score, N, A, P, P_max, S_A, S_P, S_H, per_pyramid_counts
    """
    try:
        return await brain_client.value_factor_trendScore(start_date=start_date, end_date=end_date)
    except Exception as e:
        return {"error": str(e)}

# --- Community and Events Tools ---

@mcp.tool()
async def get_events() -> Dict[str, Any]:
    """
    Get available events and competitions.
    
    Returns:
        Available events and competitions
    """
    try:
        return await brain_client.get_events()
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_leaderboard(user_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Get leaderboard data.
    
    Args:
        user_id: Optional user ID to filter results
    
    Returns:
        Leaderboard data
    """
    try:
        return await brain_client.get_leaderboard(user_id)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}


# --- SPC (Systematic Predictions Challenge) Tools ---

@mcp.tool()
async def get_spc_submissions(limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """
    List the current user's SPC (Systematic Predictions Challenge) prompt submissions.

    Args:
        limit: Maximum number of submissions to return (default: 50)
        offset: Pagination offset (default: 0)

    Returns:
        Paginated list of submissions with id, name, prompt, sampleOutput, model,
        modelVersion, weight, updateFrequency, lastModified, and status
    """
    try:
        return await brain_client.get_spc_submissions(limit, offset)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def create_spc_submission(
    name: str,
    prompt: str,
    sample_output: str,
    model: str,
    model_version: str,
    weight: float,
    update_frequency: str,
    skip_validation: bool = False,
) -> Dict[str, Any]:
    """
    Create a new SPC (Systematic Predictions Challenge) prompt submission.

    The prompt is run periodically by the platform on the chosen model; its JSON
    output (ISIN|MIC keys, confidence scores in [-1, 1]) forms a long/short
    portfolio whose PnL is scored. Local validation of the sample output
    (JSON shape, ISIN|MIC format, ISIN checksum, score range) runs before
    submitting; failures are returned without submitting.

    Args:
        name: Submission name (max 200 characters)
        prompt: English prompt text sent to the model (max 10000 characters)
        sample_output: Sample JSON output produced by the prompt, as a string.
            Must be a pure JSON object mapping "ISIN|MIC" to numeric scores in [-1, 1]
        model: One of gpt, claude, gemini, deepseek, kimi, qwen, glm, llama, minimax, mistral
        model_version: Model version string, e.g. "5" or "4.8" (max 100 characters)
        weight: Prompt weight between 0 and 1 (two decimals). 0 means the prompt does not run
        update_frequency: One of daily, weekly, monthly, quarterly
        skip_validation: Submit even if local validation fails (default: False)

    Returns:
        The created submission (including its id), or validation errors
    """
    try:
        return await brain_client.create_spc_submission(
            name, prompt, sample_output, model, model_version, weight, update_frequency, skip_validation
        )
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def set_spc_submission_weight(submission_id: str, weight: float) -> Dict[str, Any]:
    """
    Set the weight of an existing SPC submission. Setting weight to 0 withdraws it.

    Weight is the ONLY field the platform allows changing after creation; there
    is no DELETE, and prompt text, model, and frequency are immutable. To change
    a prompt's content, create a new submission with create_spc_submission and
    set the old one's weight to 0. Use get_spc_submissions to find ids.

    Args:
        submission_id: Id of the submission to update (e.g. "V45nl1y")
        weight: New weight between 0 and 1 (two decimals); 0 withdraws the prompt

    Returns:
        The updated submission, or validation errors
    """
    try:
        return await brain_client.set_spc_submission_weight(submission_id, weight)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_spc_leaderboard(
    board: Optional[str] = None,
    limit: int = 30,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    Get the SPC (Systematic Predictions Challenge) monthly leaderboard.

    Args:
        board: Month key like "202607" (default: current month, chosen server-side)
        limit: Maximum number of entries to return (default: 30)
        offset: Pagination offset (default: 0)

    Returns:
        Leaderboard entries aggregated by user
    """
    try:
        return await brain_client.get_spc_leaderboard(board, limit, offset)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}


# --- Forum Tools ---

@mcp.tool()
async def get_operators() -> Dict[str, Any]:
    """
    Get available operators for alpha creation.
    
    Returns:
        Dictionary containing operators list and count
    """
    try:
        operators = await brain_client.get_operators()
        if isinstance(operators, list):
            return _slim_text_lookup({"results": operators, "count": len(operators)}, n=160)
        return _slim_text_lookup(operators, n=160)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def run_selection(
    selection: str,
    instrument_type: str = "EQUITY",
    region: str = "USA",
    delay: int = 1,
    selection_limit: int = 1000,
    selection_handling: str = "POSITIVE",
) -> Dict[str, Any]:
    """
    Run a selection query to filter instruments.
    
    Args:
        selection: Selection criteria
        instrument_type: Type of instruments
        region: Geographic region
        delay: Delay setting
        selection_limit: Maximum number of results
        selection_handling: How to handle selection results
    
    Returns:
        Selection results
    """
    try:
        return await brain_client.run_selection(
            selection, instrument_type, region, delay, selection_limit, selection_handling
        )
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_user_profile(user_id: str = "self") -> Dict[str, Any]:
    """
    Get user profile information.
    
    Args:
        user_id: User ID (default: "self" for current user)
    
    Returns:
        User profile data
    """
    try:
        return await brain_client.get_user_profile(user_id)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_documentations() -> Dict[str, Any]:
    """
    Get available documentations and learning materials.
    
    Returns:
        List of documentations
    """
    try:
        return await brain_client.get_documentations()
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

# --- Message and Forum Tools ---

@mcp.tool()
async def get_messages(limit: Optional[int] = None, offset: int = 0) -> Dict[str, Any]:
    """
    Get messages for the current user with optional pagination.
    
    Args:
        limit: Maximum number of messages to return (e.g., 10 for top 10 messages)
        offset: Number of messages to skip (for pagination)
    
    Returns:
        Messages for the current user, optionally limited by count
    """
    try:
        return await brain_client.get_messages(limit, offset)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_glossary_terms(email: str = "", password: str = "") -> List[Dict[str, str]]:
    """
    Get glossary terms from WorldQuant BRAIN forum.
    
    Note: This uses Playwright and is implemented in forum_functions.py
    
    Args:
        email: Your BRAIN platform email address (optional if in config)
        password: Your BRAIN platform password (optional if in config)
    
    Returns:
        A list of glossary terms with definitions
    """
    try:
        config = load_config()
        credentials = config.get("credentials", {})
        email = email or credentials.get("email")
        password = password or credentials.get("password")
        if not email or not password:
            raise ValueError("Authentication credentials not provided or found in config.")
        
        return await brain_client.get_glossary_terms(email, password)
    except Exception as e:
        logger.error(f"Error in get_glossary_terms tool: {e}")
        return [{"error": str(e)}]

@mcp.tool()
async def search_forum_posts(search_query: str, email: str = "", password: str = "", 
                             max_results: int = 50) -> Dict[str, Any]:
    """
    Search forum posts on WorldQuant BRAIN support site.
    
    Note: This uses Playwright and is implemented in forum_functions.py
    
    Args:
        search_query: Search term or phrase
        email: Your BRAIN platform email address (optional if in config)
        password: Your BRAIN platform password (optional if in config)
        max_results: Maximum number of results to return (default: 50)
    
    Returns:
        Search results with analysis
    """
    try:
        config = load_config()
        credentials = config.get("credentials", {})
        email = email or credentials.get("email")
        password = password or credentials.get("password")
        if not email or not password:
            return {"error": "Authentication credentials not provided or found in config."}
            
        return await brain_client.search_forum_posts(email, password, search_query, max_results)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def read_forum_post(article_id: str, email: str = "", password: str = "", 
                          include_comments: bool = True) -> Dict[str, Any]:
    """
    Get a specific forum post by article ID.
    
    Note: This uses Zendesk support SSO plus JSON APIs and is implemented in forum_functions.py
    
    Args:
        article_id: The article ID to retrieve (e.g., "32984819083415-新人求模板")
        email: Your BRAIN platform email address (optional if in config)
        password: Your BRAIN platform password (optional if in config)
    
    Returns:
        Forum post content with comments
    """
    try:
        config = load_config()
        credentials = config.get("credentials", {})
        email = email or credentials.get("email")
        password = password or credentials.get("password")
        if not email or not password:
            return {"error": "Authentication credentials not provided or found in config."}

        return await brain_client.read_forum_post(email, password, article_id, include_comments)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_alpha_yearly_stats(alpha_id: str) -> Dict[str, Any]:
    """Get yearly statistics for an alpha."""
    try:
        return _slim_yearly(await brain_client.get_alpha_yearly_stats(alpha_id))
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def check_correlation(alpha_id: str) -> Dict[str, Any]:
    """Check alpha correlation against production alphas, self alphas, or both."""
    correlation_type = "both"
    threshold = 0.7
    try:
        return _slim_check_correlation(await brain_client.check_correlation(alpha_id, correlation_type, threshold))
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def check_self_correlation(
    alpha_id: str,
    threshold: float = 0.7,
    correlation_type: str = 'self',
) -> Dict[str, Any]:
    """Validate self-correlation with the local incremental-cache calculation.

    This does not call the BRAIN /correlations/self endpoint, so it does not
    consume the platform correlation slot or use the correlation lock.

    The OS pool is partitioned by each alpha's ``classifications`` to match the
    platform exactly (the platform reports two numbers from the same pool):
      * correlation_type='self' (default) -> "Self Correlation": pool EXCLUDES
        Power Pool Alphas. Use this to mirror the submission "Self Correlation".
      * correlation_type='powerpool' -> "Power Pool Correlation": pool is ONLY
        Power Pool Alphas.
      * correlation_type='all' -> legacy whole-pool behaviour (mixes both;
        can over-report vs the platform's Self Correlation).

    Args:
        alpha_id: Target alpha ID.
        threshold: Pass/fail threshold applied to each max correlation
            (passes when max < threshold). Default 0.7.
        correlation_type: 'self' | 'powerpool' | 'all'. Default 'self'.

    Returns:
        Dict with the local max self-correlation, pass/fail result, top
        correlated OS alpha records, and pool-partition metadata
        (full_os_pool_size, excluded_power_pool_count, correlation_type).
    """
    try:
        return _slim_check_correlation(await brain_client.check_self_correlation(
            alpha_id,
            threshold=threshold,
            correlation_type=correlation_type,
        ))
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def compute_mutual_correlation(
    alpha_ids: List[str],
    threshold: float = 0.5,
    years: int = 4,
) -> Dict[str, Any]:
    """Compute pairwise ("mutual") correlation AMONG a given set of your alphas.

    Use this to vet a submission basket that must be mutually decorrelated
    (e.g. a "no two alphas may correlate above 0.5" rule). It is fully local —
    it fetches each alpha's PnL and correlates their daily returns; it does NOT
    call the BRAIN correlation endpoint or consume the correlation slot.

    Distinct from the other two correlation tools:
      * check_correlation      -> target vs the PRODUCTION pool.
      * check_self_correlation -> target vs your submitted-OS pool
                                  (Self excl. Power Pool Alphas / Power Pool only).
      * compute_mutual_correlation (this) -> the full NxN matrix AMONG the
                                  supplied alphas themselves.

    Correlation is on the last ``years`` of daily returns (diff of cumulative
    PnL), matching the local self-correlation convention.

    Args:
        alpha_ids: 2+ alpha IDs to correlate against each other.
        threshold: Max acceptable pairwise correlation (default 0.5).
        years: Trailing window of daily returns to use (default 4).

    Returns:
        Dict with: matrix (NxN), max_pair (most-correlated pair),
        pairs_over_threshold, all_below_threshold (bool), and
        max_mutually_below_subset (a greedy maximal basket whose members are all
        mutually below threshold), plus missing_pnl for any unfetchable ids.
    """
    try:
        return await brain_client.get_mutual_correlation(alpha_ids, threshold=threshold, years=years)
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def set_alpha_properties(alpha_id: str, name: Optional[str] = None, 
                               color: Optional[str] = None, tags: Optional[List[str]] = None,
                               descriptions: str = "None",
                               selection_description: Optional[str] = None,
                               combo_description: Optional[str] = None) -> Dict[str, Any]:
    """
      Note: Update alpha properties (name, color, tags, descriptions).
      For SUPER alphas, selection_description and combo_description are also required and must
      each be at least 100 English characters.
      Args:
        color: may be one of `RED` `GREEN` `YELLOW` `BLUE` `PURPLE`；
        name: 使用生产相关性和自相关性命名，不能带空格；建议基于 correlation
        的最大值命名，例如 `0.6534_0.2306` 表示 prod correlation = 0.6534, self correlation = 0.2306；
        tags 如果alpha质量非常好可以设置 `PowerPoolSelected`，质量不好或不好判断就不要设置tag；
        descriptions: Write in English, <=100 words. The three sections MUST be separated by
        actual newline characters (i.e. use the JSON escape sequence \\n\\n between sections,
        NOT the literal text "\\n\\n"). Example value:
        "Idea: <your idea here>\\n\\nRationale for data used: <your rationale>\\n\\nRationale for operators used: <your rationale>"
        The three section headers must appear exactly as:
        - Idea:
        - Rationale for data used:
        - Rationale for operators used:
        selection_description: (SUPER alpha only) Description of the selection expression logic.
        Must be at least 100 English characters. Write in English.
        combo_description: (SUPER alpha only) Description of the combo expression logic.
        Must be at least 100 English characters. Write in English.
    """
    try:
        if descriptions and descriptions == "None":
            return {
                "error": (
                    "descriptions cannot be the literal string 'None'. "
                    "Please regenerate it in English using exactly these three sections: "
                    "Idea:, Rationale for data used:, and Rationale for operators used:."
                )
            }
        # Normalize literal \n sequences to actual newlines in case the LLM emits
        # backslash-n as two characters rather than a true newline escape.
        if descriptions and descriptions != "None":
            descriptions = descriptions.replace('\\n', '\n')
        return _slim_alpha_response(await brain_client.set_alpha_properties(alpha_id, name, color, tags, descriptions,
                                                       selection_description, combo_description))
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_record_sets(alpha_id: str) -> Dict[str, Any]:
    """List available record sets for an alpha."""
    try:
        return await brain_client.get_record_sets(alpha_id)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_record_set_data(alpha_id: str, record_set_name: str) -> Dict[str, Any]:
    """Get data from a specific record set."""
    try:
        return await brain_client.get_record_set_data(alpha_id, record_set_name)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_user_activities(user_id: str, grouping: Optional[str] = None) -> Dict[str, Any]:
    """Get user activity diversity data."""
    try:
        return await brain_client.get_user_activities(user_id, grouping)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_pyramid_multipliers() -> Dict[str, Any]:
    """Get current pyramid multipliers showing BRAIN's encouragement levels."""
    try:
        return _slim_pyramids(await brain_client.get_pyramid_multipliers(), "multipliers")
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_pyramid_alphas(start_date: Optional[str] = None,
                               end_date: Optional[str] = None) -> Dict[str, Any]:
    """Get user's current alpha distribution across pyramid categories.
    Defaults to the current quarter if no dates are provided."""
    try:
        return _slim_pyramids(await brain_client.get_pyramid_alphas(start_date, end_date), "alphas")
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}


# --- Candidate pool ------------------------------------------------------- #
# BRAIN accepts only ~4 regular-alpha submissions per day, so qualified alphas
# queue in a local pool. Two facts make the pool more than a list:
#   1. Pyramid planning must count submitted + pooled together.
#   2. Submitting one alpha RAISES every other candidate's production and self
#      correlation, because the submitted alpha joins the pool they are measured
#      against:  projected_corr(B) = max(corr_now(B), |corr(B, submitted)|).
# The pool therefore enforces pairwise |corr| < prod threshold at admission —
# the only moment when acting on it is still cheap.

@mcp.tool()
async def pool_check(
    alpha_id: str,
    prod_threshold: float = 0.70,
    self_threshold: float = 0.70,
    mutual_threshold: float = 0.40,
    refresh_prod: bool = True,
) -> Dict[str, Any]:
    """Dry-run: may this alpha join the candidate pool? Does NOT modify the pool.

    Checks four things and reports every violation rather than stopping at the
    first: the alpha's own production correlation, its own self correlation, its
    pairwise correlation against each pooled candidate versus ``prod_threshold``
    (SAFETY — a pair at/above this means submitting either one pushes the other
    past the production gate), and versus ``mutual_threshold`` (DIVERSITY, the
    basket rule).

    Pairwise correlations are computed locally from PnL and consume no BRAIN
    correlation slot; only the alpha's own production correlation hits the
    rate-limited endpoint (skip it with refresh_prod=false when cached).
    """
    try:
        return await cpool.evaluate_candidate(
            brain_client, alpha_id, cpool.load_pool(),
            prod_threshold=prod_threshold,
            self_threshold=self_threshold,
            mutual_threshold=mutual_threshold,
            refresh_prod=refresh_prod,
        )
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}


@mcp.tool()
async def pool_add(
    alpha_id: str,
    note: Optional[str] = None,
    force: bool = False,
    allow_diversity_fail: bool = False,
    prod_threshold: float = 0.70,
    self_threshold: float = 0.50,
    mutual_threshold: float = 0.40,
    refresh_prod: bool = True,
) -> Dict[str, Any]:
    """Admit an alpha to the candidate pool after running the pool_check gates.

    Rejected candidates are reported with reasons and NOT stored. ``force=true``
    admits anyway and records ``forced_reasons`` on the entry so the compromise
    stays visible in later planning. ``allow_diversity_fail=true`` waives only
    the mutual-correlation basket rule, never the production-safety rule.

    This never submits anything.
    """
    try:
        return await cpool.add_candidate(
            brain_client, alpha_id,
            note=note, force=force, allow_diversity_fail=allow_diversity_fail,
            prod_threshold=prod_threshold,
            self_threshold=self_threshold,
            mutual_threshold=mutual_threshold,
            refresh_prod=refresh_prod,
        )
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}


@mcp.tool()
async def pool_remove(alpha_ids: List[str]) -> Dict[str, Any]:
    """Drop alphas from the candidate pool (e.g. after manual submission)."""
    try:
        return cpool.remove_candidates(alpha_ids)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}


@mcp.tool()
async def pool_list(region: Optional[str] = None,
                    pyramid: Optional[str] = None) -> Dict[str, Any]:
    """List pooled candidates with metrics and correlations, newest gates first."""
    try:
        return cpool.list_pool(region=region, pyramid=pyramid)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}


@mcp.tool()
async def pool_pyramid_coverage(
    region: Optional[str] = None,
    delay: Optional[int] = None,
    target: int = 3,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Dict[str, Any]:
    """True pyramid coverage: submitted alphas AND pooled candidates, per pyramid.

    A pyramid lights on SUBMITTED alphas only (``target`` of them, default 3).
    The pool column is the queue that can get it there, so each row reports
    ``needed_submissions`` and whether the pool can cover it:
      OS_SUFFICIENT                    already lit
      NEEDS_<n>_SUBMISSIONS_FROM_POOL  pool has enough candidates queued
      SHORT_BY_<n>_CANDIDATES          research still needed

    Omit region/delay to aggregate across all of them.
    """
    try:
        return await cpool.pyramid_coverage(
            brain_client, region=region, delay=delay, target=target,
            start_date=start_date, end_date=end_date,
        )
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}


@mcp.tool()
async def pool_submission_plan(
    max_submissions: int = 4,
    region: Optional[str] = None,
    delay: Optional[int] = None,
    target: int = 3,
    prod_threshold: float = 0.70,
    self_threshold: float = 0.70,
    resolve_conflicts: bool = False,
) -> Dict[str, Any]:
    """Pick today's submission batch and prove it is safe for the rest of the pool.

    Ranks candidates by unmet pyramid need, then pyramid multiplier, then Sharpe,
    and takes up to ``max_submissions`` (BRAIN's daily regular-alpha cap is 4).
    A candidate is skipped when it clashes with one already selected, or when
    submitting it would push a candidate left in the pool past a gate:

        projected_prod_corr(B) = max(prod_corr_now(B), max |corr(B, batch)|)
        projected_self_corr(B) = max(self_corr_now(B), max |corr(B, batch)|)

    ``remaining_pool_after_batch`` lists that projection for every candidate left
    behind, and ``all_remaining_safe`` is the single answer to "will submitting
    this batch break anything still queued?".

    If two pooled candidates are mutually exclusive (submitting either destroys
    the other), a purely protective plan would submit neither and stall forever.
    Those pairs appear in ``conflicts`` with a recommended keep/drop;
    ``resolve_conflicts=true`` acts on the recommendation and lists the give-ups
    in ``sacrificed``.

    Returns a plan only — the agent never submits.
    """
    try:
        return await cpool.submission_plan(
            brain_client,
            max_submissions=max_submissions, region=region, delay=delay,
            target=target, prod_threshold=prod_threshold,
            self_threshold=self_threshold, resolve_conflicts=resolve_conflicts,
        )
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}


@mcp.tool()
async def pool_sync(refresh_prod: bool = False) -> Dict[str, Any]:
    """Refresh pooled entries; drop any that are now submitted (status ACTIVE).

    ``refresh_prod=true`` also re-queries production correlation per entry. That
    endpoint is single-concurrency and slow, so it is off by default — use it
    after you submit something, since submissions move everyone's numbers.
    """
    try:
        return await cpool.sync_pool(brain_client, refresh_prod=refresh_prod)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}


@mcp.tool()
async def recommend_datasets(
    region: str = "USA",
    delay: int = 1,
    universe: str = "TOP3000",
    top_n: int = 20,
) -> Dict[str, Any]:
    """
    Recommend datasets for alpha construction with unlit pyramid priority:
    
    1. **Pyramid lighting (点塔)**: Uses the pyramid-alphas and pyramid-multipliers
       endpoints. Unlit pyramids (fewer than 3 alphas) are recommended first.
    2. **Dataset quality**: Ranks datasets by OS/IS Sharpe within the same pyramid.
    3. **Dataset popularity**: Favors datasets with more platform users and more
       submitted alphas (dataset userCount and alphaCount).
    4. **Randomness**: Adds a small random score so recommendations keep some variety.
    
    Each dataset gets a score (0~95):
    - Pyramid lighting: 0~40 pts
    - Dataset quality: 0~30 pts
    - Dataset users: 0~10 pts
    - Dataset submissions: 0~10 pts
    - Randomness: 0~5 pts
    
    Args:
        region: Market region (e.g., "USA", "CHN", "EUR", "ASI", "GLB")
        delay: Data delay (0 or 1)
        universe: Stock universe (e.g., "TOP3000")
        top_n: Number of top recommendations to return (default 20)
    
    Returns:
        Ranked dataset recommendations with scores, pyramid status summary,
        and neutralization options for the selected region/delay/universe.
    """
    try:
        return await brain_client.recommend_datasets(region, delay, universe, top_n)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}
        
@mcp.tool()
async def get_user_competitions(user_id: Optional[str] = None) -> Dict[str, Any]:
    """Get list of competitions that the user is participating in."""
    try:
        return await brain_client.get_user_competitions(user_id)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_competition_details(competition_id: str) -> Dict[str, Any]:
    """Get detailed information about a specific competition."""
    try:
        return await brain_client.get_competition_details(competition_id)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_competition_agreement(competition_id: str) -> Dict[str, Any]:
    """Get the rules, terms, and agreement for a specific competition."""
    try:
        return await brain_client.get_competition_agreement(competition_id)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def get_platform_setting_options() -> Dict[str, Any]:
    """Discover valid simulation setting options (instrument types, regions, delays, universes, neutralization).

    Use this when a simulation request might contain an invalid/mismatched setting. If an AI or user supplies
    incorrect parameters (e.g., wrong region for an instrument type), call this tool to retrieve the authoritative
    option sets and correct the inputs before proceeding.

    Returns:
        A structured list of valid combinations and choice lists to validate or fix simulation settings.
    """
    try:
        return _slim_text_lookup(await brain_client.get_platform_setting_options(), n=300)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

@mcp.tool()
async def performance_comparison(alpha_id: str, competition: Optional[str] = None,
                                 team_id: Optional[str] = None) -> Dict[str, Any]:
    """Get the mandatory pre-submission comparison for an unsubmitted IS alpha.

    Call and save this check before submission.  BRAIN does not retain it for
    this API after an alpha moves to OS; an OS result means the report can no
    longer be retrieved, not that the endpoint is broken.  This tool never uses
    the removed legacy ``/alphas/{id}/performance-comparison`` route.

    Every non-empty valid response is automatically archived under
    ``D:\\wqb_tuzige\\tracking\\performance_comparison`` (override with
    ``BRAIN_PERFORMANCE_COMPARISON_ARCHIVE``).  The returned ``_archive`` field
    records whether and where it was saved.

    Args:
        alpha_id: The alpha ID (e.g. "A1wYQ2xd" or "XgpEr77l").
        competition: Optional competition ID (e.g. "PAC2026"). If omitted,
            the user's own (self) alpha endpoint is used.
        team_id: Optional team ID.
    """
    try:
        return await brain_client.performance_comparison(alpha_id, competition, team_id)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}
        
# --- Dataframe Tool ---

@mcp.tool()
async def expand_nested_data(data: List[Dict[str, Any]], preserve_original: bool = True) -> List[Dict[str, Any]]:
    """Flatten complex nested data structures into tabular format."""
    try:
        return await brain_client.expand_nested_data(data, preserve_original)
    except Exception as e:
        return [{"error": f"An unexpected error occurred: {str(e)}"}]
        
# --- Documentation Tool ---

@mcp.tool()
async def get_documentation_page(page_id: str) -> Dict[str, Any]:
    """Retrieve detailed content of a specific documentation page/article."""
    try:
        return await brain_client.get_documentation_page(page_id)
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

# --- Advanced Simulation Tools ---

@mcp.tool()
async def create_multi_simulation(
    alpha_expressions: List[str],
    instrument_type: str = "EQUITY",
    region: str = "USA",
    universe: str = "TOP3000",
    delay: int = 1,
    decay: int = 4,
    neutralization: str = "INDUSTRY",
    truncation: float = 0.0,
    test_period: str = "P0Y0M",
    unit_handling: str = "VERIFY",
    nan_handling: str = "OFF",
    language: str = "FASTEXPR",
    lookback: Optional[int] = None,
    visualization: bool = False,
    pasteurization: str = "ON",
    max_trade: str = "OFF"
) -> Dict[str, Any]:
    """
    🚀 Create multiple regular alpha simulations on BRAIN platform in a single request.
    
    This tool creates a multisimulation with multiple regular alpha expressions,
    waits for all simulations to complete, and returns detailed results for each alpha.
    
    ⏰ NOTE: Multisimulations can take 8+ minutes to complete. This tool will wait
    for the entire process and return comprehensive results.
    Call get_platform_setting_options to get the valid options for the simulation.
    Args:
        alpha_expressions: List of alpha expressions/code strings (2-10 expressions required)
        instrument_type: Type of instruments (default: "EQUITY")
        region: Market region (default: "USA")
        universe: Universe of stocks (default: "TOP3000")
        delay: Data delay (default: 1)
        decay: Decay value (default: 4)
        neutralization: Neutralization method (default: "NONE")
        truncation: Truncation value (default: 0.0)
        test_period: Test period (default: "P0Y0M")
        unit_handling: Unit handling method. Used for FASTEXPR simulations.
        nan_handling: NaN handling method. Used for FASTEXPR simulations.
        language: Expression language ("FASTEXPR" or "PYTHON")
        lookback: Historical lookback window. Only used for PYTHON simulations; defaults to 256 for PYTHON.
        visualization: Enable visualization (default: False)
        pasteurization: Pasteurization setting (default: "ON")
        max_trade: Max trade setting (default: "OFF")
    
    Returns:
        Dictionary containing multisimulation results and individual alpha details
    """
    try:
        # Validate input
        if len(alpha_expressions) < 2:
            return {"error": "At least 2 alpha expressions are required"}
        if len(alpha_expressions) > 10:
            return {"error": "Maximum 10 alpha expressions allowed per request"}

        await brain_client.ensure_authenticated()
        normalized_language = language.upper()
        
        # Create multisimulation data
        multisimulation_data = []
        for alpha_expr in alpha_expressions:
            settings = {
                'instrumentType': instrument_type,
                'region': region,
                'universe': universe,
                'delay': delay,
                'decay': decay,
                'neutralization': neutralization,
                'truncation': truncation,
                'pasteurization': pasteurization,
                'language': normalized_language,
                'visualization': visualization,
                'testPeriod': test_period,
                'maxTrade': max_trade
            }

            if normalized_language == "PYTHON":
                settings['lookback'] = 256 if lookback is None else lookback
            else:
                settings['unitHandling'] = unit_handling
                settings['nanHandling'] = nan_handling

            simulation_item = {
                'type': 'REGULAR',
                'settings': settings,
                'regular': alpha_expr
            }
            multisimulation_data.append(simulation_item)
        
        # Send multisimulation request
        response = await brain_client._request('POST', f"{brain_client.base_url}/simulations", json=multisimulation_data)
        
        if response.status_code != 201:
            return {
                "error": f"Failed to create multisimulation. Status: {response.status_code}",
                "details": response.text,
            }
        
        # Get multisimulation location
        location = response.headers.get('Location', '')
        if not location:
            return {"error": "No location header in multisimulation response"}
        
        # Wait for children to appear and get results
        return _slim_multisim(await _wait_for_multisimulation_completion(location, len(alpha_expressions)))
        
    except Exception as e:
        return {"error": f"Error creating multisimulation: {str(e)}"}

async def _wait_for_multisimulation_completion(location: str, expected_children: int) -> Dict[str, Any]:
    """Wait for multisimulation to complete and return results"""
    try:
        # Simple progress indicator for users
        print(f"Waiting for multisimulation to complete... (this may take several minutes)", file=sys.stderr)
        print(f"Expected {expected_children} alpha simulations", file=sys.stderr)
        print("", file=sys.stderr)
        # Wait for children to appear - much more tolerant for 8+ minute multisimulations
        children = []
        max_wait_attempts = 200  # Increased significantly for 8+ minute multisimulations
        wait_attempt = 0
        
        while wait_attempt < max_wait_attempts and len(children) == 0:
            wait_attempt += 1
            
            try:
                multisim_response = await brain_client._request('GET', location)
                if multisim_response.status_code == 200:
                    multisim_data = multisim_response.json()
                    children = multisim_data.get('children', [])
                    
                    if children:
                        break
                    else:
                        # Wait before next attempt - use longer intervals for multisimulations
                        retry_after = multisim_response.headers.get("Retry-After", 5)
                        wait_time = float(retry_after)
                        await asyncio.sleep(wait_time)
            except Exception as e:
                await asyncio.sleep(5)
        
        if not children:
            return {"error": f"Children did not appear within {max_wait_attempts} attempts (multisimulation may still be processing)"}
        
        # Process each child to get alpha results
        alpha_results = []
        for i, child_id in enumerate(children):
            try:
                # The children are full URLs, not just IDs
                child_url = child_id if child_id.startswith('http') else f"{brain_client.base_url}/simulations/{child_id}"
                
                # Wait for this alpha to complete - more tolerant timing
                finished = False
                max_alpha_attempts = 100  # Increased for longer alpha processing
                alpha_attempt = 0
                
                while not finished and alpha_attempt < max_alpha_attempts:
                    alpha_attempt += 1
                    
                    try:
                        alpha_progress = await brain_client._request('GET', child_url)
                        if alpha_progress.status_code == 200:
                            alpha_data = alpha_progress.json()
                            retry_after = alpha_progress.headers.get("Retry-After", 0)
                            
                            if retry_after == 0:
                                finished = True
                                break
                            else:
                                wait_time = float(retry_after)
                                await asyncio.sleep(wait_time)
                        else:
                            await asyncio.sleep(5)
                    except Exception as e:
                        await asyncio.sleep(5)
                
                if finished:
                    # Get alpha details from the completed simulation
                    alpha_id = alpha_data.get("alpha")
                    if alpha_id:
                        # Now get the actual alpha details from the alpha endpoint
                        alpha_details = await brain_client._request('GET', f"{brain_client.base_url}/alphas/{alpha_id}")
                        if alpha_details.status_code == 200:
                            alpha_detail_data = alpha_details.json()
                            alpha_results.append({
                                'alpha_id': alpha_id,
                                'location': child_url,
                                'details': alpha_detail_data
                            })
                        else:
                            alpha_results.append({
                                'alpha_id': alpha_id,
                                'location': child_url,
                                'error': f'Failed to get alpha details: {alpha_details.status_code}'
                            })
                    else:
                        alpha_results.append({
                            'location': child_url,
                            'error': 'No alpha ID found in completed simulation'
                        })
                else:
                    alpha_results.append({
                        'location': f"child_{i+1}",
                        'error': f'Alpha simulation did not complete within {max_alpha_attempts} attempts'
                    })
                    
            except Exception as e:
                alpha_results.append({
                    'location': f"child_{i+1}",
                    'error': str(e)
                })
        
        # Return comprehensive results
        print(f"Multisimulation completed! Retrieved {len(alpha_results)} alpha results", file=sys.stderr)
        return {
            'success': True,
            'message': f'Successfully created {expected_children} regular alpha simulations',
            'total_requested': expected_children,
            'total_created': len(alpha_results),
            'multisimulation_id': location.split('/')[-1],
            'multisimulation_location': location,
            'alpha_results': alpha_results
        }
        
    except Exception as e:
        return {"error": f"Error waiting for multisimulation completion: {str(e)}"}

# --- Three-stage multisimulation tools (throttling-era pattern) --------------
# Split of create_multi_simulation into submit / check / fetch so callers
# control the polling cadence, see rate-limit signals (429 + Retry-After,
# CONCURRENT/DAILY_SIMULATION_LIMIT bodies, per-child CANCELLED statuses),
# and never need multi-minute blocking tool calls.

def _build_multisim_payload(alpha_expressions, instrument_type, region, universe,
                            delay, decay, neutralization, truncation, test_period,
                            unit_handling, nan_handling, language, lookback,
                            visualization, pasteurization, max_trade):
    normalized_language = language.upper()
    payload = []
    for alpha_expr in alpha_expressions:
        settings = {
            'instrumentType': instrument_type,
            'region': region,
            'universe': universe,
            'delay': delay,
            'decay': decay,
            'neutralization': neutralization,
            'truncation': truncation,
            'pasteurization': pasteurization,
            'language': normalized_language,
            'visualization': visualization,
            'testPeriod': test_period,
            'maxTrade': max_trade
        }
        if normalized_language == "PYTHON":
            settings['lookback'] = 256 if lookback is None else lookback
        else:
            settings['unitHandling'] = unit_handling
            settings['nanHandling'] = nan_handling
        payload.append({'type': 'REGULAR', 'settings': settings,
                        'regular': alpha_expr})
    return payload


@mcp.tool()
async def submit_multi_simulation(
    alpha_expressions: List[str],
    instrument_type: str = "EQUITY",
    region: str = "USA",
    universe: str = "TOP3000",
    delay: int = 1,
    decay: int = 4,
    neutralization: str = "INDUSTRY",
    truncation: float = 0.0,
    test_period: str = "P0Y0M",
    unit_handling: str = "VERIFY",
    nan_handling: str = "OFF",
    language: str = "FASTEXPR",
    lookback: Optional[int] = None,
    visualization: bool = False,
    pasteurization: str = "ON",
    max_trade: str = "OFF"
) -> Dict[str, Any]:
    """Stage 1/3: submit a multisimulation and return IMMEDIATELY (seconds).

    Returns {submitted, location, multisimulation_id} on success. On HTTP 429
    returns {error: "RATE_LIMITED", retry_after}. Poll the location with
    check_multi_simulation, then call fetch_multi_simulation_result.
    Same parameters as create_multi_simulation.
    """
    try:
        if len(alpha_expressions) < 2:
            return {"error": "At least 2 alpha expressions are required"}
        if len(alpha_expressions) > 10:
            return {"error": "Maximum 10 alpha expressions allowed per request"}
        await brain_client.ensure_authenticated()
        payload = _build_multisim_payload(
            alpha_expressions, instrument_type, region, universe, delay, decay,
            neutralization, truncation, test_period, unit_handling, nan_handling,
            language, lookback, visualization, pasteurization, max_trade)
        response = await brain_client._request(
            'POST', f"{brain_client.base_url}/simulations", json=payload)
        if response.status_code == 429:
            return {
                "error": "RATE_LIMITED",
                "status_code": 429,
                "retry_after": response.headers.get("Retry-After"),
                "details": response.text[:500],
            }
        if response.status_code != 201:
            return {
                "error": f"Failed to create multisimulation. Status: {response.status_code}",
                "status_code": response.status_code,
                "details": response.text[:800],
            }
        location = response.headers.get('Location', '')
        if not location:
            return {"error": "No location header in multisimulation response"}
        return {
            "submitted": True,
            "location": location,
            "multisimulation_id": location.rstrip('/').split('/')[-1],
            "n_expressions": len(alpha_expressions),
        }
    except Exception as e:
        return {"error": f"Error submitting multisimulation: {str(e)}"}


@mcp.tool()
async def check_multi_simulation(location: str) -> Dict[str, Any]:
    """Stage 2/3: single status probe of a multisimulation (one GET, seconds).

    Returns {status, children, retry_after}. Re-call after sleeping
    ~retry_after seconds until status is COMPLETE or ERROR (children may
    appear before completion). RATE_LIMITED status means back off harder.
    """
    try:
        await brain_client.ensure_authenticated()
        response = await brain_client._request('GET', location)
        if response.status_code == 429:
            return {"status": "RATE_LIMITED",
                    "retry_after": response.headers.get("Retry-After") or 30}
        if response.status_code != 200:
            return {"status": "HTTP_ERROR",
                    "status_code": response.status_code,
                    "details": response.text[:500]}
        data = response.json()
        retry_after = response.headers.get("Retry-After")
        status = data.get("status")
        if not status:
            status = "IN_PROGRESS" if retry_after else "UNKNOWN"
        return {
            "status": status,
            "children": data.get("children", []),
            "n_children": len(data.get("children", [])),
            "retry_after": float(retry_after) if retry_after else 0.0,
        }
    except Exception as e:
        return {"status": "CLIENT_ERROR", "error": str(e)}


@mcp.tool()
async def fetch_multi_simulation_result(location: str) -> Dict[str, Any]:
    """Stage 3/3: fetch per-child results after check reports completion.

    One GET per child simulation plus one GET per produced alpha. Child
    simulations without an alpha id report their platform status explicitly
    (e.g. CANCELLED / ERROR) instead of a generic message. Output matches
    create_multi_simulation's alpha_results shape.
    """
    try:
        await brain_client.ensure_authenticated()
        response = await brain_client._request('GET', location)
        if response.status_code != 200:
            return {"error": f"multisim fetch failed: {response.status_code}",
                    "details": response.text[:400]}
        data = response.json()
        children = data.get("children", [])
        if not children:
            return {"error": "no children yet — poll check_multi_simulation until completion",
                    "status": data.get("status")}
        alpha_results = []
        for i, child_id in enumerate(children):
            child_url = child_id if str(child_id).startswith('http') \
                else f"{brain_client.base_url}/simulations/{child_id}"
            try:
                child_resp = await brain_client._request('GET', child_url)
                if child_resp.status_code != 200:
                    alpha_results.append({
                        "location": child_url,
                        "error": f"child fetch failed: {child_resp.status_code}"})
                    continue
                child = child_resp.json()
                child_status = child.get("status")
                alpha_id = child.get("alpha")
                if not alpha_id:
                    alpha_results.append({
                        "location": child_url,
                        "status": child_status,
                        "error": f"No alpha ID (child status: {child_status})"})
                    continue
                details_resp = await brain_client._request(
                    'GET', f"{brain_client.base_url}/alphas/{alpha_id}")
                if details_resp.status_code == 200:
                    alpha_results.append({"alpha_id": alpha_id,
                                          "location": child_url,
                                          "details": details_resp.json()})
                else:
                    alpha_results.append({
                        "alpha_id": alpha_id, "location": child_url,
                        "error": f"Failed to get alpha details: {details_resp.status_code}"})
            except Exception as e:
                alpha_results.append({"location": f"child_{i+1}", "error": str(e)})
        return _slim_multisim({
            "success": True,
            "message": f"fetched {len(alpha_results)} child results",
            "total_requested": len(children),
            "total_created": len(alpha_results),
            "multisimulation_id": location.rstrip('/').split('/')[-1],
            "multisim_status": data.get("status"),
            "alpha_results": alpha_results,
        })
    except Exception as e:
        return {"error": f"Error fetching multisimulation result: {str(e)}"}

# --- Payment and Financial Tools ---

@mcp.tool()
async def get_daily_and_quarterly_payment(email: str = "", password: str = "") -> Dict[str, Any]:
    """
    Get daily and quarterly payment information from WorldQuant BRAIN platform.
    
    This function retrieves both base payments (daily alpha performance payments) and 
    other payments (competition rewards, quarterly payments, referrals, etc.).
    
    Args:
        email: Your BRAIN platform email address (optional if in config)
        password: Your BRAIN platform password (optional if in config)
    
    Returns:
        Dictionary containing base payment and other payment data with summaries and detailed records
    """
    try:
        config = load_config()
        credentials = config.get("credentials", {})
        email = email or credentials.get("email")
        password = password or credentials.get("password")
        if not email or not password:
            return {"error": "Authentication credentials not provided or found in config."}
            
        await brain_client.authenticate(email, password)
        
        # Get base payments
        try:
            base_response = await brain_client._request('GET', f"{brain_client.base_url}/users/self/activities/base-payment")
            base_response.raise_for_status()
            base_payments = base_response.json()
        except:
            base_payments = "no data"
            
        try:
            # Get other payments
            other_response = await brain_client._request('GET', f"{brain_client.base_url}/users/self/activities/other-payment")
            other_response.raise_for_status()
            other_payments = other_response.json()
        except:
            other_payments = "no data"    
        return {
            "base_payments": base_payments,
            "other_payments": other_payments
        }
        
    except Exception as e:
        return {"error": f"An unexpected error occurred: {str(e)}"}

from typing import Sequence
@mcp.tool()
async def lookINTO_SimError_message(locations: Sequence[str]) -> dict:
    """
    Fetch and parse error/status from multiple simulation locations (URLs).
    Args:
        locations: List of simulation result URLs (e.g., /simulations/{id})
    Returns:
        List of dicts with location, error message, and raw response
    """
    results = []
    for loc in locations:
        try:
            resp = await brain_client._request('GET', loc)
            if resp.status_code != 200:
                results.append({
                    "location": loc,
                    "location_url": brain_client._to_absolute_url(loc),
                    "error": f"HTTP {resp.status_code}",
                    "status_code": resp.status_code,
                    "raw": brain_client._response_payload(resp)
                })
                continue
            data = resp.json() if resp.text else {}
            error_msg = brain_client._simulation_error_message(data)
            # If alpha ID is missing, include that info
            if not data.get("alpha") and error_msg == "Unknown error":
                error_msg = "Simulation did not get through; inspect raw/status for the platform response."
            extra_info = ""
            if error_msg and "does not support event inputs" in error_msg:
                extra_info = "Operator xxx does not support event inputs : If fields is vector type  should use vec_* operator with event input"
            results.append({
                "location": loc,
                "location_url": brain_client._to_absolute_url(loc),
                "error": error_msg,
                "status": data.get("status"),
                "alpha": data.get("alpha"),
                "raw": data,
                "extra_info": extra_info
            })
        except Exception as e:
            results.append({
                "location": loc,
                "error": str(e),
                "raw": None
            })
    return _slim_text_lookup({"results": results}, n=2000)

# --- Main entry point ---
if __name__ == "__main__":
    print("running the server", file=sys.stderr)
    
    # Validate critical environment setup
    config = load_config()
    creds = config.get("credentials", {})
    if not creds.get("email") or not creds.get("password"):
        print("[WARNING] No BRAIN credentials found in config. Authentication will fail until credentials are provided.", file=sys.stderr)
    
    # Verify Redis connectivity
    if brain_client.redis_client:
        print("[INFO] Redis connection established successfully", file=sys.stderr)
    else:
        print("[WARNING] Redis connection failed - caching disabled", file=sys.stderr)
    
    # Run using Streamable HTTP transport in container environment so the server remains
    # running and accessible over HTTP (not stdio which exits in non-interactive containers).
    try:
        mcp.run(transport='streamable-http')
    except TypeError:
        # Fallback if signature differs
        mcp.run('streamable-http')
