"""自動投稿パイプラインから通知メールを送る。

Gmail の SMTP を使う。アプリパスワード必須:
  https://myaccount.google.com/apppasswords

環境変数:
  NOTIFY_EMAIL              通知先 (例: kyorochan.fate@gmail.com)
  NOTIFY_GMAIL_ADDRESS      送信元 Gmail アドレス
  NOTIFY_GMAIL_APP_PASSWORD 16桁アプリパスワード
未設定なら通知はスキップ (ログに残るだけ)。
"""
import os
import smtplib
import ssl
from email.message import EmailMessage


_SMTP_HOST = 'smtp.gmail.com'
_SMTP_PORT = 465


def is_enabled():
    return bool(
        os.environ.get('NOTIFY_EMAIL')
        and os.environ.get('NOTIFY_GMAIL_ADDRESS')
        and os.environ.get('NOTIFY_GMAIL_APP_PASSWORD')
    )


def send_digest(blog_name, results):
    """1ブログ分の自動運用結果をメールで送る。

    Args:
        blog_name: ブログ表示名
        results: [{
            'topic': '...', 'status': 'published'|'drafted'|'failed',
            'url': '...' (publishedのみ),
            'issues_text': '...' (drafted/failedのみ),
            'reason': '...' (failedのみ),
        }, ...]

    BLOCKやfailedが1件もない場合は送信しない (成功通知は鬱陶しいため)。
    """
    needs_attention = [r for r in results if r['status'] != 'published']
    if not needs_attention:
        print(f'[notifier] all published for {blog_name}, no email sent', flush=True)
        return False

    if not is_enabled():
        print(f'[notifier] DISABLED (env vars missing). would notify: {len(needs_attention)} items', flush=True)
        return False

    subject = f'[ブログ自動運用] {blog_name}: 要対応 {len(needs_attention)} 件'
    body = _format_digest(blog_name, results)

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = os.environ['NOTIFY_GMAIL_ADDRESS']
    msg['To'] = os.environ['NOTIFY_EMAIL']
    msg.set_content(body)

    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(_SMTP_HOST, _SMTP_PORT, context=ctx, timeout=30) as smtp:
            smtp.login(os.environ['NOTIFY_GMAIL_ADDRESS'], os.environ['NOTIFY_GMAIL_APP_PASSWORD'])
            smtp.send_message(msg)
        print(f'[notifier] email sent to {os.environ["NOTIFY_EMAIL"]}', flush=True)
        return True
    except Exception as e:
        print(f'[notifier] SMTP send failed: {e}', flush=True)
        return False


def _format_digest(blog_name, results):
    published = [r for r in results if r['status'] == 'published']
    drafted = [r for r in results if r['status'] == 'drafted']
    failed = [r for r in results if r['status'] == 'failed']

    lines = [f'ブログ: {blog_name}', '']
    lines.append(f'公開済み: {len(published)} / 下書き退避: {len(drafted)} / 生成失敗: {len(failed)}')
    lines.append('')

    if drafted:
        lines.append('===== 下書きに退避 (コンプラ違反のためレビュー必要) =====')
        for r in drafted:
            lines.append(f'■ {r["topic"]}')
            lines.append(r.get('issues_text', '(詳細なし)'))
            lines.append('')

    if failed:
        lines.append('===== 生成失敗 (例外) =====')
        for r in failed:
            lines.append(f'■ {r["topic"]}')
            lines.append(f'  理由: {r.get("reason", "(詳細なし)")}')
            lines.append('')

    if published:
        lines.append('===== 公開済み =====')
        for r in published:
            lines.append(f'  - {r["topic"]} → {r.get("url", "")}')
        lines.append('')

    lines.append('Webアプリのトップから下書き一覧を確認してください。')
    return '\n'.join(lines)
