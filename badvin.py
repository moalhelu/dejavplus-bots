# -*- coding: utf-8 -*-
import os
import re
import json
import time
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import logging
from urllib.parse import urljoin

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)


def select_oldest_badvin_record_with_photos(records):
    """Select the oldest record that actually has photos.

    records: iterable of dict-like items with optional keys:
      - "date" / "sale_date" / "record_date" / "timestamp"
      - "photos": list of urls

    Returns the chosen record dict or None if no record has photos.
    """
    def _parse_ts(rec):
        """Extract a sortable timestamp from common date keys or relative text."""
        def _from_str(raw: str):
            raw = raw.strip()
            # ISO-like
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except Exception:
                pass
            # yyyy-mm-dd
            try:
                return datetime.strptime(raw.split()[0], "%Y-%m-%d").timestamp()
            except Exception:
                pass
            # mm/dd/yyyy
            try:
                return datetime.strptime(raw.split()[0], "%m/%d/%Y").timestamp()
            except Exception:
                pass
            # Relative phrases like "7 years ago", "3 months ago", "a day ago"
            rel = raw.lower()
            if "ago" in rel:
                if rel.startswith("a ") or rel.startswith("an "):
                    count = 1
                    rest = rel.split(" ", 2)[1:]
                else:
                    m = re.search(r"(\d+)", rel)
                    count = int(m.group(1)) if m else None
                    rest = rel.split(" ", 1)[1:] if count else []
                if count:
                    unit = "".join(rest).lower()
                    delta = None
                    if "year" in unit:
                        delta = timedelta(days=365 * count)
                    elif "month" in unit:
                        delta = timedelta(days=30 * count)
                    elif "week" in unit:
                        delta = timedelta(weeks=count)
                    elif "day" in unit:
                        delta = timedelta(days=count)
                    if delta:
                        return (datetime.utcnow() - delta).timestamp()
            return None

        for key in ("timestamp", "date", "sale_date", "record_date", "raw_date", "ts"):
            if key not in rec or rec.get(key) in (None, ""):
                continue
            val = rec.get(key)
            if isinstance(val, (int, float)):
                return float(val)
            if isinstance(val, str):
                ts = _from_str(val)
                if ts is not None:
                    return ts
        return None

    total = len(records)
    photos_records = [r for r in records if r.get("photos")]
    with_photos = len(photos_records)

    if not photos_records:
        logger.info("Badvin selector: no records with photos (total=%s)", total)
        return None

    dated = []
    for rec in photos_records:
        ts = _parse_ts(rec)
        if ts is not None:
            dated.append((ts, rec))

    if dated:
        dated.sort(key=lambda x: x[0])  # oldest first
        ts, chosen = dated[0]
        logger.info(
            "Badvin selector: total=%s with_photos=%s chosen_idx=%s ts=%s photos=%s (dated)",
            total,
            with_photos,
            chosen.get("idx"),
            ts,
            len(chosen.get("photos", [])),
        )
        return chosen

    # No usable dates: pick the last occurrence in DOM order (tends to be older sale history)
    chosen = photos_records[-1]
    logger.info(
        "Badvin selector: total=%s with_photos=%s chosen_idx=%s photos=%s (fallback-last)",
        total,
        with_photos,
        chosen.get("idx"),
        len(chosen.get("photos", [])),
    )
    return chosen

