"""自動投稿前のコンプライアンス自己検査。

人間レビューを外して全自動運用するため、生成された記事が:
- 元記事のコピペになっていないか (n-gram一致率)
- 画像URLが実在しているか / 著作者表示が付いているか
- 引用ブロック (`> 出典:` / `> 画像:`) が画像に付随しているか

を検査する。違反は重大度別に分類:
- BLOCK: 投稿不可 (下書きに退避してユーザー通知)
- WARN : 投稿可能だが報告したい問題

設計方針:
- ヒューリスティック (規則ベース)。LLM不要なので高速・決定的・無料。
- 日本語の形態素解析は不要。文字n-gramと正規表現で十分。
"""
import re
from collections import Counter
from urllib.parse import urlparse
import requests


# しきい値
_PLAGIARISM_NGRAM = 20            # 20文字連続一致を「コピペ片」とみなす
_PLAGIARISM_MAX_FRAGMENTS = 5     # 1ソースから5片以上は重大コピペ (BLOCK)
_PLAGIARISM_WARN_FRAGMENTS = 1    # 1片でも警告 (WARN — 投稿はする)
# 注: 製品型番 (例: "UGREEN USB Type-C ケーブル PD対応 100W") の連続一致を
# 過剰検出しないため、片数の閾値を 3 → 5 に引き上げ。文章としてのコピペは
# 5片以上で確実に検出される。

_IMG_RE = re.compile(r'!\[[^\]]*\]\((https?://[^)\s]+)\)')
# 画像直後の引用ブロック (画像 / 引用元 / Source / 出典 / Credit 等)
_ATTRIBUTION_RE = re.compile(
    r'^\s*>\s*(画像|引用元|出典|Source|Credit|写真)', re.IGNORECASE | re.MULTILINE
)

# 「自前で投稿した」画像 (Hatena Fotolife) はライセンス検査スキップ
_INTERNAL_IMAGE_HOSTS = ('hatena.ne.jp', 'hatenablog.com', 'st-hatena.com', 'cdn-ak.f.st-hatena.com', 'f.hatena.ne.jp')

# Wikimedia 画像はライセンス表記が特に重要
_WIKI_HOSTS = ('upload.wikimedia.org', 'commons.wikimedia.org')


def check_article(title, body, sources=None, rehost_removed=0):
    """記事のコンプライアンスを検査する。

    Args:
        title: 記事タイトル
        body: 記事本文 (Markdown)
        sources: [{'title','url','source','text','image'}, ...] (生成時の参考情報)
        rehost_removed: rehost_external_images() が削除した画像数 (幻覚URLの目安)

    Returns:
        {
            'ok': bool,                # BLOCK 違反なし
            'blocks': [{'kind','msg'}, ...],   # 投稿不可の違反
            'warns':  [{'kind','msg'}, ...],   # 警告
            'summary': '...'           # ログ表示用1行サマリ
        }
    """
    blocks = []
    warns = []

    # 1. コピペ検査
    if sources:
        plag = _check_plagiarism(body, sources)
        if plag['severity'] == 'block':
            blocks.append({'kind': 'plagiarism', 'msg': plag['msg']})
        elif plag['severity'] == 'warn':
            warns.append({'kind': 'plagiarism', 'msg': plag['msg']})

    # 2. 画像URL / 著作者表示 検査
    img_issues = _check_images(body)
    blocks.extend(img_issues['blocks'])
    warns.extend(img_issues['warns'])

    # 3. rehost 過剰削除 (幻覚画像の兆候)
    if rehost_removed >= 2:
        warns.append({
            'kind': 'image_hallucination',
            'msg': f'rehost時に{rehost_removed}枚の画像URLが無効として削除されました。Geminiが画像URLを幻覚した可能性があります。',
        })

    # 4. タイトル/本文の最低限健全性
    if not (title or '').strip():
        blocks.append({'kind': 'missing_title', 'msg': 'タイトルが空です。'})
    if len((body or '').strip()) < 800:
        blocks.append({
            'kind': 'too_short',
            'msg': f'本文が{len(body or "")}文字しかありません。生成失敗の可能性が高いため投稿しません。',
        })

    summary_parts = []
    if blocks:
        summary_parts.append(f'BLOCK x{len(blocks)}')
    if warns:
        summary_parts.append(f'WARN x{len(warns)}')
    if not summary_parts:
        summary_parts.append('OK')

    return {
        'ok': not blocks,
        'blocks': blocks,
        'warns': warns,
        'summary': ' / '.join(summary_parts),
    }


# ---------- plagiarism ----------

def _normalize_for_compare(text):
    """改行・空白・引用記号を取り除いて比較用の連続テキストにする。"""
    text = re.sub(r'```.*?```', '', text or '', flags=re.DOTALL)  # コードブロック除去
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text)              # 画像除去
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)          # リンクをテキストに
    text = re.sub(r'^>\s.*$', '', text, flags=re.MULTILINE)       # 引用行除去
    text = re.sub(r'[#*_`>\-]', '', text)                         # マークダウン記号除去
    text = re.sub(r'\s+', '', text)                               # 空白全除去
    return text


