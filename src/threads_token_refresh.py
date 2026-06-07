"""Threads 長期トークンの自動延長 + 期限通知。

長期トークンは60日有効。 https://graph.threads.net/refresh_access_token を叩くと
新しい60日トークンに延長できる (内部的にスライディングウィンドウ)。

設計:
- 1日1回 cron で各ブログのトークン残日数をチェック
- 残り14日以下 → 自動延長を試行
- 延長成功 → Firestore のトークン+expires_at を更新
- 延長失敗 → Gmail通知 (手動でテスター招待+再生成が必要)
- 残り3日以下になっても延長できてない → 緊急通知
"""
import os
from datetime import datetime, timezone, timedelta
import requests

from webapp import blogs as blog_store
from src import notifier


_REFRESH_API = 'https://graph.threads.net/refresh_access_token'

# 残り日数の閾値
_REFRESH_THRESHOLD_DAYS = 14   # この日数以下なら延長試行
_URGENT_THRESHOLD_DAYS = 3     # この日数以下で延長未成功なら緊急通知


def check_and_refresh_all():
    """全ブログをスキャンしてトークンチェック。

    Returns:
        {'checked': N, 'refreshed': N, 'expired': N, 'failed': N, 'details': [...]}
    """
    blogs = blog_store.list_blogs()
    results = {'checked': 0, 'refreshed': 0, 'expired': 0, 'failed': 0, 'details': []}

    for blog in blogs:
        token = (blog.get('threads_access_token') or '').strip()
        if not token:
            continue  # トークン無いブログはスキップ

        results['checked'] += 1
        r = _check_one(blog)
        results['details'].append(r)
        if r.get('refreshed'):
            results['refreshed'] += 1
        if r.get('expired'):
            results['expired'] += 1
        if r.get('error'):
            results['failed'] += 1

    return results


def _check_one(blog):
    """1ブログ分のチェック + 必要なら延長。"""
    blog_id = blog['id']
    blog_name = blog.get('name', blog_id)
    expires_at = blog.get('threads_token_expires_at')
    now = datetime.now(timezone.utc)

    days_left = None
    if expires_at:
        try:
            # Firestore Timestamp → datetime
            if hasattr(expires_at, 'timestamp'):
                exp_dt = expires_at
            else:
                exp_dt = datetime.fromisoformat(str(expires_at).replace('Z', '+00:00'))
            days_left = (exp_dt - now).total_seconds() / 86400
        except Exception as e:
            print(f'[threads-refresh] expires_at parse failed: {e}', flush=True)

    print(f'[threads-refresh] {blog_name}: days_left={days_left}', flush=True)

    # 残日数十分 → 何もしない
    if days_left is not None and days_left > _REFRESH_THRESHOLD_DAYS:
        return {'blog': blog_name, 'days_left': days_left, 'action': 'ok_no_refresh_needed'}

    # 延長試行
    token = blog['threads_access_token']
    try:
        r = requests.get(_REFRESH_API, params={
            'grant_type': 'th_refresh_token',
            'access_token': token,
        }, timeout=20)
    except Exception as e:
        msg = f'request_exception: {e}'
        _notify_failure(blog_name, days_left, msg)
        return {'blog': blog_name, 'days_left': days_left, 'error': msg}

    if r.status_code >= 400:
        msg = f'HTTP {r.status_code}: {r.text[:300]}'
        print(f'[threads-refresh] FAIL {blog_name}: {msg}', flush=True)

        # 残り3日切ってる → 緊急通知
        if days_left is not None and days_left <= _URGENT_THRESHOLD_DAYS:
            _notify_failure(blog_name, days_left, msg, urgent=True)
        else:
            _notify_failure(blog_name, days_left, msg)

        # 既に期限切れ?
        if days_left is not None and days_left <= 0:
            return {'blog': blog_name, 'days_left': days_left, 'expired': True, 'error': msg}
        return {'blog': blog_name, 'days_left': days_left, 'error': msg}

    data = r.json()
    new_token = data.get('access_token')
    expires_in = int(data.get('expires_in') or 60 * 86400)  # seconds (60日デフォルト)
    if not new_token:
        msg = f'no_access_token_in_response: {data}'
        _notify_failure(blog_name, days_left, msg)
        return {'blog': blog_name, 'days_left': days_left, 'error': msg}

    new_expires_at = now + timedelta(seconds=expires_in)

    # Firestore 更新
    try:
        blog_store.update_threads_token(blog_id, new_token, new_expires_at)
        print(f'[threads-refresh] OK {blog_name}: new_expires_at={new_expires_at.isoformat()}', flush=True)
    except Exception as e:
        msg = f'firestore_update_failed: {e}'
        _notify_failure(blog_name, days_left, msg)
        return {'blog': blog_name, 'days_left': days_left, 'error': msg}

    return {
        'blog': blog_name,
        'days_left': days_left,
        'refreshed': True,
        'new_expires_at': new_expires_at.isoformat(),
    }


def _notify_failure(blog_name, days_left, error_msg, urgent=False):
    """Gmail で延長失敗を通知。notifier が無効なら print のみ。"""
    if not notifier.is_enabled():
        print(f'[threads-refresh] (notify disabled) blog={blog_name} days_left={days_left} err={error_msg}', flush=True)
        return

    subject_prefix = '🚨 緊急' if urgent else '⚠️'
    days_str = f'{days_left:.1f}日' if days_left is not None else '不明'
    subject = f'{subject_prefix} Threadsトークン延長失敗 ({blog_name}, 残り{days_str})'
    body = (
        f'Threadsトークンの自動延長に失敗しました。\n\n'
        f'ブログ: {blog_name}\n'
        f'残り日数: {days_str}\n'
        f'エラー: {error_msg}\n\n'
        '【手動対応手順】\n'
        '1. https://developers.facebook.com/apps/1589280262161941 にアクセス\n'
        '2. ユースケース → Threads API → 設定\n'
        '3. ユーザートークン生成ツールで新トークン発行\n'
        '4. アプリの「ブログ編集」画面で新トークンを貼り直し→保存\n'
    )

    import smtplib, ssl
    from email.message import EmailMessage
    try:
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = os.environ['NOTIFY_GMAIL_ADDRESS']
        msg['To'] = os.environ['NOTIFY_EMAIL']
        msg.set_content(body)
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, context=ctx, timeout=30) as smtp:
            smtp.login(os.environ['NOTIFY_GMAIL_ADDRESS'], os.environ['NOTIFY_GMAIL_APP_PASSWORD'])
            smtp.send_message(msg)
        print(f'[threads-refresh] notify email sent for {blog_name}', flush=True)
    except Exception as e:
        print(f'[threads-refresh] notify email failed: {e}', flush=True)
