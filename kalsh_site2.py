import asyncio
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen, Request

from playwright.async_api import async_playwright

START_URL = "https://kalashnikovgroup.ru/"
OUT_DIR = Path(__file__).parent
ASSETS_DIR = OUT_DIR / "assets"
ASSETS_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
LANGS = "en-US,en;q=0.9,it;q=0.8,ru;q=0.7"

ALLOWED_HOSTS = {
    "kalashnikovgroup.ru",
    "www.kalashnikovgroup.ru",
    "en.kalashnikovgroup.ru",
}


def sanitize_filename(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path
    if path.endswith("/") or path == "":
        filename = "index"
    else:
        filename = Path(path).name
    # Append extension if missing for some types
    if not os.path.splitext(filename)[1]:
        # guess based on common endpoints
        if any(seg in path for seg in (".css",)):
            filename += ".css"
        elif any(seg in path for seg in (".js",)):
            filename += ".js"
        elif any(seg in path for seg in (".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif")):
            pass
    # sanitize
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    return filename or "file"

def fetch_binary(url: str, referer: str | None = None) -> bytes | None:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": LANGS,
        "Accept": "*/*",
        "Referer": referer or START_URL,
    }
    try:
        req = Request(url, headers=headers)
        with urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception:
        return None


def local_path_for_url(url: str) -> tuple[Path, str] | None:
    """Return (absolute_path, relative_path) under assets/ or None if not allowed."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    # Mirror host and full path under assets
    host = parsed.netloc
    # Optionally restrict hosts; for now allow primary domain and subdomain; permit absolute same-host or absolute path
    # If different host, still mirror under assets/external/<host>/...
    base_dir = ASSETS_DIR
    rel_parts = []
    if host and host not in ALLOWED_HOSTS:
        rel_parts.extend(["external", re.sub(r"[^A-Za-z0-9.-]", "_", host)])
    # Path handling
    path = parsed.path or "/"
    if path.endswith("/"):
        path = path + "index"
    # Remove leading slash for relative join
    if path.startswith("/"):
        path = path[1:]
    # Sanitize path to be Windows-safe (remove characters like : * ? " < > |)
    # Keep directory separators and common safe chars, replace others with underscore
    safe_path = re.sub(r"[^A-Za-z0-9._/,\-]", "_", path)
    # Normalize any accidental double slashes
    safe_path = re.sub(r"/{2,}", "/", safe_path)
    # If no extension, try preserve; JS/CSS often have .js/.css; Next chunks have .js
    rel_path = Path(*rel_parts, *safe_path.split('/'))
    abs_path = base_dir / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    # Always return POSIX-style path for use inside HTML (forward slashes)
    rel_posix = (Path("assets") / rel_path).as_posix()
    return abs_path, rel_posix


async def capture_page():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="it-IT",
            timezone_id="Europe/Rome",
            viewport={"width": 1366, "height": 900},
            bypass_csp=True,
        )
        page = await context.new_page()

        # Extra headers
        await context.set_extra_http_headers({
            "Accept-Language": LANGS,
        })

        saved_map: dict[str, str] = {}

        async def on_response(response):
            try:
                url = response.url
                # Only save successful GET-like resources
                if response.status < 200 or response.status >= 400:
                    return
                ct = (response.headers.get("content-type") or "").lower()
                # Heuristics for static assets
                if any(x in ct for x in (
                        "text/css",
                        "javascript",
                        "application/x-javascript",
                        "application/octet-stream",
                        "application/wasm",
                        "model/",
                        "application/gltf+json",
                        "application/json"
                    )) or \
                   any(url.lower().split("?")[0].endswith(ext) for ext in (
                       ".css", ".js", ".mjs", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico",
                       ".woff", ".woff2", ".ttf", ".eot", ".otf",
                       ".mp4", ".webm", ".mp3", ".ogg", ".wav",
                       ".gltf", ".glb", ".bin", ".wasm", ".ktx", ".ktx2", ".basis", ".hdr", ".env", ".dds", ".json"
                    )) or \
                   urlparse(url).path.startswith("/_next/"):
                    loc = local_path_for_url(url)
                    if not loc:
                        return
                    abs_path, rel_path = loc
                    if url in saved_map:
                        return
                    body = await response.body()
                    if body is None:
                        return
                    abs_path.write_bytes(body)
                    saved_map[url] = rel_path.replace('\\','/')
            except Exception:
                pass

        page.on("response", on_response)

        # Navigate
        resp = await page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)
        if resp is None or not resp.ok:
            # Try again following redirects fully
            await page.goto(START_URL, wait_until="load", timeout=60000)

        # Handle cookie banners if any (best-effort)
        try:
            # Common selectors, best-effort; ignore failures
            for sel in [
                'button:has-text("Accept")',
                'button:has-text("I agree")',
                'button:has-text("Consenti")',
                'button:has-text("Согласен")',
                '[data-testid="cookie-accept"]',
            ]:
                btn = await page.query_selector(sel)
                if btn:
                    await btn.click()
                    await asyncio.sleep(0.5)
                    break
        except Exception:
            pass

        # Scroll to bottom to trigger lazy-load
        last_height = await page.evaluate("() => document.body.scrollHeight")
        for _ in range(20):
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(0.8)
            new_height = await page.evaluate("() => document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height

        # Try to trigger 3D viewers lazily loaded (best-effort)
        try:
            # Click any canvas or elements with data-3d attributes to initialize loaders
            selectors = [
                "canvas",
                "[data-3d]",
                "model-viewer",
                "[class*='three']",
                "[class*='3d']",
            ]
            for sel in selectors:
                elems = await page.query_selector_all(sel)
                for e in elems[:5]:
                    try:
                        await e.scroll_into_view_if_needed()
                        await e.click(timeout=1000)
                        await asyncio.sleep(0.5)
                    except Exception:
                        pass
            # Wait a bit for network bursts
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                await asyncio.sleep(2)
        except Exception:
            pass

        # Wait for network to be mostly idle
        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass

        # Capture final HTML
        html = await page.content()
        (OUT_DIR / "index.html").write_text(html, encoding="utf-8")

        # Collect asset URLs from DOM
        # We'll evaluate in the page context to get resolved URLs
        asset_urls = await page.evaluate(
            "() => Array.from(new Set([\n"
            "  ...Array.from(document.querySelectorAll('link[rel=\\'stylesheet\\']')).map(n => n.href),\n"
            "  ...Array.from(document.querySelectorAll('script[src]')).map(n => n.src),\n"
            "  ...Array.from(document.querySelectorAll('img[src]')).map(n => n.src),\n"
            "  ...Array.from(document.querySelectorAll('source[srcset]')).flatMap(n => (n.srcset||'').split(',').map(s=>s.trim().split(' ')[0]).filter(Boolean)),\n"
            "]))")

        # Download assets (fallback for anything not caught via network listener)
        url_map: dict[str, str] = dict(saved_map)
        for u in asset_urls:
            if not u:
                continue
            # skip data: URIs
            if u.startswith("data:"):
                continue
            abs_url = urljoin(START_URL, u)
            # If already saved via listener, map and continue
            if abs_url in url_map:
                continue
            loc = local_path_for_url(abs_url)
            if not loc:
                continue
            abs_path, rel_path = loc
            if abs_path.exists():
                url_map[abs_url] = rel_path.replace('\\','/')
                continue
            data = fetch_binary(abs_url, referer=START_URL)
            if data:
                try:
                    abs_path.write_bytes(data)
                    url_map[abs_url] = rel_path.replace('\\','/')
                except Exception:
                    pass

        # Inject runtime URL rewriter for fetch/XHR to map remote assets to local mirrored paths
        rewriter_js = r"""
