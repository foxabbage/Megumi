import asyncio
import os
from playwright.async_api import async_playwright, Page, Browser, BrowserContext
import base64
import io
import json
from typing import Any, Dict, List
from PIL import Image, ImageDraw, ImageFont
import random
import stat
from pathlib import Path

# 一些结构性/装饰性标签，通常没有直接交互
STRUCTURAL_TAGS = {
    "html", "head", "meta", "link", "style", "script", "base",
    "title", "body",
    "header", "footer", "main", "nav", "section", "article",
    "aside", "summary", "details",
}

# 明显非交互的 ARIA role
NON_INTERACTIVE_ROLES = {
    "presentation", "none",
    "img", "banner", "main", "contentinfo",
    "navigation", "region",
}

def looks_interactive(el: Dict[str, Any]) -> bool:
    tag = (el.get("tag") or "").lower()
    role = (el.get("role") or "").lower()
    typ = (el.get("type") or "").lower()
    text = (el.get("text") or "").strip()
    cls = (el.get("cls") or "").lower()
    id_ = (el.get("id") or "").lower()
    aria_label = (el.get("ariaLabel") or "").strip()

    # 原生可交互控件
    if tag in {"a", "button", "select", "textarea"}:
        return True
    if tag == "input" and typ not in {"hidden"}:
        return True

    # 有典型交互 role
    if role in {"button", "link", "tab", "menuitem", "option", "switch", "checkbox", "radio", "textbox"}:
        return True

    # 有常见事件或可获得焦点
    if any(x in el for x in ("onclick", "onmousedown", "onmouseup")):
        return True

    # class / id 里有明显交互关键词
    if any(k in cls or k in id_ for k in ("btn", "button", "link", "click", "nav", "tab", "menu")):
        return True

    # 有文本/aria-label 且是小块区域
    if (text or aria_label) and tag in {"div", "span", "li"}:
        return True

    return False

def is_obviously_non_interactive(el: Dict[str, Any]) -> bool:
    tag = (el.get("tag") or "").lower()
    role = (el.get("role") or "").lower()
    typ = (el.get("type") or "").lower()
    bbox = el.get("bbox") or {}
    w = bbox.get("width") or 0
    h = bbox.get("height") or 0

    if tag in STRUCTURAL_TAGS:
        if not (tag == "body" and (el.get("isContentEditable") or "ke-content" in (el.get("cls") or ""))):
            return True

    if w * h < 9:
        return True
    if tag in STRUCTURAL_TAGS or tag in {"video", "audio", "source"}:
        return True
    if tag == "input" and typ == "hidden":
        return True
    if role in NON_INTERACTIVE_ROLES:
        return True
    if not looks_interactive(el):
        return True

    return False

