"""Export a folium map to HTML and PNG (PNG via headless Chrome + selenium).

OpenStreetMap's tile servers block requests without a Referer header, which is
what browsers send for file:// pages. Maps are therefore always viewed through
a small local HTTP server (see serve_directory).
"""

from __future__ import annotations

import contextlib
import functools
import threading
import time
from collections.abc import Iterator
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import folium

from .options import PngOptions


class PngExportError(RuntimeError):
    """Raised when the PNG cannot be produced (missing selenium/Chrome, driver error...)."""


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - silence per-request logging
        pass


@contextlib.contextmanager
def serve_directory(directory: str | Path, port: int = 0, host: str = "127.0.0.1") -> Iterator[str]:
    """Serve `directory` over HTTP in a background thread; yields the base URL.

    port=0 picks a free port.
    """
    handler = functools.partial(_QuietHandler, directory=str(Path(directory).resolve()))
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


def page_url(base_url: str, html_path: str | Path) -> str:
    return base_url + quote(Path(html_path).name)


def save_html(m: folium.Map, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    m.save(str(path))
    return path


def save_png(html_path: str | Path, png_path: str | Path, opts: PngOptions | None = None) -> Path:
    opts = opts or PngOptions()
    html_path = Path(html_path).resolve()
    png_path = Path(png_path)
    try:
        from selenium import webdriver
        from selenium.common.exceptions import WebDriverException
    except ImportError as exc:
        raise PngExportError("selenium is not installed (install the [png] extra)") from exc

    options = webdriver.ChromeOptions()
    for arg in ("--headless=new", "--hide-scrollbars", "--disable-gpu", "--no-sandbox",
                f"--window-size={opts.width},{opts.height}"):
        options.add_argument(arg)

    try:
        driver = webdriver.Chrome(options=options)
    except WebDriverException as exc:
        raise PngExportError(f"could not start headless Chrome: {exc.msg or exc}") from exc

    try:
        driver.set_window_size(opts.width, opts.height)
        with serve_directory(html_path.parent) as base_url:
            driver.get(page_url(base_url, html_path))
            time.sleep(opts.wait_s)  # let map tiles load
        png_path.parent.mkdir(parents=True, exist_ok=True)
        if not driver.save_screenshot(str(png_path)):
            raise PngExportError("Chrome failed to write the screenshot")
    except WebDriverException as exc:
        raise PngExportError(f"Chrome error while rendering: {exc.msg or exc}") from exc
    finally:
        driver.quit()
    return png_path
