# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownParameterType=false
"""Translation and RTL helpers extracted from the monolith."""
from __future__ import annotations

import re
import time
import asyncio
from typing import Any, Dict, List, Match, Optional, Tuple, cast

import aiohttp

from bot_core.config import get_env
from bot_core.telemetry import atimed

try:  # optional dependency
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover
    BeautifulSoup = None  # type: ignore

ARABIC_INDIC = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
ARABIC_INDIC_DIGITS = False  # forced off per user request
KURDISH_LANGS = {"ku", "ckb"}
RTL_LANGS = {"ar", "ku", "ckb"}
# Budget targets (seconds) — tightened to fail fast and fall back quicker for non-English
TRANSLATE_TOTAL_TIMEOUT = float(get_env().translator_defaults.get("TRANSLATE_TOTAL_TIMEOUT", "6") or 6)
PROVIDER_TIMEOUT = float(get_env().translator_defaults.get("TRANSLATE_PROVIDER_TIMEOUT", "1.5") or 1.5)
FREE_GOOGLE_TIMEOUT = float(get_env().translator_defaults.get("TRANSLATE_FREE_GOOGLE_TIMEOUT", "2") or 2)
MAX_CONCURRENCY = int(get_env().translator_defaults.get("TRANSLATE_CONCURRENCY", "10") or 10)

# Simple TTL cache to avoid retranslating common fragments
_BATCH_CACHE: Dict[Tuple[str, str], Tuple[float, str]] = {}
_CACHE_TTL = 60 * 60  # 60 minutes
_HTTP_SESSION: Optional[aiohttp.ClientSession] = None
_HTTP_SESSION_LOCK = asyncio.Lock()


def to_arabic_digits(text: str) -> str:
    if not ARABIC_INDIC_DIGITS:
        return text
    return text.translate(_ARABIC_INDIC)


async def _get_http_session(timeout: float = PROVIDER_TIMEOUT) -> aiohttp.ClientSession:
    """Reuse a single ClientSession to cut connection overhead."""

    global _HTTP_SESSION
    async with _HTTP_SESSION_LOCK:
        if _HTTP_SESSION and not _HTTP_SESSION.closed:
            return _HTTP_SESSION
        connector = aiohttp.TCPConnector(limit=100, limit_per_host=0, enable_cleanup_closed=True, ttl_dns_cache=120)
        _HTTP_SESSION = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), connector=connector)
        return _HTTP_SESSION


async def close_http_session() -> None:
    """Close the shared translation ClientSession on shutdown."""

    global _HTTP_SESSION
    async with _HTTP_SESSION_LOCK:
        if _HTTP_SESSION and not _HTTP_SESSION.closed:
            await _HTTP_SESSION.close()
        _HTTP_SESSION = None


def _rtl_css_block(lang_code: str = "ar") -> str:
    lang = (lang_code or "ar").lower()
    if lang in {"ku", "ckb"}:
        font_stack = "\"Arial\",\"Tahoma\",sans-serif"
        line_height = "1.9"
    else:
        font_stack = "\"Arial\",\"Tahoma\",sans-serif"
        line_height = "1.7"
    return (
        "\n<style>\n"
        "  html, body { direction: rtl; unicode-bidi: isolate-override; }\n"
        f"  body {{ font-family: {font_stack}; line-height: {line_height}; font-size: 15px; word-break: break-word; }}\n"
        "  table { direction: rtl; width: 100%; border-collapse: collapse; }\n"
        "  td, th { text-align: right; vertical-align: top; padding: 4px; }\n"
        "  img { max-width: 100%; height: auto; }\n"
        "  .ltr, .vin, .code { direction: ltr; unicode-bidi: embed; font-family: \"DejaVu Sans Mono\",\"Consolas\",monospace; }\n"
        "</style>\n"
    )


