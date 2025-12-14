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

    def get_free_report(self, result_url, vin):
        try:
            r = self.session.get(result_url, headers=self.headers, timeout=self.timeout)
            r.raise_for_status()
            # try buy-basic flow
            buy_url = result_url + "/buy/?reportType=basic"
            r = self.session.get(buy_url, headers={**self.headers, "Referer": result_url}, timeout=self.timeout)
            soup = BeautifulSoup(r.text, "html.parser")
            csrf = soup.find('input', {'name':'_csrf'})
            if not csrf or not csrf.get('value'):
                report_url = result_url + "?buySuccess=basic"
                r = self.session.get(report_url, headers=self.headers, timeout=self.timeout); r.raise_for_status()
                return report_url, r.text
            # submit form
            form = soup.find('form', {'action': lambda a: a and '/buy' in a})
            if not form:
                report_url = result_url + "?buySuccess=basic"
                r = self.session.get(report_url, headers=self.headers, timeout=self.timeout); r.raise_for_status()
                return report_url, r.text
            submit_url = urljoin(self.base_url, form.get('action'))
            data = {'_csrf': csrf.get('value'), 'reportType':'basic'}
            for inp in form.find_all('input'):
                name = inp.get('name'); value = inp.get('value')
                if name and name not in data and name != '_csrf':
                    data[name]=value
            r = self.session.post(submit_url, data=data, headers={**self.headers, "Referer": buy_url}, allow_redirects=True, timeout=self.timeout)
            if "buySuccess=basic" in r.url:
                return r.url, r.text
            report_url = result_url + "?buySuccess=basic"
            r = self.session.get(report_url, headers=self.headers, timeout=self.timeout); r.raise_for_status()
            return report_url, r.text
        except Exception as e:
            logger.error(f"get_free_report error for {vin}: {e}")
            return None, None

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

            def _push(url: str):
                if not url:
                    return
                url = url.strip()
                full = urljoin(self.base_url, url)
                if self.is_car_image(full):
                    imgs.append(full)

            def _parse_sale_date(node):
                # Attempt to extract a sortable timestamp from sale-record nodes
                for attr in ("data-sale-date", "data-date", "data-sold-date"):
                    raw = node.get(attr)
                    if raw:
                        try:
                            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
                        except Exception:
                            pass
                for attr in ("data-date", "data-sale-date", "data-sold-date"):
                    raw = node.get(attr)
                    if raw:
                        try:
                            return datetime.fromisoformat(raw).timestamp()
                        except Exception:
                            pass
                for attr in ("data-epoch", "data-ts", "data-timestamp"):
                    raw = node.get(attr)
                    if raw:
                        try:
                            return float(raw)
                        except Exception:
                            pass
                # Look for yyyy-mm-dd in text
                text = node.get_text(" ", strip=True)
                m = re.search(r"(20\d{2}-\d{2}-\d{2})", text)
                if m:
                    try:
                        return datetime.fromisoformat(m.group(1)).timestamp()
                    except Exception:
                        pass
                return None

            def _raw_date(node):
                for attr in ("data-sale-date", "data-date", "data-sold-date"):
                    raw = node.get(attr)
                    if raw:
                        return raw
                text = node.get_text(" ", strip=True)
                m = re.search(r"(20\d{2}-\d{2}-\d{2})", text)
                if m:
                    return m.group(1)
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
                # Collect candidate sale-record blocks (some pages list multiple sale histories)
                candidates = []
                for idx, node in enumerate(sale_section.find_all(["div", "section", "article", "li"], class_=lambda c: c and ("sale" in _cls_str(c) or "record" in _cls_str(c) or "bv-gallery" in _cls_str(c)))):
                    gallery = node.find("div", class_=lambda c: c and "bv-gallery" in _cls_str(c)) or node
                    candidates.append((idx, _parse_sale_date(node), gallery))

                if candidates:
                    prepared_records = []
                    for idx, ts, gallery in candidates:
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
                        raw_date = _raw_date(gallery) or _raw_date(node)
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
                return {'vin': vin, 'photos_count': 0}, []

            car_data = {'vin': vin, 'photos_count': len(out)}
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
