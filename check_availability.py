#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
サッポロビール園 開拓使館（TableCheck）の空席監視スクリプト。

TableCheck の予約画面が実際に使っている内部API
  POST https://production-booking.tablecheck.com/v2/booking/availability_v5/dates
を直接叩いて、指定日・指定時間帯・指定人数の空きを判定する。

空きが出たら
  1) GitHub Issue を作成（→ GitHub からメール通知が届く）
  2) SMTP の環境変数が設定されていれば、直接メールも送る
の両方（または設定されている方だけ）で通知する。

依存パッケージなし（Python 標準ライブラリのみ）。
"""

import json
import os
import smtplib
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

# ─────────────────────────────────────────────────────────
# 設定：ここを書き換えれば別の日付・時間帯・人数にも使えます
# ─────────────────────────────────────────────────────────
SHOP_ID = "sapporobiergarten-kaitakushikan"
SHOP_NAME = "サッポロビール園 開拓使館"

TARGET_DATE = "2026-09-20"                     # 監視したい日 (YYYY-MM-DD)
TARGET_TIMES = [                               # 監視したい開始時刻
    "17:30", "17:45", "18:00", "18:15", "18:30", "18:45", "19:00",
]

# 大人2・子供1・幼児1 = 合計4名。
# TableCheck のこの店舗は「4名」という総数だけで在庫を判定しており、
# pax_child / pax_baby を分けても結果は変わらないことを実測で確認済み。
# そのため予約画面と同じ「大人4名」で問い合わせる（判定は同一）。
PAX_ADULT = 4
PAX_SENIOR = 0
PAX_CHILD = 0
PAX_BABY = 0

# 席カテゴリー。ホール席のIDは実測で特定済み。
# 個室（5名以上・2日前20:00締切）は "6821687bf051e0db9ba5cac3"
SERVICE_CATEGORY_ID = "6821687195bff68ae33913c0"
SERVICE_CATEGORY_NAME = "ホール席"

API_URL = "https://production-booking.tablecheck.com/v2/booking/availability_v5/dates"
BOOKING_URL = (
    f"https://www.tablecheck.com/ja/{SHOP_ID}/reserve"
    f"?num_people={PAX_ADULT + PAX_SENIOR + PAX_CHILD + PAX_BABY}"
    f"&start_date={TARGET_DATE}"
)

JST = timezone(timedelta(hours=9))
TIMEOUT = 30

# ─────────────────────────────────────────────────────────
# 空席判定
# ─────────────────────────────────────────────────────────


def _post_json(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://www.tablecheck.com",
            "Referer": f"https://www.tablecheck.com/ja/{SHOP_ID}/reserve",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def is_slot_open(hhmm):
    """TARGET_DATE の hhmm に空きがあるか判定する。"""
    start_at = f"{TARGET_DATE}T{hhmm}:00.000+09:00"
    want = datetime.fromisoformat(start_at)

    payload = {
        "shop_id": SHOP_ID,
        "start_at": start_at,
        "pax_adult": PAX_ADULT,
        "pax_senior": PAX_SENIOR,
        "pax_child": PAX_CHILD,
        "pax_baby": PAX_BABY,
        "service_category_ids": [SERVICE_CATEGORY_ID],
        "manual_duration": None,
        "start_date": f"{TARGET_DATE}T00:00:00.000+09:00",
        "end_date": f"{TARGET_DATE}T23:59:00.000+09:00",
        "voucher_ids": [],
        "smoking": "none",
        "orders": [],
        "use_experience_page": False,
        "locale": "ja",
    }

    body = _post_json(API_URL, payload)
    slots = (body.get("availability_dates", {}).get("data", {}) or {}).get(TARGET_DATE, [])

    for slot in slots:
        if not slot.get("a"):
            continue
        # slot["t"] は UTC の ISO8601 (例 "2026-09-20T06:30:00Z")
        t = datetime.fromisoformat(slot["t"].replace("Z", "+00:00"))
        if t == want:
            return True
    return False


def find_open_slots():
    open_slots, errors = [], []
    for hhmm in TARGET_TIMES:
        try:
            if is_slot_open(hhmm):
                open_slots.append(hhmm)
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError) as exc:
            errors.append(f"{hhmm}: {type(exc).__name__}: {exc}")
    return open_slots, errors


# ─────────────────────────────────────────────────────────
# 通知
# ─────────────────────────────────────────────────────────


def build_message(open_slots):
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"**{SHOP_NAME}**（{SERVICE_CATEGORY_NAME}）に空きが出ました。",
        "",
        f"- 日付: **{TARGET_DATE}**",
        f"- 空いている時間: **{' / '.join(open_slots)}**",
        f"- 人数: {PAX_ADULT + PAX_SENIOR + PAX_CHILD + PAX_BABY}名（大人2・子供1・幼児1）",
        f"- 検知時刻: {now} JST",
        "",
        f"予約はこちら → {BOOKING_URL}",
        "",
        "> 空席はすぐ埋まることがあります。早めにどうぞ。",
        "> 予約が取れたら、GitHub の Actions タブからこのワークフローを Disable してください。",
    ]
    return "\n".join(lines)


def notify_github_issue(open_slots, body_md):
    """GitHub Issue を作成/更新する。GitHub がアカウントのメール宛に通知を送る。"""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        return "skipped (GITHUB_TOKEN / GITHUB_REPOSITORY 未設定)"

    api = f"https://api.github.com/repos/{repo}/issues"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "tablecheck-watcher",
    }

    def _req(url, method="GET", payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")

    marker = f"<!-- tablecheck-watch:{TARGET_DATE} -->"
    title = f"🍺 空席あり {TARGET_DATE} {'/'.join(open_slots)} — {SHOP_NAME}"

    # 同じ監視対象の open Issue が既にあれば、内容が変わったときだけコメントする
    existing = _req(f"{api}?state=open&per_page=100")
    for issue in existing:
        if marker in (issue.get("body") or ""):
            if "/".join(open_slots) in (issue.get("title") or ""):
                return f"既存 Issue #{issue['number']} と同内容のため通知スキップ"
            _req(
                f"{api}/{issue['number']}/comments",
                "POST",
                {"body": f"{marker}\n\n空き状況が更新されました。\n\n{body_md}"},
            )
            _req(f"{api}/{issue['number']}", "PATCH", {"title": title})
            return f"既存 Issue #{issue['number']} にコメントしました"

    created = _req(api, "POST", {"title": title, "body": f"{marker}\n\n{body_md}"})
    return f"Issue #{created['number']} を作成しました"


def notify_smtp(open_slots, body_md):
    """SMTP の環境変数が揃っていれば直接メールを送る。"""
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    mail_to = os.environ.get("MAIL_TO")
    if not all([host, user, password, mail_to]):
        return "skipped (SMTP_* / MAIL_TO 未設定)"

    port = int(os.environ.get("SMTP_PORT", "465"))

    msg = EmailMessage()
    msg["Subject"] = f"【空席あり】{SHOP_NAME} {TARGET_DATE} {'/'.join(open_slots)}"
    msg["From"] = user
    msg["To"] = mail_to
    msg.set_content(body_md.replace("**", "").replace("> ", ""))

    with smtplib.SMTP_SSL(host, port, timeout=TIMEOUT) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    return f"{mail_to} にメールを送信しました"


def write_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


# ─────────────────────────────────────────────────────────


def main():
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now} JST] {SHOP_NAME} / {TARGET_DATE} / {SERVICE_CATEGORY_NAME} をチェック中…")

    open_slots, errors = find_open_slots()

    for err in errors:
        print(f"  ! エラー {err}", file=sys.stderr)

    if not open_slots:
        print("  → 空きなし")
        write_summary(f"### 空きなし\n{now} JST — {TARGET_DATE} {'/'.join(TARGET_TIMES)}")
        # 全時刻でエラーが出た場合はAPI仕様変更の可能性があるので失敗扱いにする
        if errors and len(errors) == len(TARGET_TIMES):
            print("  !! 全リクエストが失敗しました。API仕様が変わった可能性があります。", file=sys.stderr)
            return 1
        return 0

    print(f"  → 空きあり: {', '.join(open_slots)}")
    body_md = build_message(open_slots)

    print("  GitHub Issue: " + notify_github_issue(open_slots, body_md))
    try:
        print("  SMTP: " + notify_smtp(open_slots, body_md))
    except Exception as exc:  # メール失敗で Issue 通知まで巻き添えにしない
        print(f"  SMTP 送信に失敗: {exc}", file=sys.stderr)

    write_summary(f"### 🍺 空きあり: {' / '.join(open_slots)}\n\n{body_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