class BadvinScraper:
    def __init__(self, email, password):
        self.email = email
        self.password = password
        self.session = requests.Session()
        self.base_url = "https://badvin.com"
        self.logged_in = False
        self.timeout = float(os.getenv("BADVIN_REQUEST_TIMEOUT", "12") or 12.0)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://badvin.com",
            "Referer": "https://badvin.com/users/login"
        }

    def login(self):
        try:
            login_url = f"{self.base_url}/users/login"
            r = self.session.get(login_url, headers=self.headers, timeout=self.timeout)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            csrf = soup.find('input', {'name':'_csrf'})
            if not csrf or not csrf.get('value'):
                logger.info("No CSRF found on login page")
                return False
            data = {'_csrf': csrf.get('value'), 'email': self.email, 'password': self.password}
            r = self.session.post(login_url, data=data, headers=self.headers, allow_redirects=True, timeout=self.timeout)
            if r.status_code == 200 and ("already logged in" in r.text.lower() or self.email in r.text):
                self.logged_in = True
                return True
            # double check
            r = self.session.get(self.base_url, headers=self.headers, timeout=self.timeout)
            if r.status_code == 200 and ("already logged in" in r.text.lower() or self.email in r.text):
                self.logged_in = True
                return True
            return False
        except Exception as e:
            logger.error(f"Login error: {e}")
            return False

    def search_vin(self, vin):
        try:
            if not self.logged_in and not self.login():
                return None
            r = self.session.get(self.base_url, headers=self.headers, timeout=self.timeout)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            csrf = soup.find('input', {'name':'_csrf'})
            if not csrf or not csrf.get('value'):
                return None
            data = {'_csrf': csrf.get('value'), 'str': vin}
            headers = self.headers.copy()
            headers["Referer"] = self.base_url
            r = self.session.post(f"{self.base_url}/search", data=data, headers=headers, allow_redirects=True, timeout=self.timeout)
            if r.status_code==200 and "/v/" in r.url and vin.lower() in r.url.lower():
                return r.url
            return None
        except Exception as e:
            logger.error(f"search_vin error for {vin}: {e}")
            return None

    def get_report(self, result_url, vin, report_type: str = "basic"):
        """Fetch a report page for the given report_type.

        For purchased accounts, older sale records/photos are often only available
        in FULL report flows. This keeps the previous basic flow but allows
        callers to request other types (e.g. full).
        """
        try:
            report_type = (report_type or "basic").strip().lower() or "basic"
            r = self.session.get(result_url, headers=self.headers, timeout=self.timeout)
            r.raise_for_status()

            buy_url = result_url + f"/buy/?reportType={report_type}"
            r = self.session.get(buy_url, headers={**self.headers, "Referer": result_url}, timeout=self.timeout)
            soup = BeautifulSoup(r.text, "html.parser")
            csrf = soup.find('input', {'name':'_csrf'})

            # If we can't find CSRF/form, try the buySuccess URL directly.
            if not csrf or not csrf.get('value'):
                report_url = result_url + f"?buySuccess={report_type}"
                r = self.session.get(report_url, headers=self.headers, timeout=self.timeout)
                r.raise_for_status()
                return report_url, r.text

            form = soup.find('form', {'action': lambda a: a and '/buy' in a})
            if not form:
                report_url = result_url + f"?buySuccess={report_type}"
                r = self.session.get(report_url, headers=self.headers, timeout=self.timeout)
                r.raise_for_status()
                return report_url, r.text

            submit_url = urljoin(self.base_url, form.get('action'))
            data = {'_csrf': csrf.get('value'), 'reportType': report_type}
            for inp in form.find_all('input'):
                name = inp.get('name'); value = inp.get('value')
                if name and name not in data and name != '_csrf':
                    data[name] = value

            r = self.session.post(
                submit_url,
                data=data,
                headers={**self.headers, "Referer": buy_url},
                allow_redirects=True,
                timeout=self.timeout,
            )

            if f"buySuccess={report_type}" in (r.url or ""):
                return r.url, r.text

            report_url = result_url + f"?buySuccess={report_type}"
            r = self.session.get(report_url, headers=self.headers, timeout=self.timeout)
            r.raise_for_status()
            return report_url, r.text
        except Exception as e:
            logger.error(f"get_report error for {vin}: {e}")
            return None, None

    def get_free_report(self, result_url, vin):
        """Backward-compatible wrapper (defaults to basic)."""
        return self.get_report(result_url, vin, "basic")

    def is_car_image(self, url: str):
        if not url:
            return False
        u = str(url).lower()
        excluded = [
            'payment','logo','stripe','paypal','google-wallet','american-express',
            'discover-card','mastercard','visa','digicert','siteseal','svg','icon',
            'trustpilot','reviews','capterra','badge','banner','button','favicon'
        ]
        if any(k in u for k in excluded):
            return False
        return True

    def extract_car_data_and_images(self, report_content, vin):
        """
        STRICT: Collect images ONLY from the Sale Record section.
        - Scope: inside <div class="js-sale-record-container"> (prefer .bv-gallery if present)
        - Accept: img[src], img[data-src], and <a href> wrapping images
        - Filter: URL must contain the VIN (case-insensitive) and pass is_car_image()
        - Selection: choose the OLDEST sale record block when multiple exist
        - NO page-wide fallback (prevents mixing other cars' images)
        - Accept any count >=1; only empty when no valid URLs.
        """
        try:
            soup = BeautifulSoup(report_content, "html.parser")
            imgs = []
            diag = {
                "sale_section_found": False,
                "sale_record_blocks": 0,
                "json_records": 0,
                "source": "dom",
            }

            def _push(url: str):
                if not url:
                    return
                url = url.strip()
                full = urljoin(self.base_url, url)
                if self.is_car_image(full):
                    imgs.append(full)

            def _parse_date_text_to_ts(raw: str):
                raw = (raw or "").strip()
                if not raw:
                    return None
                # ISO-like
                try:
                    return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
                except Exception:
                    pass
                # yyyy-mm-dd
                try:
                    return datetime.strptime(raw.split()[0], "%Y-%m-%d").timestamp()
                except Exception:
                    pass
                # mm/dd/yyyy
                try:
                    return datetime.strptime(raw.split()[0], "%m/%d/%Y").timestamp()
                except Exception:
                    pass
                # Relative phrases: "2 hours ago" / "8 months ago" / "a day ago"
                rel = raw.lower()
                if "ago" in rel:
                    # handle "a/an"
                    if rel.startswith("a ") or rel.startswith("an "):
                        count = 1
                        unit_part = rel.split(" ", 2)[1:]
                        unit = " ".join(unit_part)
                    else:
                        m = re.search(r"\b(\d+)\b", rel)
                        count = int(m.group(1)) if m else None
                        unit = rel
                    if count:
                        delta = None
                        if "minute" in unit:
                            delta = timedelta(minutes=count)
                        elif "hour" in unit:
                            delta = timedelta(hours=count)
                        elif "day" in unit:
                            delta = timedelta(days=count)
                        elif "week" in unit:
                            delta = timedelta(weeks=count)
                        elif "month" in unit:
                            delta = timedelta(days=30 * count)
                        elif "year" in unit:
                            delta = timedelta(days=365 * count)
                        if delta:
                            return (datetime.utcnow() - delta).timestamp()
                return None

            def _raw_date(node):
                # Prefer explicit attributes
                for attr in ("data-sale-date", "data-date", "data-sold-date", "data-created", "data-updated"):
                    raw = node.get(attr)
                    if raw:
                        return str(raw)
                # Then try to extract a compact date from visible text
                text = node.get_text(" ", strip=True)
                if not text:
                    return None
                # Parenthesized (YYYY-MM-DD)
                m = re.search(r"\((20\d{2}-\d{2}-\d{2})\)", text)
                if m:
                    return m.group(1)
                # YYYY-MM-DD
                m = re.search(r"(20\d{2}-\d{2}-\d{2})", text)
                if m:
                    return m.group(1)
                # MM/DD/YYYY
                m = re.search(r"\b(\d{1,2}/\d{1,2}/20\d{2})\b", text)
                if m:
                    return m.group(1)
                # Relative (e.g., "8 months ago")
                m = re.search(r"\b(\d+\s*(?:minute|hour|day|week|month|year)s?\s+ago)\b", text, flags=re.IGNORECASE)
                if m:
                    return m.group(1)
                m = re.search(r"\b(?:a|an)\s+(?:minute|hour|day|week|month|year)\s+ago\b", text, flags=re.IGNORECASE)
                if m:
                    return m.group(0)
                return None

            def _parse_sale_date(node):
                # Attempt to extract a sortable timestamp from sale-record nodes
                for attr in ("data-sale-date", "data-date", "data-sold-date"):
                    raw = node.get(attr)
                    if raw:
                        ts = _parse_date_text_to_ts(str(raw))
                        if ts is not None:
                            return ts
                for attr in ("data-epoch", "data-ts", "data-timestamp"):
                    raw = node.get(attr)
                    if raw:
                        try:
                            return float(raw)
                        except Exception:
                            pass
                text = node.get_text(" ", strip=True)
                # Prefer an explicit date substring if present
                if text:
                    m = re.search(r"\((20\d{2}-\d{2}-\d{2})\)", text)
                    if m:
                        ts = _parse_date_text_to_ts(m.group(1))
                        if ts is not None:
                            return ts
                    m = re.search(r"\b(\d{1,2}/\d{1,2}/20\d{2})\b", text)
                    if m:
                        ts = _parse_date_text_to_ts(m.group(1))
                        if ts is not None:
                            return ts
                    m = re.search(r"(20\d{2}-\d{2}-\d{2})", text)
                    if m:
                        ts = _parse_date_text_to_ts(m.group(1))
                        if ts is not None:
                            return ts
                    m = re.search(r"\b(?:a|an|\d+)\s+(?:minute|hour|day|week|month|year)s?\s+ago\b", text, flags=re.IGNORECASE)
                    if m:
                        ts = _parse_date_text_to_ts(m.group(0))
                        if ts is not None:
                            return ts
                return None

            def _cls_str(c):
                if isinstance(c, (list, tuple)):
                    return " ".join([str(x) for x in c if x])
                return str(c or "")

            sale_section = soup.find("div", class_="js-sale-record-container")
            if not sale_section:
                block = None
                for h2 in soup.find_all("h2"):
                    if "sale record" in (h2.get_text(strip=True) or "").lower():
                        block = h2.find_parent("div", class_="block")
                        break
                if block:
                    sale_section = block.find("div", class_="js-sale-record-container")

            search_root = None
            record_summaries = []  # [(idx, ts, photo_count, raw_date)]
            if sale_section:
                diag["sale_section_found"] = True
                # Collect sale-record blocks without accidentally matching nested tiles.
                candidates = []

                galleries = sale_section.find_all("div", class_=lambda c: c and "bv-gallery" in _cls_str(c))
                if galleries:
                    for idx, gallery in enumerate(galleries):
                        node = gallery
                        # Walk up a bit to a container that likely represents a single sale record.
                        for _ in range(6):
                            parent = getattr(node, "parent", None)
                            if not parent or parent == sale_section:
                                break
                            # If parent contains multiple galleries, stop here.
                            try:
                                if len(parent.find_all("div", class_=lambda c: c and "bv-gallery" in _cls_str(c))) > 1:
                                    break
                            except Exception:
                                break
                            node = parent
                        candidates.append((idx, _parse_sale_date(node), gallery, node))
                else:
                    # Fallback: use immediate children of the sale section that contain images
                    children = sale_section.find_all(recursive=False)
                    for idx, node in enumerate(children):
                        if not node.find("img") and not node.find("a", href=True):
                            continue
                        gallery = node.find("div", class_=lambda c: c and "bv-gallery" in _cls_str(c)) or node
                        candidates.append((idx, _parse_sale_date(node), gallery, node))

                if not candidates:
                    # Last fallback: treat the entire container as one record.
                    candidates = [(0, _parse_sale_date(sale_section), sale_section, sale_section)]

                diag["sale_record_blocks"] = len(candidates)

                if candidates:
                    prepared_records = []
                    for idx, ts, gallery, node in candidates:
                        photos_local = []
                        def _push_local(url: str):
                            if not url:
                                return
                            url = url.strip()
                            full = urljoin(self.base_url, url)
                            if self.is_car_image(full):
                                photos_local.append(full)
                        for a in gallery.find_all("a", href=True):
                            _push_local(a.get("href"))
                        for img in gallery.find_all("img"):
                            _push_local(img.get("src"))
                            _push_local(img.get("data-src"))
                        raw_date = _raw_date(node) or _raw_date(gallery)
                        record_summaries.append((idx, ts, len(photos_local), raw_date))
                        prepared_records.append({"idx": idx, "timestamp": ts, "raw_date": raw_date, "photos": photos_local})

                    logger.debug("Badvin records for %s: count=%s", vin, len(prepared_records))
                    for rec in prepared_records:
                        logger.debug(
                            "record[%s]: raw_date=%s ts=%s photos=%s",
                            rec.get("idx"),
                            rec.get("raw_date"),
                            rec.get("timestamp"),
                            len(rec.get("photos", [])),
                        )

                    chosen = select_oldest_badvin_record_with_photos(prepared_records)
                    if chosen and chosen.get("photos"):
                        imgs.extend(chosen["photos"])
                        logger.debug(
                            "Badvin: chose record idx=%s ts=%s photos=%s vin=%s",
                            chosen.get("idx"),
                            chosen.get("timestamp"),
                            len(chosen.get("photos", [])),
                            vin,
                        )
                    else:
                        logger.info("Badvin: no record with photos selected for vin %s", vin)
                logger.info("Badvin: record summaries for vin %s => %s", vin, record_summaries)

            def _extract_sale_records_from_embedded_json() -> list[dict]:
                records: list[dict] = []
                for script in soup.find_all("script"):
                    stype = (script.get("type") or "").strip().lower()
                    # Many pages omit type; accept empty or json-ish
                    if stype and "json" not in stype:
                        continue
                    raw = script.string or script.get_text() or ""
                    raw = raw.strip()
                    if not raw or len(raw) < 20:
                        continue
                    # Quick filter to skip huge non-related scripts
                    if "link_img_hd" not in raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except Exception:
                        continue

                    def _walk(x: object) -> None:
                        if isinstance(x, dict):
                            if "link_img_hd" in x:
                                records.append(x)
                            for v in x.values():
                                _walk(v)
                        elif isinstance(x, list):
                            for it in x:
                                _walk(it)

                    _walk(obj)
                return records

            # If DOM sale-record section yielded nothing, try embedded JSON (JS-rendered pages).
            if not imgs:
                json_records = _extract_sale_records_from_embedded_json()
                diag["json_records"] = len(json_records)
                if json_records:
                    prepared: list[dict] = []
                    for idx, entry in enumerate(json_records):
                        if not isinstance(entry, dict):
                            continue
                        photos: list[str] = []
                        val = entry.get("link_img_hd")
                        if isinstance(val, list):
                            for u in val:
                                if isinstance(u, str) and u.strip():
                                    photos.append(urljoin(self.base_url, u.strip()))
                        elif isinstance(val, str) and val.strip():
                            photos.append(urljoin(self.base_url, val.strip()))

                        date_val = None
                        for k in (
                            "sale_date",
                            "sold_date",
                            "auction_date",
                            "date",
                            "created_at",
                            "updated_at",
                            "saleDate",
                        ):
                            if entry.get(k):
                                date_val = entry.get(k)
                                break
                        prepared.append({"idx": idx, "date": date_val, "timestamp": date_val, "photos": photos})

                    chosen = select_oldest_badvin_record_with_photos(prepared)
                    if chosen and chosen.get("photos"):
                        diag["source"] = "json"
                        for u in chosen["photos"]:
                            if self.is_car_image(u):
                                imgs.append(u)

            # De-duplicate while preserving order
            seen = set()
            out = []
            for u in imgs:
                if u not in seen:
                    seen.add(u)
                    out.append(u)

            logger.info(
                "Badvin: records=%s selected_photos=%s summaries=%s vin=%s",
                len(record_summaries) if 'record_summaries' in locals() else 0,
                len(out),
                record_summaries if 'record_summaries' in locals() else [],
                vin,
            )

            # Accept any non-empty set of images
            if not out:
                logger.info(f"No valid images found for VIN {vin} in sale record section.")
                car_data = {'vin': vin, 'photos_count': 0}
                car_data.update(diag)
                return car_data, []

            car_data = {'vin': vin, 'photos_count': len(out)}
            car_data.update(diag)
            return car_data, out

        except Exception as e:
            logger.error(f"Error extracting car data for {vin}: {e}")
            return {'vin': vin, 'photos_count': 0}, []


    def logout(self):
        try:
            self.session.get(f"{self.base_url}/users/logout", headers=self.headers)
        except Exception:
            pass
        self.logged_in = False
        return True
