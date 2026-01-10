# Guide: Setting Up Playwright with a Local Chrome Executable

This guide explains how to configure Playwright to use a locally extracted Google Chrome browser. This is useful in environments where you don't have `sudo` permissions to install Chrome or its dependencies system-wide.

## The Problem

When running a Playwright script, you might encounter an error like this:

```
playwright._impl._errors.Error: BrowserType.launch: Executable doesn't exist at ...
```

This means Playwright cannot find the Chrome browser executable in its default search paths.

## The Solution: Local Extraction

The solution is to download the Chrome `.deb` package, extract the executable, and then point your Playwright script to it.

### Step 0: pip install playwright

Before you begin, make sure you have Playwright installed. You can install it using pip:

```bash
pip install playwright
```

### Step 1: Download the Chrome `.deb` Package

You can download the latest stable version of Google Chrome for Debian/Ubuntu-based systems using `wget`:

```bash
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
```

### Step 2: Extract the Executable

Once you have the `.deb` file, you can extract its contents without installing it.

1.  **Extract the `.deb` archive:** The `ar` command extracts the contents of the `.deb` file.

    ```bash
    ar x google-chrome-stable_current_amd64.deb
    ```

    This will create several files, including `data.tar.xz`.

2.  **Extract the data archive:** The `data.tar.xz` file contains the actual program files.

    ```bash
    tar -xf data.tar.xz
    ```

    This will create an `opt` directory in your current location.

### Step 3: Locate the Chrome Executable

The Chrome executable is now located at `./opt/google/chrome/chrome`.

### Step 4: Configure Your Playwright Script

In your Python script forum_functions.py, you need to tell Playwright where to find the Chrome executable by using the `executable_path` argument when launching the browser.

It is best practice to use an **absolute path** to avoid issues with the script's working directory. You can construct the absolute path by combining your project's root directory with the relative path to the executable.

Here is an example:

```python
import os
from playwright.async_api import async_playwright
import asyncio

async def main():
    # Get the absolute path to the executable
    project_dir = os.getcwd() # Or specify your project's absolute path
    executable_path = os.path.join(project_dir, 'opt/google/chrome/chrome')

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=executable_path,
            args=['--headless=new', '--no-sandbox']
        )
        page = await browser.new_page()
        await page.goto('http://playwright.dev')
        print(await page.title())
        await browser.close()

if __name__ == '__main__':
    asyncio.run(main())
```

By providing the absolute `executable_path`, you are explicitly telling Playwright where to find the browser, making your script more robust.

## Alternative: `playwright install`

If you have `sudo` permissions, the recommended way to install the browsers for Playwright is to use the following command:

```bash
playwright install --with-deps
```

This will download the browser and also install all the necessary system dependencies.