def inject_rtl(html_str: str, lang: str = "ar") -> str:
    lang_code = (lang or "ar").lower()
    if lang_code not in RTL_LANGS:
        return html_str or ""
    try:
        html = html_str or ""
        if "<html" not in html.lower():
            html = "<!doctype html><html><head></head><body>" + html + "</body></html>"

        def _apply_html(match: Match[str]) -> str:
            attrs = match.group(1)
            attrs = re.sub(r"\s(lang|dir)\s*=\s*(['\"]).*?\2", "", attrs, flags=re.I)
            return f"<html{attrs} lang='{lang_code}' dir='rtl'>"

        html = re.sub(r"(?i)<html([^>]*)>", _apply_html, html, count=1)

        if re.search(r"(?i)<head[^>]*>", html):
            html = re.sub(r"(?i)<head([^>]*)>", lambda m: f"<head" + m.group(1) + ">" + _rtl_css_block(lang_code), html, count=1)
        else:
            html = re.sub(r"(?i)(<html[^>]*>)", lambda m: m.group(1) + f"<head>{_rtl_css_block(lang_code)}</head>", html, count=1)
        return html
    except Exception:
        return html_str or ""


# Map Kurdish to Sorani (Arabic script) for translation providers
KU_TARGET = "ckb"

# Basic Latin-to-Sorani transliteration (best-effort, lightweight)
_KU_LATIN_MAP = {
    "a": "ا", "b": "ب", "c": "ج", "ç": "چ", "d": "د", "e": "ە", "ê": "ێ", "f": "ف",
    "g": "گ", "h": "ھ", "i": "ی", "î": "ی", "j": "ژ", "k": "ک", "l": "ل", "m": "م",
    "n": "ن", "o": "ۆ", "p": "پ", "q": "ق", "r": "ر", "s": "س", "ş": "ش", "t": "ت",
    "u": "و", "û": "وو", "v": "ڤ", "w": "و", "x": "خ", "y": "ی", "z": "ز",
    "â": "ا", "î": "ی", "ô": "ۆ", "û": "وو",
}


def _latin_ku_to_arabic(text: str) -> str:
    if not text:
        return text
    out = []
    for ch in text:
        lower = ch.lower()
        mapped = _KU_LATIN_MAP.get(lower)
        out.append(mapped if mapped else ch)
    return "".join(out)


def _ensure_kurdish_arabic(text: str) -> str:
    """Force Sorani output by transliterating any Latin characters to Arabic script."""

    return _latin_ku_to_arabic(text or "")


def _ensure_kurdish_arabic_batch(texts: List[str]) -> List[str]:
    return [_ensure_kurdish_arabic(t) for t in texts]


def _apply_kurdish_arabic_to_soup(soup: Any) -> None:
    """Mutate BeautifulSoup tree to force Sorani Arabic text nodes."""

    if soup is None:
        return
    try:
        for element in soup.find_all(text=True):
            if element.parent and element.parent.name in ("script", "style", "noscript"):
                continue
            element.replace_with(_ensure_kurdish_arabic(str(element)))
    except Exception:
        return


def _normalize_target(target: str) -> str:
    t = (target or "ar").lower()
    if t in KURDISH_LANGS:
        return KU_TARGET
    return t


def _preprocess_kurdish_texts(texts: list[str], target: str) -> list[str]:
    if (target or "").lower() not in KURDISH_LANGS:
        return texts
    return [_latin_ku_to_arabic(t) for t in texts]


def _cache_get_batch(texts: List[str], target: str) -> Tuple[Dict[str, str], List[str]]:
    hits: Dict[str, str] = {}
    missing: List[str] = []
    now = time.time()
    for text in texts:
        key = (target, text)
        cached = _BATCH_CACHE.get(key)
        if cached and cached[0] > now:
            hits[text] = cached[1]
        else:
            missing.append(text)
    return hits, missing


