# -*- coding: utf-8 -*-
"""
fetch_html_from_commoncrawl_warc.py

Athenaで取得したCommon Crawl index CSVから、
対応するWARCレコードをHTTP Rangeで取得し、HTML本文を保存します。

入力CSVに必要な列:
- url
- warc_filename
- warc_record_offset
- warc_record_length

使い方:
  python fetch_html_from_commoncrawl_warc.py
  python fetch_html_from_commoncrawl_warc.py --input athena_index.csv --outdir html_outputs --limit 10

出力:
- html_outputs/html/*.html
- html_outputs/extract_log.csv
"""

import argparse
import csv
import gzip
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests


CC_DATA_BASE = "https://data.commoncrawl.org"


def safe_filename(url: str, idx: int) -> str:
    """URLからWindowsでも安全な短いファイル名を作る。"""
    parsed = urlparse(url)
    host = parsed.netloc.replace(":", "_")
    path = parsed.path.strip("/").replace("/", "_")
    if not path:
        path = "index"
    name = f"{idx:04d}_{host}_{path}"
    name = re.sub(r"[^0-9A-Za-z._-]+", "_", name)
    return name[:180] + ".html"


def fetch_warc_range(warc_filename: str, offset: int, length: int, timeout: int = 120) -> bytes:
    """Common Crawl WARCの指定byte rangeだけを取得する。"""
    warc_url = f"{CC_DATA_BASE}/{warc_filename}"
    end = int(offset) + int(length) - 1
    headers = {
        "Range": f"bytes={int(offset)}-{end}",
        "User-Agent": "commoncrawl-teaching-demo/1.0"
    }
    r = requests.get(warc_url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.content


def decompress_if_needed(data: bytes) -> bytes:
    """gzip圧縮されているWARCレコードなら展開する。"""
    try:
        return gzip.decompress(data)
    except OSError:
        return data


def split_warc_http(record_bytes: bytes):
    """
    WARCヘッダ、HTTPヘッダ、HTTP本文を分離する。
    厳密なWARC parserではないが、Common CrawlのHTML取得デモには十分。
    """
    parts = record_bytes.split(b"\r\n\r\n", 1)
    if len(parts) != 2:
        return b"", b"", record_bytes

    warc_header, rest = parts

    parts2 = rest.split(b"\r\n\r\n", 1)
    if len(parts2) != 2:
        return warc_header, b"", rest

    http_header, http_body = parts2
    return warc_header, http_header, http_body


def detect_encoding(http_header: bytes) -> str:
    """HTTPヘッダからcharsetを推定。なければ空文字。"""
    text = http_header.decode("iso-8859-1", errors="ignore")
    m = re.search(r"charset=([A-Za-z0-9_\-]+)", text, flags=re.I)
    return m.group(1) if m else ""


def decode_html(http_header: bytes, body: bytes) -> str:
    """HTML本文を文字列化する。"""
    encodings = []
    enc = detect_encoding(http_header)
    if enc:
        encodings.append(enc)

    encodings += ["utf-8", "cp932", "shift_jis", "euc_jp", "iso-8859-1"]

    for e in encodings:
        try:
            return body.decode(e, errors="replace")
        except Exception:
            continue

    return body.decode("utf-8", errors="replace")


def load_rows(input_csv: str):
    with open(input_csv, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="19587cd1-0e2b-4d35-9116-a9a1098c33d7.csv",
                        help="Athenaで保存したindex CSV")
    parser.add_argument("--outdir", default="commoncrawl_html_output",
                        help="HTML保存先ディレクトリ")
    parser.add_argument("--limit", type=int, default=0,
                        help="処理件数。0なら全件")
    parser.add_argument("--sleep", type=float, default=0.5,
                        help="各取得後の待機秒数")
    args = parser.parse_args()

    input_csv = args.input
    outdir = Path(args.outdir)
    html_dir = outdir / "html"
    html_dir.mkdir(parents=True, exist_ok=True)

    rows = load_rows(input_csv)
    if args.limit and args.limit > 0:
        rows = rows[:args.limit]

    required = {"url", "warc_filename", "warc_record_offset", "warc_record_length"}
    if rows:
        missing = required - set(rows[0].keys())
        if missing:
            raise ValueError(f"入力CSVに必要な列がありません: {missing}")

    log_rows = []
    print(f"rows: {len(rows)}")

    for i, row in enumerate(rows, 1):
        url = row.get("url", "")
        try:
            raw = fetch_warc_range(
                row["warc_filename"],
                row["warc_record_offset"],
                row["warc_record_length"],
            )
            record = decompress_if_needed(raw)
            warc_header, http_header, http_body = split_warc_http(record)
            html_text = decode_html(http_header, http_body)

            filename = safe_filename(url, i)
            out_path = html_dir / filename
            out_path.write_text(html_text, encoding="utf-8", errors="replace")

            ok = "<html" in html_text.lower() or "<!doctype html" in html_text.lower()
            print(f"[{i}/{len(rows)}] OK  {url} -> {out_path}")

            log_rows.append({
                "ok": ok,
                "url": url,
                "html_file": str(out_path),
                "bytes_downloaded": len(raw),
                "html_chars": len(html_text),
                "error": "",
            })

        except Exception as e:
            print(f"[{i}/{len(rows)}] FAIL {url} : {repr(e)}")
            log_rows.append({
                "ok": False,
                "url": url,
                "html_file": "",
                "bytes_downloaded": "",
                "html_chars": "",
                "error": repr(e),
            })

        time.sleep(args.sleep)

    log_path = outdir / "extract_log.csv"
    with open(log_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["ok", "url", "html_file", "bytes_downloaded", "html_chars", "error"]
        )
        writer.writeheader()
        writer.writerows(log_rows)

    print(f"\nSaved HTML files to: {html_dir}")
    print(f"Saved log to       : {log_path}")


if __name__ == "__main__":
    main()
