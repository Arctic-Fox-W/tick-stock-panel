"""同花顺 xiadan 窗口截图 + OCR 持仓解析。

窗口标题「网上股票交易系统5.0」为 DirectUI 自绘窗口, UI Automation 拿不到结构化
表格, 只能 PrintWindow 截图后 OCR。截图复用自选截图 OCR 的预处理与引擎
(preprocess_for_ocr + Tesseract chi_sim+eng), 与该流程已验证结果保持一致。
"""
from __future__ import annotations

import ctypes
import logging
import re
import time
from ctypes import wintypes
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from app.services.watchlist_ocr.pipeline import build_instrument_lookups
from app.services.watchlist_ocr.provider import get_ocr_provider

logger = logging.getLogger(__name__)

WINDOW_TITLE = "网上股票交易系统5.0"

# 六位代码 (证券/ETF); 名称里的 300 等短数字不会误命中
_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
# 数字 token: 容忍负号、千分位逗号、小数 (如 1,100 / -93.400 / 16.50)
_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]


def find_window(title: str = WINDOW_TITLE) -> int | None:
    """按精确标题枚举顶层窗口, 返回第一个命中的 HWND (找不到返回 None)。"""
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd: int, _lparam: int) -> bool:
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value == title:
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(_enum, 0)
    return found[0] if found else None


def _capture_window_png(hwnd: int) -> bytes:
    """PrintWindow 截取窗口内容为 PNG 字节 (24bpp BGR DIB)。"""
    user32.SetProcessDPIAware()

    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("无法获取交易窗口尺寸")
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise RuntimeError("交易窗口尺寸无效")

    hwnd_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
    if not mem_dc or not bitmap:
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        user32.ReleaseDC(hwnd, hwnd_dc)
        raise RuntimeError("无法创建内存位图")

    old = gdi32.SelectObject(mem_dc, bitmap)
    try:
        # PW_RENDERFULLCONTENT=2: 捕获 DirectUI 后台渲染内容, 避开黑屏
        user32.PrintWindow(hwnd, mem_dc, 2)

        stride = ((width * 3 + 3) // 4) * 4
        bi = _BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = width
        bi.bmiHeader.biHeight = -height  # 负值=自上而下
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 24
        bi.bmiHeader.biCompression = 0
        bi.bmiHeader.biSizeImage = stride * height

        buf = ctypes.create_string_buffer(stride * height)
        got = gdi32.GetDIBits(mem_dc, bitmap, 0, height, buf, ctypes.byref(bi), 0)
        if got == 0:
            raise RuntimeError("无法读取交易窗口像素")

        img = Image.frombytes("RGB", (width, height), buf.raw, "raw", "BGR", stride)
        if all(channel[1] == 0 for channel in img.getextrema()):
            raise RuntimeError("交易窗口截图为空白, 请确认窗口未被最小化或遮挡")

        out = BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    finally:
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)


def _ensure_tesseract_cmd() -> None:
    """pytesseract 依赖 PATH 定位 tesseract; 未加入 PATH 时显式指向标准安装路径。"""
    import shutil

    import pytesseract

    if shutil.which("tesseract"):
        return
    candidates = (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    )
    for candidate in candidates:
        if Path(candidate).exists():
            pytesseract.pytesseract.tesseract_cmd = candidate
            return


def _parse_numbers(rest: str) -> list[float]:
    """抽取子串中的数值 token (去千分位逗号), 失败项跳过。"""
    values: list[float] = []
    for token in _NUM_RE.findall(rest):
        cleaned = token.replace(",", "")
        try:
            values.append(round(float(cleaned), 4))
        except ValueError:
            continue
    return values


def _clean_name_fragment(fragment: str) -> str:
    """去掉左侧栏噪声与引号, 取代码前最后一个「词」(名称, 如 300ETF / 科创50)。"""
    fragment = fragment.strip().strip("\"'“”‘’|=-—·")
    matched = re.search(r"[A-Za-z0-9\u4e00-\u9fff.\-]+$", fragment)
    return matched.group(0) if matched else fragment