def _cache_set_batch(pairs: Dict[str, str], target: str) -> None:
    expires = time.time() + _CACHE_TTL
    if len(_BATCH_CACHE) > 4096:
        _BATCH_CACHE.clear()
    for original, translated in pairs.items():
        _BATCH_CACHE[(target, original)] = (expires, translated)


async def _azure_translate(session: aiohttp.ClientSession, texts: List[str], target_lang: str, defaults: Dict[str, str]) -> Optional[List[str]]:
    key = defaults.get("AZURE_TRANSLATOR_KEY", "")
    if not key:
        return None
    try:
        endpoint = defaults.get("AZURE_TRANSLATOR_ENDPOINT", "https://api.cognitive.microsofttranslator.com")
        url = f"{endpoint}/translate?api-version=3.0&to={target_lang}"
        headers = {
            "Ocp-Apim-Subscription-Key": key,
            "Ocp-Apim-Subscription-Region": defaults.get("AZURE_TRANSLATOR_REGION", "global") or "global",
            "Content-Type": "application/json",
        }
        body = [{"Text": t} for t in texts]
        async with session.post(url, json=body, headers=headers) as resp:
            data = await resp.json()
            payloads: List[Dict[str, Any]] = []
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        payloads.append(cast(Dict[str, Any], item))
            translated: List[str] = []
            for item in payloads:
                translations_raw = item.get("translations")
                translations: List[Dict[str, Any]] = []
                if isinstance(translations_raw, list):
                    for t in translations_raw:
                        if isinstance(t, dict):
                            translations.append(cast(Dict[str, Any], t))
                if not translations:
                    continue
                first = translations[0]
                tr = str(first.get("text", ""))
                translated.append(tr)
            if len(translated) == len(texts):
                return translated
    except Exception:
        return None
    return None


async def _google_cloud_translate(session: aiohttp.ClientSession, texts: List[str], target_lang: str, defaults: Dict[str, str]) -> Optional[List[str]]:
    key = defaults.get("GOOGLE_TRANSLATE_API_KEY", "")
    if not key:
        return None
    try:
        url = f"https://translation.googleapis.com/language/translate/v2?key={key}"
        payload: Dict[str, Any] = {"q": texts, "target": target_lang, "format": "text"}
        headers = {"Content-Type": "application/json"}
        async with session.post(url, json=payload, headers=headers) as resp:
            data = await resp.json()
            translations: List[Dict[str, Any]] = []
            if isinstance(data, dict):
                data_section = data.get("data")
                if isinstance(data_section, dict):
                    maybe_translations = data_section.get("translations")
                    if isinstance(maybe_translations, list):
                        for t in maybe_translations:
                            if isinstance(t, dict):
                                translations.append(cast(Dict[str, Any], t))
            outs = [str(item.get("translatedText", "")) for item in translations]
            if len(outs) == len(texts):
                return outs
    except Exception:
        return None
    return None


async def _libre_translate(session: aiohttp.ClientSession, texts: List[str], target_lang: str, defaults: Dict[str, str]) -> Optional[List[str]]:
    libre_url = defaults.get("LIBRETRANSLATE_URL", "")
    if not libre_url:
        return None
    try:
        endpoint = libre_url.rstrip("/") + "/translate" if not libre_url.endswith("/translate") else libre_url
        headers = {"Content-Type": "application/json"}
        results: List[str] = []
        api_key = defaults.get("LIBRETRANSLATE_API_KEY", "")
        for text in texts:
            payload = {"q": text, "source": "auto", "target": target_lang, "format": "text"}
            if api_key:
                payload["api_key"] = api_key
            async with session.post(endpoint, json=payload, headers=headers) as resp:
                data = await resp.json()
                results.append(data.get("translatedText", ""))
        if len(results) == len(texts):
            return results
    except Exception:
        return None
    return None


