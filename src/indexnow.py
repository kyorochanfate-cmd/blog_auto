"""IndexNow protocol — Bing/Yandex/Naver等にURLを即通知する。

仕組み:
1. ランダムな英数字キーを生成 (例: "abc123...")
2. ブログのルートに {key}.txt を置く必要がある (内容はキー文字列のみ)
3. https://api.indexnow.org/IndexNow に URL を POST すると検索エンジンが取得しに来る

はてなブログでは独自ドメインなしの場合、サブディレクトリにファイル設置が難しい。
代わりに blog ルート ({hatena_blog_domain}/{key}.txt) の代わりとして、
キーをHatena側にホストできなければ Search Console 連携を案内する仕組みも必要。

ここでは「キーが配布済み (config か Firestoreに保存)」「URLは indexnow へ POST」の最小実装。
キー設置はユーザー手動 (Hatenaの記事1件にキーを書いて公開する手があるが、対応サポート外なので
本実装では「IndexNow キーが設定されているブログのみ送信する」形にする)。
"""
import requests


_INDEXNOW_ENDPOINT = 'https://api.indexnow.org/IndexNow'


def submit_url(blog, article_url):
    """ブログ設定に indexnow_key があればURLを送信する。

    blog: dict — indexnow_key (任意), hatena_blog_domain
    article_url: 公開された記事のURL

    戻り値: (ok: bool, message: str)
    """
    key = (blog.get('indexnow_key') or '').strip()
    domain = (blog.get('hatena_blog_domain') or '').strip()
    if not key or not domain:
        return False, 'indexnow_key 未設定 (スキップ)'
    if not article_url:
        return False, '記事URLが空'

    payload = {
        'host': domain,
        'key': key,
        'urlList': [article_url],
    }
    try:
        r = requests.post(
            _INDEXNOW_ENDPOINT,
            json=payload,
            headers={'Content-Type': 'application/json; charset=utf-8'},
            timeout=15,
        )
    except Exception as e:
        return False, f'送信失敗: {e}'

    if r.status_code in (200, 202):
        return True, f'IndexNow送信OK (status={r.status_code})'
    return False, f'IndexNow失敗 (status={r.status_code}): {r.text[:200]}'