def _check_plagiarism(body, sources):
    """body と各 source.text の間で 20文字連続一致する箇所を数える。

    `_PLAGIARISM_NGRAM` 文字以上の連続一致片 (重なり除外後) が
    1ソースから `_PLAGIARISM_MAX_FRAGMENTS` 個以上 → block
    1ソースから `_PLAGIARISM_WARN_FRAGMENTS` 個以上 → warn
    """
    body_norm = _normalize_for_compare(body)
    if len(body_norm) < _PLAGIARISM_NGRAM:
        return {'severity': 'none', 'msg': ''}

    worst = {'source': '', 'fragments': [], 'count': 0}
    for src in sources:
        src_text = src.get('text') or ''
        if not src_text or src.get('source') == 'Gemini知識ベース':
            # 知識ベースのスタブはコピー元にならない
            continue
        src_norm = _normalize_for_compare(src_text)
        if len(src_norm) < _PLAGIARISM_NGRAM:
            continue

        fragments = _find_long_common_substrings(body_norm, src_norm, _PLAGIARISM_NGRAM)
        if len(fragments) > worst['count']:
            worst = {
                'source': src.get('source') or src.get('url', ''),
                'fragments': fragments[:5],  # 報告用に最大5片
                'count': len(fragments),
            }

    if worst['count'] >= _PLAGIARISM_MAX_FRAGMENTS:
        sample = ' / '.join(f'「{f[:30]}…」' for f in worst['fragments'][:3])
        return {
            'severity': 'block',
            'msg': f'参考記事「{worst["source"]}」と {worst["count"]} 箇所で{_PLAGIARISM_NGRAM}文字以上の一致あり。コピペの可能性が高い。例: {sample}',
        }
    if worst['count'] >= _PLAGIARISM_WARN_FRAGMENTS:
        sample = f'「{worst["fragments"][0][:30]}…」' if worst['fragments'] else ''
        return {
            'severity': 'warn',
            'msg': f'参考記事「{worst["source"]}」と {worst["count"]} 箇所で{_PLAGIARISM_NGRAM}文字以上の一致あり。例: {sample}',
        }
    return {'severity': 'none', 'msg': ''}


def _find_long_common_substrings(a, b, min_len):
    """a と b の間で min_len 文字以上連続一致する箇所をすべて返す (重なりは1つにマージ)。

    シンプルなスライディング検索。Pythonで a,b 各5000文字程度なら十分速い。
    """
    if not a or not b or len(a) < min_len or len(b) < min_len:
        return []

    found = []
    seen_b_starts = set()  # 同じ箇所を重複検出しない用
    i = 0
    while i <= len(a) - min_len:
        chunk = a[i:i + min_len]
        # b 内で最初の出現を探す
        j = b.find(chunk)
        if j == -1:
            i += 1
            continue
        if j in seen_b_starts:
            i += 1
            continue
        # 一致を可能な限り伸ばす
        end_a, end_b = i + min_len, j + min_len
        while end_a < len(a) and end_b < len(b) and a[end_a] == b[end_b]:
            end_a += 1
            end_b += 1
        match = a[i:end_a]
        found.append(match)
        seen_b_starts.add(j)
        i = end_a  # この一致片はスキップ
    return found


# ---------- image checks ----------

def _is_internal_image(url):
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return any(h in host for h in _INTERNAL_IMAGE_HOSTS)


def _is_wikimedia(url):
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return any(h in host for h in _WIKI_HOSTS)


def _check_images(body):
    """画像URLの実在 & 著作者表示を検査。"""
    blocks, warns = [], []
    if not body:
        return {'blocks': blocks, 'warns': warns}

    # 画像位置と直後の引用ブロックの位置を全て取得
    lines = body.splitlines()
    for line_idx, line in enumerate(lines):
        for m in _IMG_RE.finditer(line):
            url = m.group(1)
            # 1) URL 実在チェック (外部画像のみ)
            if not _is_internal_image(url):
                if not _is_url_reachable(url):
                    blocks.append({
                        'kind': 'broken_image',
                        'msg': f'画像URLにアクセスできません: {url}',
                    })
                    continue  # 壊れた画像は属性検査スキップ

            # 2) 著作者表示の有無 (画像の直後3行以内に `> 画像:` などがあるか)
            has_attr = False
            for j in range(line_idx + 1, min(line_idx + 4, len(lines))):
                if _ATTRIBUTION_RE.match(lines[j]):
                    has_attr = True
                    break
                # 空行はスキップ可
                if lines[j].strip() == '':
                    continue
                # 画像直後の非空行が引用でなければ打ち切り (Wikimediaは特に厳しく)
                if not lines[j].strip().startswith('>'):
                    break

            if _is_wikimedia(url) and not has_attr:
                blocks.append({
                    'kind': 'missing_attribution',
                    'msg': f'Wikimedia画像に著作者表示がありません (CC BY-SA違反): {url}',
                })
            elif not _is_internal_image(url) and not has_attr:
                # 外部画像 (報道写真等) で出典なし → 著作権リスク
                warns.append({
                    'kind': 'missing_attribution',
                    'msg': f'外部画像に出典表示が見当たりません: {url}',
                })

    return {'blocks': blocks, 'warns': warns}


def _is_url_reachable(url, timeout=8):
    """HEAD で 2xx/3xx ならOK。HEADを蹴るサーバはGETで再試行。"""
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True,
                          headers={'User-Agent': 'Mozilla/5.0 (compatible; gadget-blog-bot/1.0)'})
        if r.status_code < 400:
            return True
        # 403/405 など → GETで再試行
        if r.status_code in (403, 405, 501):
            r = requests.get(url, timeout=timeout, stream=True,
                             headers={'User-Agent': 'Mozilla/5.0 (compatible; gadget-blog-bot/1.0)'})
            return r.status_code < 400
        return False
    except Exception:
        return False


def format_issues_for_human(check_result):
    """通知メール用に整形した文字列を返す。"""
    lines = []
    if check_result['blocks']:
        lines.append('【BLOCK (投稿停止)】')
        for b in check_result['blocks']:
            lines.append(f'  - [{b["kind"]}] {b["msg"]}')
    if check_result['warns']:
        lines.append('【WARN (要確認)】')
        for w in check_result['warns']:
            lines.append(f'  - [{w["kind"]}] {w["msg"]}')
    return '\n'.join(lines) if lines else '(問題なし)'