JS_COLLECT_ALL_RECURSIVE = r"""(args) => {
  const selector = args[0];
  const maxDepth = args[1];
  const maxPerDoc = args[2];
  const minArea = args[3];
  const includeIframes = args[4];

  function oneLine(s){ return String(s||"").replace(/\\s+/g," ").trim(); }

  function describe(el){
    const tagName = (el.tagName || "").toLowerCase();
    const hrefAttr = el.getAttribute("href") || "";
    const typeAttr = (el.getAttribute("type") || "").toLowerCase();
    const roleAttr = (el.getAttribute("role") || "").toLowerCase();
    const ariaAttr = el.getAttribute("aria-label") || "";

    let clickable = true;
    if (tagName == "div") clickable = false;

    if (tagName === "a" && hrefAttr) clickable = true;
    if (tagName === "button") clickable = true;
    if (tagName === "input" && typeAttr !== "hidden") clickable = true;

    if (["button","link","tab","menuitem","option","switch","checkbox","radio"].includes(roleAttr)) {
        clickable = true;
    }

    const isContentEditable = el.isContentEditable || el.getAttribute('contenteditable') === 'true';
    if (isContentEditable) clickable = true;

    if (el.onclick === "function") clickable = true;
    try {
        if (!clickable && typeof el.onclick === "function") {
            clickable = true;
        }
    } catch(e) {}

    const cls = (el.getAttribute("class") || "").toLowerCase();
    if (cls.includes("ke-content") || cls.includes("ke-edit-textarea")) {
        clickable = true;
    }

    return {
        tag: tagName,
        id: oneLine(el.getAttribute("id")),
        cls: oneLine(el.getAttribute("class")),
        role: roleAttr,
        name: oneLine(el.getAttribute("name")),
        type: typeAttr,
        href: oneLine(hrefAttr),
        src: oneLine(el.getAttribute("src")),
        ariaLabel: oneLine(ariaAttr),
        text: oneLine(el.textContent).slice(0, 40),
        clickable: clickable,
        isContentEditable: el.isContentEditable || el.getAttribute('contenteditable') === 'true',
    };
  }

  const out = [];
  let counter = 0;

  function walk(doc, depth, ox, oy, path){
    if (!doc || depth > maxDepth) return;
    const win = doc.defaultView;
    if (!win) return;

    const sx = 0;
    const sy = 0;
    const dpr = win.devicePixelRatio || 1;

    const nodes = Array.from(doc.querySelectorAll(selector)).slice(0, maxPerDoc);
    for (const el of nodes){
      try {
        const r = el.getBoundingClientRect();
        if (!r) continue;
        const area = r.width * r.height;
        if (area < minArea) continue;

        const vw = win.innerWidth || doc.documentElement.clientWidth || 0;
        const vh = win.innerHeight || doc.documentElement.clientHeight || 0;
        if (r.right <= 0 || r.bottom <= 0 || r.left >= vw || r.top >= vh) {
          continue;
        }

        const desc = describe(el);
        if (!desc.clickable) continue;

        if (!el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) continue;

        out.push({
          id: `e_${counter++}`,
          path,
          depth,
          dpr,
          ...describe(el),
          bbox: {
            x: (ox + r.x - sx) * dpr,
            y: (oy + r.y - sy) * dpr,
            width: r.width * dpr,
            height: r.height * dpr
          }
        });
      } catch(e) {}
    }

    if (!includeIframes) return;

    const iframes = Array.from(doc.querySelectorAll("iframe")).slice(0, maxPerDoc);
    for (let i = 0; i < iframes.length; i++){
      const fr = iframes[i];
      try {
        const rr = fr.getBoundingClientRect();
        const iframeOx = ox + rr.x - sx;
        const iframeOy = oy + rr.y - sy;
        const nextPath = path + `/iframe[${i}]`;

        try {
          const childDoc = fr.contentDocument;
          if (!childDoc) {
            out.push({
              id: `iframe_${counter++}`,
              path: nextPath,
              depth,
              dpr,
              tag: "iframe",
              src: oneLine(fr.getAttribute("src")),
              error: "iframe not ready (contentDocument is null)",
              bbox: {x: iframeOx * dpr, y: iframeOy * dpr, width: rr.width * dpr, height: rr.height * dpr},
            });
            continue;
          }
          walk(childDoc, depth + 1, iframeOx, iframeOy, nextPath);
        } catch(e) {
          out.push({
            id: `iframe_${counter++}`,
            path: nextPath,
            depth,
            dpr,
            tag: "iframe",
            src: oneLine(fr.getAttribute("src")),
            error: String(e),
            bbox: {x: iframeOx * dpr, y: iframeOy * dpr, width: rr.width * dpr, height: rr.height * dpr},
          });
        }
      } catch(e) {}
    }
  }

  walk(document, 0, 0, 0, "root");
  return JSON.stringify(out);
}"""

def mark_containing_items_for_removal(items):
    """标记需要移除的包含项"""
    def bbox_contains(a, b):
        ax1, ay1 = a["x"], a["y"]
        ax2, ay2 = ax1 + a["width"], ay1 + a["height"]
        bx1, by1 = b["x"], b["y"]
        bx2, by2 = bx1 + b["width"], by1 + b["height"]
        return (bx1 >= ax1 and by1 >= ay1 and bx2 <= ax2 and by2 <= ay2)

    for item in items:
        item["to_remove"] = False

    n = len(items)
    for i in range(n):
        a = items[i]
        for j in range(n):
            if i == j:
                continue
            b = items[j]
            if (a.get("text") or "").strip() == (b.get("text") or "").strip():
                if bbox_contains(a["bbox"], b["bbox"]):
                    a["to_remove"] = True
                    break

    return [item for item in items if not item["to_remove"]]