async def _custom_translate(session: aiohttp.ClientSession, texts: List[str], target_lang: str, defaults: Dict[str, str]) -> Optional[List[str]]:
    custom_url = defaults.get("TRANSLATE_API_URL", "")
    if not custom_url:
        return None
    try:
        headers = {"Content-Type": "application/json"}
        custom_key = defaults.get("TRANSLATE_API_KEY", "")
        if custom_key:
            headers["Authorization"] = f"Bearer {custom_key}"
        payload: Dict[str, Any] = {"texts": texts, "target": target_lang, "source": None}
        async with session.post(custom_url, json=payload, headers=headers) as resp:
            data = await resp.json()
            outs_raw = data.get("translations") if isinstance(data, dict) else None
            outs: List[str] = []
            if isinstance(outs_raw, list):
                outs = [str(x) for x in outs_raw]
            if len(outs) == len(texts):
                return outs
    except Exception:
        return None
    return None


async def _google_free_batch(texts: List[str], target_lang: str) -> List[str]:
    if not texts:
        return []
    url = "https://translate.googleapis.com/translate_a/single"
    results: List[str] = []
    timeout = aiohttp.ClientTimeout(total=FREE_GOOGLE_TIMEOUT)
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    # Translate many strings per request by joining them with a delimiter that
    # is very unlikely to be changed by translation.
    # This avoids issuing hundreds of HTTP requests for large HTML pages.
    delimiter = "__DVSEP__9f3b0a__"
    max_chars_per_request = 3500

    def _chunk_texts(items: List[str]) -> List[List[str]]:
        chunks: List[List[str]] = []
        current: List[str] = []
        current_len = 0
        for item in items:
            item = item or ""
            add_len = len(item) + (len(delimiter) if current else 0)
            if current and current_len + add_len > max_chars_per_request:
                chunks.append(current)
                current = [item]
                current_len = len(item)
                continue
            if current:
                current_len += len(delimiter)
            current.append(item)
            current_len += len(item)
        if current:
            chunks.append(current)
        return chunks

    async with aiohttp.ClientSession(timeout=timeout) as session:

        async def _translate_joined(joined: str) -> str:
            params = {"client": "gtx", "sl": "auto", "tl": target_lang, "dt": "t", "q": joined}
            try:
                async with session.get(url, params=params) as resp:
                    data = await resp.json(content_type=None)
                    if isinstance(data, list) and data and isinstance(data[0], list):
                        parts: List[str] = []
                        for entry in data[0]:
                            if isinstance(entry, list) and entry:
                                segment = entry[0]
                                if isinstance(segment, str):
                                    parts.append(segment)
                        return "".join(parts) if parts else joined
            except Exception:
                return joined
            return joined

        async def _one_chunk(chunk: List[str]) -> List[str]:
            joined = delimiter.join(chunk)
            async with sem:
                translated_joined = await _translate_joined(joined)
            parts = translated_joined.split(delimiter)
            if len(parts) != len(chunk):
                # If the delimiter got altered, fail-soft for this chunk.
                return chunk
            return parts

        chunks = _chunk_texts(texts)
        translated_chunks = await asyncio.gather(*[_one_chunk(ch) for ch in chunks])
        for ch in translated_chunks:
            results.extend(ch)
    return results


