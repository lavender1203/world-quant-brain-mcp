#!/usr/bin/env python3
"""
浏览器设置和下载模块
用于自动下载和配置Chrome浏览器以支持Playwright
"""

import os
import sys
import subprocess
import platform
import zipfile
import requests
from pathlib import Path
import shutil

def log(message: str, level: str = "INFO"):
    """日志输出函数"""
    print(f"[{level}] {message}", file=sys.stderr)

def get_system_info():
    """获取系统信息"""
    system = platform.system()
    machine = platform.machine().lower()
    return system, machine

def download_chrome_package():
    """下载Chrome .deb包并提取可执行文件"""
    system, machine = get_system_info()
    
    if system == "Linux" and machine in ["x86_64", "amd64"]:
        # Linux系统下载.deb包
        log("检测到Linux x86_64系统，下载Chrome .deb包...", "INFO")
        
        # 创建下载目录
        download_dir = Path("./downloads")
        download_dir.mkdir(exist_ok=True)
        
        deb_file = download_dir / "google-chrome-stable_current_amd64.deb"
        
        if not deb_file.exists():
            log("下载Chrome .deb包...", "INFO")
            try:
                url = "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"
                response = requests.get(url, stream=True)
                response.raise_for_status()
                
                with open(deb_file, 'wb') as f:
                    shutil.copyfileobj(response.raw, f)
                log("Chrome .deb包下载完成", "SUCCESS")
            except Exception as e:
                log(f"下载失败: {e}", "ERROR")
                return None
        else:
            log("Chrome .deb包已存在", "INFO")
        
        # 提取.deb包
        log("提取Chrome .deb包...", "INFO")
        try:
            # 使用ar命令提取
            subprocess.run(["ar", "x", str(deb_file)], cwd=download_dir, check=True)
            
            # 提取data.tar.xz
            data_file = download_dir / "data.tar.xz"
            if data_file.exists():
                subprocess.run(["tar", "-xf", str(data_file)], cwd=download_dir, check=True)
                log("Chrome .deb包提取完成", "SUCCESS")
            
            # 检查Chrome可执行文件
            chrome_path = download_dir / "opt" / "google" / "chrome" / "chrome"
            if chrome_path.exists():
                return str(chrome_path.absolute())
            else:
                log("未找到Chrome可执行文件", "ERROR")
                return None
                
        except Exception as e:
            log(f"提取失败: {e}", "ERROR")
            return None
    
    elif system == "Windows":
        # Windows系统，尝试使用系统Chrome或安装
        log("检测到Windows系统，检查系统Chrome浏览器...", "INFO")
        
        # 常见Chrome安装路径
        chrome_paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")
        ]
        
        for path in chrome_paths:
            if os.path.exists(path):
                log(f"找到Chrome浏览器: {path}", "SUCCESS")
                return path
        
        log("未找到Chrome浏览器，请手动安装或从以下地址下载:", "WARNING")
        log("https://www.google.com/chrome/", "INFO")
        return None
    
    else:
        log(f"不支持的系统: {system} {machine}", "ERROR")
        return None

def setup_chrome_for_playwright():
    """为Playwright设置Chrome浏览器"""
    log("开始设置Chrome浏览器用于Playwright...", "INFO")
    
    # try:
    #     # 尝试安装Playwright浏览器
    #     log("尝试安装Playwright Chromium浏览器...", "INFO")
    #     result = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
    #                           capture_output=True, text=True)
    #     if result.returncode == 0:
    #         log("Playwright Chromium浏览器安装成功", "SUCCESS")
    #         return None  # 使用默认的Playwright浏览器
        
    # except Exception as e:
    #     log(f"Playwright浏览器安装失败: {e}", "WARNING")
    
    # 如果Playwright浏览器安装失败，尝试设置本地Chrome
    chrome_path = download_chrome_package()
    
    if chrome_path:
        log(f"Chrome浏览器路径: {chrome_path}", "SUCCESS")
        return chrome_path
    else:
        log("无法设置Chrome浏览器，将使用默认Playwright浏览器", "WARNING")
        return None

def get_playwright_chrome_path():
    """获取Playwright的Chrome路径"""
    try:
        # 使用异步API避免导入问题
        import asyncio
        from playwright.async_api import async_playwright
        
        async def get_path():
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                executable_path = browser.executable_path
                await browser.close()
                return executable_path
        
        return asyncio.run(get_path())
    except Exception as e:
        log(f"获取Playwright Chrome路径失败: {e}", "ERROR")
        return None

# 主要设置函数
def ensure_browser_available():
    """确保浏览器可用，返回要使用的浏览器路径"""
    log("检查浏览器可用性...", "INFO")
    
    # # 首先尝试获取Playwright的默认Chrome
    # playwright_path = get_playwright_chrome_path()
    # if playwright_path and os.path.exists(playwright_path):
    #     log(f"Playwright Chrome可用: {playwright_path}", "SUCCESS")
    #     return playwright_path
    
    # 如果Playwright Chrome不可用，尝试设置本地Chrome
    local_chrome_path = setup_chrome_for_playwright()
    if local_chrome_path and os.path.exists(local_chrome_path):
        log(f"本地Chrome可用: {local_chrome_path}", "SUCCESS")
        return local_chrome_path
    
    # 如果都不可用，返回None，让调用者使用默认设置
    log("无可用Chrome浏览器，将使用默认Playwright设置", "WARNING")
    return None

if __name__ == "__main__":
    # 测试函数
    browser_path = ensure_browser_available()
    if browser_path:
        print(f"可用浏览器路径: {browser_path}")
    else:
        print("无法确定浏览器路径")