def draw_dashed_line(draw, xy, dash_len=6, gap_len=4, fill=(255, 0, 0, 200), width=2):
    """画虚线"""
    x1, y1, x2, y2 = xy
    if y1 == y2:  # 水平线
        total_len = abs(x2 - x1)
        step = dash_len + gap_len
        n = max(1, int(total_len // step) + 1)
        direction = 1 if x2 >= x1 else -1
        for i in range(n):
            start = x1 + direction * i * step
            end = start + direction * dash_len
            if direction == 1:
                if start > x2: break
                end = min(end, x2)
            else:
                if start < x2: break
                end = max(end, x2)
            draw.line((start, y1, end, y2), fill=fill, width=width)
    elif x1 == x2:  # 垂直线
        total_len = abs(y2 - y1)
        step = dash_len + gap_len
        n = max(1, int(total_len // step) + 1)
        direction = 1 if y2 >= y1 else -1
        for i in range(n):
            start = y1 + direction * i * step
            end = start + direction * dash_len
            if direction == 1:
                if start > y2: break
                end = min(end, y2)
            else:
                if start < y2: break
                end = max(end, y2)
            draw.line((x1, start, x2, end), fill=fill, width=width)
    else:
        draw.line((x1, y1, x2, y2), fill=fill, width=width)

def draw_dashed_rect(draw, x1, y1, x2, y2, dash_len=6, gap_len=4, fill=(255, 0, 0, 200), width=2):
    """画虚线矩形"""
    draw_dashed_line(draw, (x1, y1, x2, y1), dash_len, gap_len, fill, width)
    draw_dashed_line(draw, (x1, y2, x2, y2), dash_len, gap_len, fill, width)
    draw_dashed_line(draw, (x1, y1, x1, y2), dash_len, gap_len, fill, width)
    draw_dashed_line(draw, (x2, y1, x2, y2), dash_len, gap_len, fill, width)

def screenshot_to_png_bytes(s: Any) -> bytes:
    if isinstance(s, (bytes, bytearray)):
        return bytes(s)
    if isinstance(s, str):
        ss = s.strip()
        if ss.startswith("data:image"):
            ss = ss.split(",", 1)[-1].strip()
        try:
            return base64.b64decode(ss, validate=True)
        except Exception:
            with open(s, "rb") as f:
                return f.read()
    raise TypeError(f"Unsupported screenshot type: {type(s)}")

def items_to_text(items_raw):
    format_ele_text = []
    for web_ele_id in range(len(items_raw)):
        item = items_raw[web_ele_id]
        is_menu = item.get('isMenu', False)
        menu_options = item.get('menuOptions', [])
        label_text = item.get('text', "")
        ele_tag_name = item.get("tag", "button")
        ele_type = item.get("type", "")
        ele_aria_label = item.get("ariaLabel", "")
        input_attr_types = ['text', 'search', 'password', 'email', 'tel']

        if is_menu and menu_options:
            trigger_text = label_text.split('\\n')[0].strip()
            options_str = ', '.join([f'"{opt}"' for opt in menu_options])
            base_text = f"[{web_ele_id}]: <{ele_tag_name}>"
            if trigger_text:
                base_text += f' "{trigger_text}"'
            elif ele_aria_label:
                base_text += f' "{ele_aria_label}"'
            format_ele_text.append(f"{base_text} is a menu with options: [{options_str}];")
            continue

        if not label_text:
            if (ele_tag_name.lower() == 'input' and ele_type in input_attr_types) or \
               ele_tag_name.lower() == 'textarea' or \
               (ele_tag_name.lower() == 'button' and ele_type in ['submit', 'button']):
                if ele_aria_label:
                    format_ele_text.append(f'[{web_ele_id}]: <{ele_tag_name}> "{ele_aria_label}";')
                else:
                    format_ele_text.append(f'[{web_ele_id}]: <{ele_tag_name}> "{label_text}";')
        elif label_text and len(label_text) < 200:
            if not ("<img" in label_text and "src=" in label_text):
                if ele_tag_name in ["button", "input", "textarea"]:
                    if ele_aria_label and (ele_aria_label != label_text):
                        format_ele_text.append(f'[{web_ele_id}]: <{ele_tag_name}> "{label_text}", "{ele_aria_label}";')
                    else:
                        format_ele_text.append(f'[{web_ele_id}]: <{ele_tag_name}> "{label_text}";')
                else:
                    if ele_aria_label and (ele_aria_label != label_text):
                        format_ele_text.append(f'[{web_ele_id}]: "{label_text}", "{ele_aria_label}";')
                    else:
                        format_ele_text.append(f'[{web_ele_id}]: "{label_text}";')

    return '\\t'.join(format_ele_text)

def draw_som(items, overlay, max_draw):
    try:
        font = ImageFont.truetype(ImageFont.load_default().path, size=20)
    except Exception:
        font = ImageFont.load_default()

    placed_label_boxes = []
    draw = ImageDraw.Draw(overlay)
    for idx, it in enumerate(items[:max_draw]):
        b = it.get("bbox") or {}
        x, y, w, h = b.get("x"), b.get("y"), b.get("width"), b.get("height")
        if None in (x, y, w, h) or w <= 0 or h <= 0:
            continue

        r, g, b_color = random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)
        color = (r, g, b_color, 255)

        x1, y1, x2, y2 = x, y, x + w, y + h
        draw_dashed_rect(draw, x1, y1, x2, y2, dash_len=6, gap_len=4, fill=color, width=2)

        if idx < max_draw:
            label = f'{idx}'
            try:
                tb = draw.textbbox((0, 0), label, font=font)
                tw, th = tb[2] - tb[0], tb[3] - tb[1]
            except Exception:
                tw, th = len(label) * 6, 12

            padding_x, padding_y = 6, 4
            label_w, label_h = tw + padding_x * 2, th + padding_y * 2

            candidates = [
                ("top_left", x1, y1 - label_h - 2),
                ("top_right", x2 - label_w, y1 - label_h - 2),
                ("bottom_left", x1, y2 + 2),
                ("bottom_right", x2 - label_w, y2 + 2),
            ]

            img_w, img_h = overlay.size
            normalized_candidates = [
                (name, max(0, min(lx, img_w - label_w)), max(0, min(ly, img_h - label_h)))
                for name, lx, ly in candidates
            ]

            best_pos = None
            best_overlap = None
            for name, lx_c, ly_c in normalized_candidates:
                candidate_rect = (lx_c, ly_c, lx_c + label_w, ly_c + label_h)
                total_overlap = sum(
                    rect_intersection_area(candidate_rect, placed)
                    for placed in placed_label_boxes
                )
                if total_overlap == 0:
                    best_pos = (lx_c, ly_c)
                    best_overlap = 0
                    break
                if best_overlap is None or total_overlap < best_overlap:
                    best_overlap = total_overlap
                    best_pos = (lx_c, ly_c)

            lx, ly = best_pos
            label_box = (lx, ly, lx + label_w, ly + label_h)
            placed_label_boxes.append(label_box)

            color = list(color)
            color[-1] = 150
            draw.rectangle([lx, ly, lx + label_w, ly + label_h], fill=tuple(color))
            draw.text((lx + padding_x, ly + padding_y), label, fill=(255, 255, 255, 255), font=font)

def rect_intersection_area(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0
    return (ix2 - ix1) * (iy2 - iy1)

def is_inside_strict(box_inner: dict, box_outer: dict) -> bool:
    x1, y1, w1, h1 = box_inner.get("x"), box_inner.get("y"), box_inner.get("width"), box_inner.get("height")
    x2, y2, w2, h2 = box_outer.get("x"), box_outer.get("y"), box_outer.get("width"), box_outer.get("height")
    i_left, i_top = x1, y1
    i_right, i_bottom = x1 + w1, y1 + h1
    o_left, o_top = x2, y2
    o_right, o_bottom = x2 + w2, y2 + h2
    return (i_left > o_left and i_top > o_top and i_right < o_right and i_bottom < o_bottom)

def remove_outer_boxes(A: list) -> list:
    n = len(A)
    remove_flags = [False] * n
    for i in range(n):
        if remove_flags[i]:
            continue
        box_i = A[i]["bbox"]
        for j in range(n):
            if i == j:
                continue
            box_j = A[j]["bbox"]
            if is_inside_strict(box_j, box_i):
                remove_flags[i] = True
                break
    return [box for k, box in enumerate(A) if not remove_flags[k]]

async def get_css_som(page, selector="*", max_depth=16, max_per_doc=3000, min_area=-1.0, max_retry=3):
    items = []
    for _ in range(max_retry):
        try:
            items_json = await page.evaluate(
                JS_COLLECT_ALL_RECURSIVE,
                [selector, max_depth, max_per_doc, float(min_area), True],
            )
            items = json.loads(items_json)
            break
        except Exception:
            try:
                await page.wait_for_load_state()
            except Exception:
                pass
            await asyncio.sleep(1)
            continue

    if items:
        items = mark_containing_items_for_removal(items)
    return items

def remove_neg_boxes(A: list) -> list:
    n = len(A)
    remove_flags = [False] * n
    for i in range(n):
        box_i = A[i]["bbox"]
        x, y, w, h = box_i.get("x"), box_i.get("y"), box_i.get("width"), box_i.get("height")
        if None in (x, y, w, h) or w <= 0 or h <= 0:
            remove_flags[i] = True
        if box_i.get("x", 0) < 0 or box_i.get("y", 0) < 0:
            remove_flags[i] = True
    return [box for k, box in enumerate(A) if not remove_flags[k]]

async def get_som(page: Page, img_path, img_path_no_box, selector="*", max_depth=16, ratio=0.3, 
                  max_per_doc=3000, min_area=-1.0, max_draw=2000, max_retry=3):
    """获取页面 SoM 标注"""
    items = await get_css_som(page, selector=selector, max_depth=max_depth, 
                               max_per_doc=max_per_doc, min_area=min_area, max_retry=max_retry)
    
    shot = await page.screenshot()
    with open(img_path_no_box, "wb") as f:
        f.write(shot)
    
    items = remove_neg_boxes(items)
    
    png_bytes = screenshot_to_png_bytes(shot)
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw_som(items, overlay, max_draw)
    img = Image.alpha_composite(img, overlay)
    img.save(img_path)
    
    return items, items_to_text(items)

PLAYWRIGHT_KEY_MAP = {
    "backspace": "Backspace", "tab": "Tab", "return": "Enter", "enter": "Enter",
    "shift": "Shift", "control": "ControlOrMeta", "alt": "Alt", "escape": "Escape",
    "space": "Space", "pageup": "PageUp", "pagedown": "PageDown", "end": "End",
    "home": "Home", "left": "ArrowLeft", "up": "ArrowUp", "right": "ArrowRight",
    "down": "ArrowDown", "insert": "Insert", "delete": "Delete", "semicolon": ";",
    "equals": "=", "multiply": "Multiply", "add": "Add", "separator": "Separator",
    "subtract": "Subtract", "decimal": "Decimal", "divide": "Divide",
    "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4", "f5": "F5", "f6": "F6",
    "f7": "F7", "f8": "F8", "f9": "F9", "f10": "F10", "f11": "F11", "f12": "F12",
    "command": "Meta",
}

class PlaywrightComputer:
    """Async Playwright wrapper for web agent"""

    def __init__(self, task_dir:str, initial_url="https://google.com/", highlight_mouse=False):
        self._initial_url = initial_url
        self.task_dir = task_dir
        self._highlight_mouse = highlight_mouse
        
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._storage_state_path = "tokens/storage_state.json"
        self._is_initialized = False

    async def _handle_new_page(self, new_page: Page):
        """Only keep one tab: redirect new tab url into current page"""
        new_url = new_page.url
        await new_page.close()
        try:
            await self._page.goto(new_url)
        except Exception as e:
            if "interrupted by another navigation" in str(e):
                pass
            else:
                raise

    def _clear_cache(self, path: str):
        dir_path = Path(path)

        for item in dir_path.iterdir():
            # 仅处理文件，忽略文件夹
            if item.is_file():
                os.chmod(item, stat.S_IWRITE)
                item.unlink()

    async def reset(self):
        self._clear_cache(f"{self.task_dir}/trajectory_som")
        self._clear_cache(f"{self.task_dir}/trajectory")
        await self.close()
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
            headless=False,
        )

        storage_state = self._storage_state_path

        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        )
        self._context = await self._browser.new_context(
            no_viewport=True,
            user_agent=user_agent,
            timezone_id="Asia/Shanghai",
            storage_state=storage_state,
        )

        self._page = await self._context.new_page()
        await self._page.goto(self._initial_url, timeout=60000, wait_until="domcontentloaded")
        self._context.on("page", lambda p: asyncio.create_task(self._handle_new_page(p)))
        print("Started local playwright (async).")
        self._is_initialized = True
        return self
    
    async def focus(self, reset_page: bool = False):
        """
        Focus on existing browser session or open new one if not available.
        
        Args:
            reset_page: If True, navigate to initial_url even when reusing session
        """
        if self._is_initialized and self._browser.is_connected():
            print("Reusing existing browser session.")

            if reset_page and self._page:
                await self._page.goto(self._initial_url, timeout=60000, wait_until="domcontentloaded")
            
            try:
                import pygetwindow as gw
                windows = [w for w in gw.getWindowsWithTitle("Chrome") if w.isVisible]
                if windows:
                    windows[0].activate()
            except (ImportError, Exception):
                pass
                
            return self
        
        print("Existing session unavailable, opening new browser...")
        return await self.reset()

    async def close(self):
        self._is_initialized = False
        if self._context:
            try:
                await self._context.storage_state(path=self._storage_state_path)
            except Exception as e:
                print(f"Failed to save storage state: {e}")

        if self._context:
            await self._context.close()
        try:
            if self._browser:
                await self._browser.close()
        except Exception as e:
            if "Browser.close: Connection closed while reading from the driver" in str(e):
                pass
            else:
                raise
        if self._playwright:
            await self._playwright.stop()

    async def click_at(self, x: int, y: int):
        await self._page.mouse.move(x, y)
        await self._page.mouse.down()
        await self._page.wait_for_timeout(100)
        await self._page.mouse.up()
        await self._page.wait_for_load_state()

    async def type_text_at(self, x: int, y: int, text: str, press_enter=True, clear_before_typing=True):
        await self.click_at(x, y)
        await asyncio.sleep(0.1)
        await self._page.wait_for_load_state()

        if clear_before_typing:
            await self.key_combination(["Control", "A"])
            await asyncio.sleep(0.1)
            await self.key_combination(["Backspace"])
            await self._page.wait_for_load_state()

        await self.click_at(x, y)
        await asyncio.sleep(0.1)
        await self._page.keyboard.type(text)
        await self._page.wait_for_load_state()

        if press_enter:
            await self.key_combination(["Enter"])
        await self._page.wait_for_load_state()

    async def scroll_at(self, x: int, y: int, direction: str, magnitude: int = 400):
        await self._page.mouse.move(x, y)
        await asyncio.sleep(0.1)

        dx = dy = 0
        if direction == "up":
            dy = -magnitude
        elif direction == "down":
            dy = magnitude
        elif direction == "left":
            dx = -magnitude
        elif direction == "right":
            dx = magnitude
        else:
            raise ValueError("Unsupported direction: ", direction)

        await self._page.mouse.wheel(dx, dy)
        await self._page.wait_for_load_state()

    async def go_back(self):
        await self._page.go_back()
        await self._page.wait_for_load_state()

    async def navigate(self, url: str, normalize=True):
        normalized_url = url
        if normalize and not normalized_url.startswith(("http://", "https://")):
            normalized_url = "https://" + normalized_url
        await self._page.goto(normalized_url)
        await self._page.wait_for_load_state()

    async def key_combination(self, keys: list[str]):
        keys = [PLAYWRIGHT_KEY_MAP.get(k.lower(), k) for k in keys]
        for key in keys[:-1]:
            await self._page.keyboard.down(key)
        await self._page.keyboard.press(keys[-1])
        for key in reversed(keys[:-1]):
            await self._page.keyboard.up(key)

    async def current_state(self, it, ratio=0.4):
        try:
            await self._page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            try:
                await self._page.wait_for_load_state("load", timeout=5000)
            except:
                pass

        await asyncio.sleep(0.1)

        img_path = f"{self.task_dir}/trajectory_som/screenshot{it}.png"
        img_path_no_box = f"{self.task_dir}/trajectory/screenshot{it}.png"

        SoM_list, format_ele_text = await get_som(self._page, img_path, img_path_no_box)
        img = Image.open(img_path)
        width, height = img.size
        img = img.resize((int(width*ratio), int(height*ratio)))
        img.save(img_path)
        return {
            "img_path": img_path,
            "img_path_no_box": img_path_no_box,
            "SoM": {
                "SoM_list": SoM_list,
                "format_ele_text": format_ele_text
            },
            "current_url": self._page.url,
            "width": width,
            "height": height
        }

    async def _select(self, x, y, text):
        await self._page.mouse.click(x, y)
        target_text = text
        handle = await self._page.evaluate_handle("""
        ([x,y]) => document.elementFromPoint(x,y)
        """, [x, y])
        
        tag = await handle.evaluate("el => el && el.tagName")
        if tag == "SELECT":
            await handle.evaluate("""(sel, label) => {
                const opt = [...sel.options].find(o => o.label.trim() === label.trim());
                if (!opt) throw new Error("找不到选项: " + label);
                sel.value = opt.value;
                sel.dispatchEvent(new Event('input', {bubbles:true}));
                sel.dispatchEvent(new Event('change', {bubbles:true}));
            }""", target_text)
        else:
            raise RuntimeError(f"坐标处元素不是 SELECT，而是 {tag}")