def _rationalize_price(value: float | None, reference: float | None) -> float | None:
    """OCR 偶发丢失小数点时, 依据现价恢复成本价的小数位。

    Tesseract 时而把「1.427」识别成「1427」(小数点丢失)。持仓的成本价与现价同量级,
    当成本价与现价比值远超正常范围(阈值 20 倍)时视为丢失小数点, 尝试在数字串中插入
    小数点, 取与现价最为接近的候选值。正常区间绝不干预, 避免误改真实成本。
    """
    if value is None or reference is None or not (value > 0 and reference > 0):
        return value
    if value / reference < 20.0:
        return value

    digits = f"{value:.4f}".replace(".", "").rstrip("0")
    if len(digits) <= 1:
        return value

    candidates = [value]
    for split in range(1, len(digits)):
        head = int(digits[:split])
        tail = int(digits[split:])
        candidates.append(head + tail / (10 ** len(digits[split:])))
    return round(min(candidates, key=lambda c: abs(c / reference - 1.0)), 4)


def _parse_holdings(
    text: str,
    code_to_symbol: dict[str, str],
    symbol_to_name: dict[str, str],
) -> list[dict[str, Any]]:
    """逐行解析: 命中六位代码的行视为持仓行, 代码后的数值按位次映射关键字段。"""
    holdings: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        matched = _CODE_RE.search(line)
        if not matched:
            continue

        code = matched.group(1)
        rest = line[matched.end():]
        numbers = _parse_numbers(rest)

        qty_val = numbers[0] if numbers else None
        qty = int(qty_val) if qty_val is not None and float(qty_val).is_integer() else None
        last_price = numbers[2] if len(numbers) > 2 else None
        cost_price = _rationalize_price(
            numbers[1] if len(numbers) > 1 else None, last_price
        )

        symbol = code_to_symbol.get(code)
        name = symbol_to_name.get(symbol) if symbol else None
        name_raw = _clean_name_fragment(line[: matched.start()])

        holdings.append({
            "code": code,
            "symbol": symbol,
            "name": name or name_raw or code,
            "qty": qty,
            "cost_price": cost_price,
            "last_price": last_price,
            "numbers": numbers,
        })
    return holdings


class XiadanService:
    """封装窗口查找、截图、OCR 与持仓结构化。"""

    def __init__(self, data_dir: str | Path | None) -> None:
        self._code_to_symbol: dict[str, str] = {}
        self._symbol_to_name: dict[str, str] = {}
        if data_dir:
            try:
                self._code_to_symbol, self._symbol_to_name = build_instrument_lookups(
                    Path(data_dir)
                )
            except Exception:  # noqa: BLE001 — 映射失败只降级为纯代码, 不影响持仓读取
                logger.exception("加载 instruments 映射失败, 持仓将仅返回代码")

    def snapshot(self) -> dict[str, Any]:
        """返回 {window_found, holdings, count, raw_text, ...}。"""
        hwnd = find_window()
        if hwnd is None:
            return {
                "window_found": False,
                "window_title": WINDOW_TITLE,
                "count": 0,
                "holdings": [],
                "raw_text": "",
                "error": "未找到标题为「网上股票交易系统5.0」的持仓窗口",
            }

        provider = get_ocr_provider()
        _ensure_tesseract_cmd()
        if not provider.available():
            raise RuntimeError("Tesseract OCR 未就绪, 请确认已安装 Tesseract 及 chi_sim 语言包")

        png = _capture_window_png(hwnd)
        text = provider.extract_text(png)
        holdings = _parse_holdings(text, self._code_to_symbol, self._symbol_to_name)

        return {
            "window_found": True,
            "window_title": WINDOW_TITLE,
            "count": len(holdings),
            "holdings": holdings,
            "raw_text": text,
            "captured_at": int(time.time()),
        }