async def translate_html_google_free(html_str: str, target: str = "ar") -> str:
    """Translate HTML using only the free Google endpoint (best-effort, fast).

    This exists to provide a reliable parallel fallback path when provider-based
    translation times out or fails silently.

    Notes:
    - Preserves VIN-like tokens by skipping translation for nodes containing VINs.
    - Uses the same RTL injection + Kurdish script enforcement as `translate_html`.
    """

    target_lang_raw = (target or "en").lower()
    target_lang = _normalize_target(target_lang_raw)
    is_kurdish = target_lang_raw in KURDISH_LANGS
    html_input = html_str or ""
    if not html_input:
        return ""

    async with atimed("translate.html.google_free", target=target_lang, html_len=len(html_input), is_kurdish=is_kurdish):
        # IMPORTANT: do NOT transliterate the whole HTML string for Kurdish.
        # Doing so corrupts tag/attribute/class names (e.g. <div class=...>),
        # breaks CSS selectors, and results in "dense text" / many pages.
        # Kurdish script enforcement is handled safely on text nodes later.
        if target_lang == "en":
            return html_input

        rtl = target_lang_raw in RTL_LANGS
        if BeautifulSoup is None:
            return inject_rtl(html_input, lang=target_lang) if rtl else html_input

        try:
            soup = BeautifulSoup(html_input, "html.parser")

            def _segment(text: str, limit: int = 2000) -> List[str]:
                if len(text) <= limit:
                    return [text]
                parts: List[str] = []
                current: List[str] = []
                current_len = 0
                for chunk in re.split(r"(\.\s+|\n)", text):
                    if not chunk:
                        continue
                    if current_len + len(chunk) > limit and current:
                        parts.append("".join(current))
                        current = []
                        current_len = 0
                    current.append(chunk)
                    current_len += len(chunk)
                if current:
                    parts.append("".join(current))
                return parts or [text]

            text_nodes: List[Any] = []
            for element in soup.find_all(text=True):
                if element.parent and element.parent.name in ("script", "style", "noscript"):
                    continue
                raw = str(element)
                if not _is_visible_text_node(raw):
                    continue
                # Keep VINs intact.
                if re.search(r"\b[A-HJ-NPR-Z0-9]{17}\b", raw):
                    continue
                text_nodes.append(element)

            originals: List[str] = []
            idx_map: dict[str, int] = {}
            expanded_map: List[List[str]] = []
            for node in text_nodes:
                raw = str(node)
                if raw not in idx_map:
                    idx_map[raw] = len(originals)
                    originals.append(raw)
                    expanded_map.append(_segment(raw))

            if not originals:
                # Nothing to translate; still enforce RTL wrapper.
                result_html = str(soup)
                return inject_rtl(result_html, lang=target_lang) if rtl else result_html

            flat_segments: List[str] = [seg for segments in expanded_map for seg in segments]

            # Use the shared cache to avoid repeating work across requests.
            cached_hits, missing = _cache_get_batch(flat_segments, target_lang)
            translated_map: Dict[str, str] = dict(cached_hits)
            if missing:
                try:
                    translated_missing = await asyncio.wait_for(
                        _google_free_batch(missing, target_lang),
                        timeout=FREE_GOOGLE_TIMEOUT + 1,
                    )
                except Exception:
                    translated_missing = []
                if translated_missing and len(translated_missing) == len(missing):
                    if is_kurdish:
                        translated_missing = _ensure_kurdish_arabic_batch(translated_missing)
                    pairs = dict(zip(missing, translated_missing))
                    _cache_set_batch(pairs, target_lang)
                    translated_map.update(pairs)
                else:
                    # Fail-soft for missing entries.
                    translated_map.update({m: m for m in missing})

            translated_segments = [translated_map.get(seg, seg) for seg in flat_segments]

            rebuilt: List[str] = []
            cursor = 0
            for segments in expanded_map:
                count = len(segments)
                rebuilt.append("".join(translated_segments[cursor:cursor + count]))
                cursor += count

            for node in text_nodes:
                idx = idx_map.get(str(node))
                if idx is not None:
                    node.replace_with(rebuilt[idx])

            if is_kurdish:
                _apply_kurdish_arabic_to_soup(soup)

            for element in soup.find_all(text=True):
                text_value = str(element) if element else ""
                if re.search(r"\b[A-HJ-NPR-Z0-9]{17}\b", text_value):
                    try:
                        vin_wrapper = soup.new_tag("span", attrs={"class": "vin"})
                        element.wrap(vin_wrapper)
                    except Exception:
                        pass

            if ARABIC_INDIC_DIGITS:
                for element in soup.find_all(text=True):
                    if element.parent and element.parent.name in ("script", "style", "noscript"):
                        continue
                    element.replace_with(to_arabic_digits(str(element)))

            result_html = str(soup)
            return inject_rtl(result_html, lang=target_lang) if rtl else result_html
        except Exception:
            return inject_rtl(html_input, lang=target_lang) if rtl else html_input