<script>(function(){
  function mapUrl(u){
    try{
      var a=document.createElement('a');
      a.href=u;
      if(!a.protocol || a.protocol==='file:' || u.startsWith('/') || u.startsWith('./') || u.startsWith('../')) return u;
      if(a.protocol==='http:'||a.protocol==='https:'){
        var host=a.host; var path=a.pathname||'/';
        if(path.endsWith('/')) path += 'index';
        if(path.startsWith('/')) path = path.slice(1);
        var safePath=path.replace(/[^A-Za-z0-9._/,-]/g,'_').replace(/\/{2,}/g,'/');
        var rel='assets/';
        if(!/^(.+\.)?kalashnikovgroup\.ru$/i.test(host)) rel += 'external/' + host.replace(/[^A-Za-z0-9.-]/g,'_') + '/';
        return '/' + rel + safePath;
      }
    }catch(e){return u}
    return u;
  }
  var ofetch=window.fetch;
  if(ofetch){
    window.fetch=function(input, init){
      try{
        var url = (typeof input==='string')? input : (input && input.url ? input.url : input);
        var mapped = mapUrl(url);
        if(typeof input==='string') return ofetch(mapped, init||undefined);
        try{ return ofetch(new Request(mapped, input), init||undefined);}catch(e){ return ofetch(mapped, init||undefined); }
      }catch(e){ return ofetch(input, init||undefined); }
    }
  }
  var oopen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url){
    try{ arguments[1]=mapUrl(url);}catch(e){}
    return oopen.apply(this, arguments);
  };
})();</script>
"""

        # Rewrite references in HTML
        rewritten = html
        # Include protocol-relative and path-only variants
        for src_url, local_rel in url_map.items():
            # Replace both absolute and as-appeared
            try:
                rewritten = rewritten.replace(src_url, local_rel)
                # protocol-relative //host/path
                parsed = urlparse(src_url)
                if parsed.netloc:
                    proto_rel = f"//{parsed.netloc}{parsed.path}"
                    rewritten = rewritten.replace(proto_rel, local_rel)
                # path-only
                if parsed.path:
                    rewritten = rewritten.replace(parsed.path, local_rel)
            except Exception:
                pass
        # Remove CSP meta and SRI/crossorigin attributes to avoid local blocking
        try:
            # Strip meta Content-Security-Policy
            rewritten = re.sub(r"<meta[^>]+http-equiv=\"Content-Security-Policy\"[^>]*>\s*", "", rewritten, flags=re.IGNORECASE)
            rewritten = re.sub(r"<meta[^>]+content=\"[^\"]*Content-Security-Policy[^\"]*\"[^>]*>\s*", "", rewritten, flags=re.IGNORECASE)
            # Remove integrity and crossorigin on link/script tags
            rewritten = re.sub(r"\s+integrity=\"[^\"]*\"", "", rewritten, flags=re.IGNORECASE)
            rewritten = re.sub(r"\s+crossorigin=\"[^\"]*\"", "", rewritten, flags=re.IGNORECASE)
        except Exception:
            pass
        # Prepend/Inject the rewriter before </head> or at top of body
        if "</head>" in rewritten:
            rewritten = rewritten.replace("</head>", rewriter_js + "</head>")
        else:
            rewritten = rewriter_js + rewritten
        (OUT_DIR / "index.html").write_text(rewritten, encoding="utf-8")

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(capture_page())