async def translate_batch(texts: List[str], target: str = "ar") -> List[str]:
    texts = [t or "" for t in texts]
    if not texts:
        return []

    cfg = get_env()
    defaults = cfg.translator_defaults
    target_lang = _normalize_target(target)
    is_kurdish = (target or "").lower() in KURDISH_LANGS or target_lang in {"ku", KU_TARGET}
    texts = _preprocess_kurdish_texts(texts, target)

    async with atimed("translate.batch", target=target_lang, n=len(texts), is_kurdish=is_kurdish):
        # Deduplicate while preserving order
        seen = set()
        deduped: List[str] = []
        for t in texts:
            if t not in seen:
                seen.add(t)
                deduped.append(t)

        cached_hits, missing = _cache_get_batch(deduped, target_lang)
        if not missing:
            merged_hits = cached_hits
            if is_kurdish:
                merged_hits = {k: _ensure_kurdish_arabic(v) for k, v in cached_hits.items()}
            return [to_arabic_digits(merged_hits[t]) for t in texts]

        session = await _get_http_session(timeout=PROVIDER_TIMEOUT)

        providers = [
            lambda: _azure_translate(session, missing, target_lang, defaults),
            lambda: _google_cloud_translate(session, missing, target_lang, defaults),
            lambda: _libre_translate(session, missing, target_lang, defaults),
            lambda: _custom_translate(session, missing, target_lang, defaults),
        ]

        async def _race() -> Optional[List[str]]:
            tasks = [asyncio.create_task(p()) for p in providers]
            done, pending = await asyncio.wait(tasks, timeout=PROVIDER_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
            result: Optional[List[str]] = None
            for task in done:
                try:
                    candidate = task.result()
                    if candidate and len(candidate) == len(missing):
                        result = candidate
                        break
                except Exception:
                    continue
            for task in pending:
                task.cancel()
            return result

        try:
            provider_result = await asyncio.wait_for(_race(), timeout=PROVIDER_TIMEOUT + 0.5)
        except Exception:
            provider_result = None

        if provider_result and len(provider_result) == len(missing):
            if is_kurdish:
                provider_result = _ensure_kurdish_arabic_batch(provider_result)
            _cache_set_batch(dict(zip(missing, provider_result)), target_lang)
            merged: Dict[str, str] = {**cached_hits, **dict(zip(missing, provider_result))}
            if is_kurdish:
                merged = {k: _ensure_kurdish_arabic(v) for k, v in merged.items()}
            return [to_arabic_digits(merged[t]) for t in texts]

        # Google free fallback (batched, parallel-limited)
        try:
            free = await asyncio.wait_for(_google_free_batch(missing, target_lang), timeout=FREE_GOOGLE_TIMEOUT + 1)
            if free and len(free) == len(missing):
                if is_kurdish:
                    free = _ensure_kurdish_arabic_batch(free)
                _cache_set_batch(dict(zip(missing, free)), target_lang)
                merged: Dict[str, str] = {**cached_hits, **dict(zip(missing, free))}
                if is_kurdish:
                    merged = {k: _ensure_kurdish_arabic(v) for k, v in merged.items()}
                return [to_arabic_digits(merged[t]) for t in texts]
        except Exception:
            pass

        # googletrans legacy fallback
        try:  # pragma: no cover - network dependent
            from googletrans import Translator  # type: ignore

            translator: Any = cast(Any, Translator)()
            result: Any = translator.translate(missing, dest=target_lang)
            seq: List[Any] = result if isinstance(result, list) else [result]
            translated_missing = [str(item.text) for item in seq]
            if is_kurdish:
                translated_missing = _ensure_kurdish_arabic_batch(translated_missing)
            if len(translated_missing) == len(missing):
                _cache_set_batch(dict(zip(missing, translated_missing)), target_lang)
                merged: Dict[str, str] = {**cached_hits, **dict(zip(missing, translated_missing))}
                if is_kurdish:
                    merged = {k: _ensure_kurdish_arabic(v) for k, v in merged.items()}
                return [to_arabic_digits(merged[t]) for t in texts]
        except Exception:
            pass

        # total failure: return originals (but still cache hits if any)
        merged: Dict[str, str] = {**cached_hits, **{m: m for m in missing}}
        if is_kurdish:
            merged = {k: _ensure_kurdish_arabic(v) for k, v in merged.items()}
        return [to_arabic_digits(merged[t]) for t in texts]


def _is_visible_text_node(text: str) -> bool:
    if not text:
        return False
    if not re.search(r"[A-Za-z\u0600-\u06FF]", text):
        return False
    return len(text.strip()) >= 2


async def translate_html(html_str: str, target: str = "ar") -> str:
    """Translate HTML strictly; لا fallback للإنجليزية عند فشل المزود.

    - إذا فشلت كل المزودات، يُعاد النص الأصلي مع RTL/LTR فقط، لكن لا يُبدَّل إلى لغة أخرى.
    - يحافظ على VIN بتوسيم منفصل.
    """

    target_lang_raw = (target or "en").lower()
    target_lang = _normalize_target(target_lang_raw)
    is_kurdish = target_lang_raw in KURDISH_LANGS
    html_input = html_str or ""
    if not html_input:
        return ""
    async with atimed("translate.html", target=target_lang, html_len=len(html_input), is_kurdish=is_kurdish):
        # IMPORTANT: do NOT transliterate the whole HTML string for Kurdish.
        # Doing so corrupts tag/attribute/class names and breaks the report layout.
        # Kurdish script enforcement is handled on extracted text segments / soup nodes.
        if target_lang == "en":
            return html_input
        rtl = target_lang_raw in RTL_LANGS
        if BeautifulSoup is None:
            return inject_rtl(html_input, lang=target_lang) if rtl else html_input

        try:
            soup = BeautifulSoup(html_input, "html.parser")
            text_nodes: List[Any] = []
            for element in soup.find_all(text=True):
                if element.parent and element.parent.name in ("script", "style", "noscript"):
                    continue
                raw = str(element)
                if _is_visible_text_node(raw):
                    text_nodes.append(element)

            # Split very long text nodes to avoid provider limits (e.g., 4-5k chars).
            def _segment(text: str, limit: int = 4000) -> List[str]:
                if len(text) <= limit:
                    return [text]
                parts: List[str] = []
                current: List[str] = []
                current_len = 0
                for chunk in re.split(r"(\.\s+|\n)", text):
                    if not chunk:
                        continue
                    if current_len + len(chunk) > limit and current:
                        parts.append("".join(current))
                        current = []
                        current_len = 0
                    current.append(chunk)
                    current_len += len(chunk)
                if current:
                    parts.append("".join(current))
                return parts or [text]

            originals: List[str] = []
            idx_map: dict[str, int] = {}
            expanded_map: List[List[str]] = []  # segments per original
            for node in text_nodes:
                raw = str(node)
                if raw not in idx_map:
                    idx_map[raw] = len(originals)
                    originals.append(raw)
                    expanded_map.append(_segment(raw))

            # Flatten segments for batch translation
            flat_segments: List[str] = [seg for segments in expanded_map for seg in segments]
            try:
                translated_segments = await asyncio.wait_for(
                    translate_batch(flat_segments, target=target_lang),
                    timeout=TRANSLATE_TOTAL_TIMEOUT,
                )
            except Exception:
                translated_segments = []
            if not translated_segments or len(translated_segments) != len(flat_segments):
                translated_segments = flat_segments  # fallback to original

            # Arabic-only: if translation is *partial* (some segments remain unchanged and still
            # contain Latin letters), do a very small, capped Google-free fallback for those
            # specific segments. This improves coverage without translating everything twice.
            if target_lang_raw == "ar":
                try:
                    unchanged_idx: List[int] = []
                    for i, (src, out) in enumerate(zip(flat_segments, translated_segments)):
                        if out != src:
                            continue
                        # Only attempt fallback for segments that look like English labels.
                        if not re.search(r"[A-Za-z]", src):
                            continue
                        if len(src.strip()) < 2:
                            continue
                        if len(src) > 300:
                            continue
                        unchanged_idx.append(i)

                    # Only do fallback when it likely matters (avoid overhead on good translations).
                    if unchanged_idx and (len(unchanged_idx) / max(1, len(flat_segments))) >= 0.08:
                        max_items = 160
                        idx_slice = unchanged_idx[:max_items]
                        src_slice = [flat_segments[i] for i in idx_slice]
                        # Deduplicate to save requests.
                        uniq_map: Dict[str, List[int]] = {}
                        for pos, s in zip(idx_slice, src_slice):
                            uniq_map.setdefault(s, []).append(pos)
                        uniq_texts = list(uniq_map.keys())

                        fallback_timeout = min(1.5, max(0.8, FREE_GOOGLE_TIMEOUT))
                        fallback_out: List[str] = []
                        try:
                            fallback_out = await asyncio.wait_for(
                                _google_free_batch(uniq_texts, target_lang),
                                timeout=fallback_timeout,
                            )
                        except Exception:
                            fallback_out = []

                        if fallback_out and len(fallback_out) == len(uniq_texts):
                            # Apply fallback only when it actually changed the text.
                            for src_text, tr_text in zip(uniq_texts, fallback_out):
                                if not tr_text or tr_text == src_text:
                                    continue
                                for idx in uniq_map.get(src_text, []):
                                    translated_segments[idx] = tr_text
                except Exception:
                    pass

            # Reconstruct per-original
            rebuilt: List[str] = []
            cursor = 0
            for segments in expanded_map:
                count = len(segments)
                rebuilt.append("".join(translated_segments[cursor:cursor + count]))
                cursor += count

            no_change = rebuilt == originals

            # If no change happened (provider failed silently), try batched Google free as last resort
            if no_change and target_lang != "en":
                try:
                    fallback_rebuilt = await asyncio.wait_for(
                        _google_free_batch(originals, target_lang),
                        timeout=FREE_GOOGLE_TIMEOUT + 1,
                    )
                    rebuilt = fallback_rebuilt if len(fallback_rebuilt) == len(originals) else originals
                except Exception:
                    rebuilt = originals
                no_change = rebuilt == originals

            for node in text_nodes:
                idx = idx_map.get(str(node))
                if idx is not None:
                    node.replace_with(rebuilt[idx])

            if is_kurdish:
                _apply_kurdish_arabic_to_soup(soup)

            for element in soup.find_all(text=True):
                text_value = str(element) if element else ""
                if re.search(r"\b[A-HJ-NPR-Z0-9]{17}\b", text_value):
                    try:
                        vin_wrapper = soup.new_tag("span", attrs={"class": "vin"})
                        element.wrap(vin_wrapper)
                    except Exception:
                        pass

            if ARABIC_INDIC_DIGITS:
                for element in soup.find_all(text=True):
                    if element.parent and element.parent.name in ("script", "style", "noscript"):
                        continue
                    element.replace_with(to_arabic_digits(str(element)))

            result_html = str(soup)
            return inject_rtl(result_html, lang=target_lang) if rtl else result_html
        except Exception:
            return inject_rtl(html_input, lang=target_lang) if rtl else html_input


async def translate_html_to_ar(html_str: str) -> str:
    return await translate_html(html_str, target="ar")
