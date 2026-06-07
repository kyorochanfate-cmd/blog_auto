"""記事品質を採点して改善提案を返す。

LLM-as-judge ではなく、ヒューリスティック (規則ベース) で採点する。
理由:
- 高速 (Gemini呼び出し不要)
- 決定的 (毎回同じ評価)
- ユーザーが「なぜこの点数か」をすぐ理解できる
"""
import re


def score_article(title, body, ownership='unspecified'):
    """記事を100点満点で採点し、項目別の評価とアドバイスを返す。

    戻り値:
    {
        'total': int (0-100),
        'grade': 'A' | 'B' | 'C' | 'D',
        'items': [
            {'name': str, 'score': int, 'max': int, 'pass': bool, 'advice': str (空ならOK)},
            ...
        ],
        'weak_items': [...] (改善必須の項目だけ),
    }
    """
    items = []

    # 1. タイトル長さ (10点)
    t_len = len(title or '')
    if 28 <= t_len <= 38:
        items.append(_pass('タイトル長さ', 10, f'{t_len}文字 (理想28〜38)'))
    elif 22 <= t_len <= 45:
        items.append(_partial('タイトル長さ', 6, 10, f'{t_len}文字。28〜38文字が理想。'))
    else:
        items.append(_fail('タイトル長さ', 0, 10, f'{t_len}文字。28〜38文字に調整推奨。'))

    # 2. タイトルの強度 (SEOワード含有) (10点)
    seo_keywords = ['vs', '比較', '違い', '徹底', 'おすすめ', '選', 'レビュー', 'まとめ',
                    '後悔', '失敗', 'コスパ', '2026', '最新', '徹底', '違い', '向け', 'べき']
    has_year = re.search(r'(202[0-9]|2030)', title or '') is not None
    has_number = re.search(r'\d', title or '') is not None
    has_seo_word = any(k in (title or '') for k in seo_keywords)
    seo_count = sum([has_year, has_number, has_seo_word])
    if seo_count >= 2:
        items.append(_pass('タイトルの強度', 10, '数字・SEOワード・年号のうち2つ以上を含む'))
    elif seo_count == 1:
        items.append(_partial('タイトルの強度', 6, 10, '数字・SEOワード・年号のうち2つ以上含めると検索性UP'))
    else:
        items.append(_fail('タイトルの強度', 0, 10, '数字・年号・「vs/比較/おすすめ/レビュー/2026年版」等を含めると検索流入が増える'))

    # 3. 文字数 (10点)
    b_len = len(body or '')
    if 2500 <= b_len <= 5000:
        items.append(_pass('文字数', 10, f'{b_len}文字 (理想2500〜5000)'))
    elif 1800 <= b_len < 2500:
        items.append(_partial('文字数', 6, 10, f'{b_len}文字。あと{2500-b_len}文字以上加筆推奨。SEO的に2500+が望ましい'))
    elif b_len > 5000:
        items.append(_partial('文字数', 7, 10, f'{b_len}文字。やや長め。中だるみ注意'))
    else:
        items.append(_fail('文字数', 0, 10, f'{b_len}文字。短すぎる。2500文字以上を目指す'))

    # 4. H2見出しの数と質 (15点)
    h2_list = re.findall(r'^##\s+(.+?)\s*$', body or '', re.MULTILINE)
    h2_count = len(h2_list)
    if 5 <= h2_count <= 8:
        items.append(_pass('H2見出しの数', 8, f'{h2_count}個 (理想5〜8)'))
    elif 3 <= h2_count < 5:
        items.append(_partial('H2見出しの数', 5, 8, f'{h2_count}個。あと{5-h2_count}個増やすとSEO的に有利'))
    else:
        items.append(_fail('H2見出しの数', 0, 8, f'{h2_count}個。5〜8個が理想'))

    # H2の長さ (短すぎ・長すぎチェック) (7点)
    if h2_list:
        avg_len = sum(len(h) for h in h2_list) / len(h2_list)
        if 8 <= avg_len <= 30:
            items.append(_pass('H2見出しの内容', 7, f'平均{avg_len:.0f}文字 (適切)'))
        else:
            items.append(_partial('H2見出しの内容', 4, 7, f'平均{avg_len:.0f}文字。8〜30文字程度が読みやすい'))
    else:
        items.append(_fail('H2見出しの内容', 0, 7, 'H2見出しが無い'))

    # 5. 個人視点の濃さ (15点) — 所有モードで判定基準を切替
    if ownership == 'not_owned':
        # 未所有: 期待感・関心ベースの表現を見る
        not_owned_phrases = [
            '気になっています', '気になります', '気になる',
            '発売されたら', '試してみたい', '触ってみたい', '買ったら', '購入したら',
            '公式によると', '公式情報', '発表によると', '発表された',
            '先行レビュー', 'レビューを見ると', 'レビューでは',
            '期待', '楽しみ', '購入を検討', '検討しています', '検討している',
            'と思います', 'と感じています', 'と予想',
        ]
        # 「実際に使ってみた」系の偽体験表現が混入してたら減点
        bad_owned_phrases = ['実際に使ってみると', '実際に使ってみた', '1週間使った', '1週間試した',
                             '触った感じ', '持った感じ', '使い心地は']
        exp_count = sum(_count_substr(body, p) for p in not_owned_phrases)
        bad_count = sum(_count_substr(body, p) for p in bad_owned_phrases)
        if bad_count >= 1:
            items.append(_fail('未所有者視点の整合', 0, 15,
                f'実体験を装った表現が{bad_count}箇所あります (例: 「実際に使ってみると」)。未所有モードでは「気になっています」「公式によると」のような表現に書き換えてください'))
        elif exp_count >= 5:
            items.append(_pass('未所有者視点の表現', 15, f'期待・関心ベースの表現{exp_count}箇所'))
        elif exp_count >= 3:
            items.append(_partial('未所有者視点の表現', 10, 15, f'期待ベース表現{exp_count}箇所。「気になっている」「発売されたら試したい」をもう少し増やす'))
        elif exp_count >= 1:
            items.append(_partial('未所有者視点の表現', 5, 15, f'期待ベース表現{exp_count}箇所のみ。情報整理に終始せず、ブロガーの関心を表現する'))
        else:
            items.append(_fail('未所有者視点の表現', 0, 15, '関心・期待のニュアンスがゼロ。「気になっています」「発売されたら試したい」等を追加'))
    elif ownership == 'owned':
        # 所有: 物理的体験+生活シーンを見る
        owned_phrases = [
            '使ってみる', '使ってみた', '使ってみると', '試した', '触った', '触れた',
            '感じた', '気になった', '便利', '不便', '測ってみ', '確認したら',
            '1週間', '一週間', '数日', '日間', '数カ月', 'カ月使', 'ヶ月使',
            '通勤', '自宅', '週末', 'カフェ', '運動', '出先', '外出',
            '重さ', '質感', '手触り', '操作感', '押し心地', '発色', '音質', '匂い',
            '私の', '自分の',
        ]
        exp_count = sum(_count_substr(body, p) for p in owned_phrases)
        if exp_count >= 7:
            items.append(_pass('実機レビューの濃さ', 15, f'体験記述{exp_count}箇所'))
        elif exp_count >= 4:
            items.append(_partial('実機レビューの濃さ', 10, 15, f'体験記述{exp_count}箇所。物理感覚 (重さ・質感・操作感) や生活シーン (通勤・自宅) をもっと'))
        elif exp_count >= 1:
            items.append(_partial('実機レビューの濃さ', 5, 15, f'体験記述{exp_count}箇所のみ。実機を持っている強みを活かして「測ってみたら」「自宅で使うと」など具体的に'))
        else:
            items.append(_fail('実機レビューの濃さ', 0, 15, '実機所有のはずなのに体験記述ゼロ。「使ってみると」「測ってみたら」等を追加'))
    else:
        # 指定なし: 既存ロジック
        experience_phrases = [
            '実際に', '使ってみる', '使ってみた', '使ってみると', '試した', '触った', '触れた',
            '感じた', '感じる', 'と思いました', '気になった', '便利', '不便',
            '1週間', '一週間', '数日', '日間', '数カ月', 'カ月使', 'ヶ月使',
            '私の', '自分の', '個人的に', '個人的には',
        ]
        exp_count = sum(_count_substr(body, p) for p in experience_phrases)
        if exp_count >= 5:
            items.append(_pass('個人視点の濃さ', 15, f'{exp_count}箇所の体験記述あり'))
        elif exp_count >= 3:
            items.append(_partial('個人視点の濃さ', 10, 15, f'体験記述{exp_count}箇所。もう少し増やす'))
        elif exp_count >= 1:
            items.append(_partial('個人視点の濃さ', 5, 15, f'体験記述{exp_count}箇所のみ。E-E-A-T評価のため増やす'))
        else:
            items.append(_fail('個人視点の濃さ', 0, 15, '個人視点ゼロ。AIっぽい一般論記事に見える'))

    # 6. 数値・スペック具体性 (10点)
    digit_count = len(re.findall(r'\d', body or ''))
    if digit_count >= 30:
        items.append(_pass('数値の具体性', 10, f'数字{digit_count}個 (具体的)'))
    elif digit_count >= 15:
        items.append(_partial('数値の具体性', 6, 10, f'数字{digit_count}個。価格・スペック等の具体数値を増やす'))
    else:
        items.append(_fail('数値の具体性', 0, 10, f'数字{digit_count}個。具体的な価格・スペック・容量・サイズが不足'))

    # 7. メリット・デメリット両論 (10点)
    pros_words = ['メリット', '良い点', 'おすすめポイント', '魅力', '優れ', '便利']
    cons_words = ['デメリット', '気になる点', '注意点', '欠点', '不便', '残念', '弱点', '物足り']
    has_pros = any(w in (body or '') for w in pros_words)
    has_cons = any(w in (body or '') for w in cons_words)
    if has_pros and has_cons:
        items.append(_pass('メリット・デメリット両論', 10, '良い点と気になる点の両方を記述'))
    elif has_pros or has_cons:
        items.append(_partial('メリット・デメリット両論', 4, 10, '一方しか書かれていない。両方書くと公平な印象でCV向上'))
    else:
        items.append(_fail('メリット・デメリット両論', 0, 10, 'メリット・デメリットの明示が無い'))

    # 8. 比較・代替案 (5点)
    comparison_words = ['比較', 'と比べる', 'と比べて', '一方で', 'vs ', '同価格帯', '同クラス', '代替',
                       '違い', 'よりも', 'に対して']
    comp_count = sum(_count_substr(body, w) for w in comparison_words)
    if comp_count >= 2:
        items.append(_pass('競合比較', 5, f'{comp_count}箇所で競合・代替に言及'))
    elif comp_count == 1:
        items.append(_partial('競合比較', 2, 5, '比較系ワード1箇所。2箇所以上推奨'))
    else:
        items.append(_fail('競合比較', 0, 5, '競合や代替案への言及が無い。「同価格帯ならXX」等を追加'))

    # 9. 画像 (5点)
    img_count = len(re.findall(r'!\[[^\]]*\]\([^)\s]+', body or ''))
    if img_count >= 1:
        items.append(_pass('画像', 5, f'{img_count}枚埋め込み済み'))
    else:
        items.append(_fail('画像', 0, 5, '画像が0枚。最低1枚は欲しい'))

    # 10. FAQ (10点)
    has_faq = re.search(r'^##\s*(よくある質問|FAQ|Q&A)', body or '', re.MULTILINE | re.IGNORECASE) is not None
    if has_faq:
        # FAQ内のQ&A数をカウント
        faq_match = re.search(r'^##\s*(?:よくある質問|FAQ|Q&A).*?$([\s\S]*?)(?=^##\s+|\Z)', body or '', re.MULTILINE | re.IGNORECASE)
        if faq_match:
            faq_content = faq_match.group(1)
            q_count = len(re.findall(r'(?:^###\s|^\*\*Q|^Q[\.:：])', faq_content, re.MULTILINE))
            if q_count >= 3:
                items.append(_pass('FAQ', 10, f'Q&A {q_count}件'))
            elif q_count >= 1:
                items.append(_partial('FAQ', 5, 10, f'Q&A {q_count}件のみ。3件以上推奨'))
            else:
                items.append(_partial('FAQ', 3, 10, 'FAQセクションはあるがQ&A形式が読み取りにくい'))
    else:
        items.append(_fail('FAQ', 0, 10, '「## よくある質問」セクションが無い。FAQリッチスニペット狙いに必要'))

    # 集計
    total = sum(it['score'] for it in items)
    max_total = sum(it['max'] for it in items)
    # 100点満点に換算
    score_100 = round(total / max_total * 100) if max_total else 0

    if score_100 >= 85:
        grade = 'A'
    elif score_100 >= 70:
        grade = 'B'
    elif score_100 >= 55:
        grade = 'C'
    else:
        grade = 'D'

    weak_items = [it for it in items if it['score'] < it['max']]

    return {
        'total': score_100,
        'grade': grade,
        'items': items,
        'weak_items': weak_items,
    }


def build_improvement_instructions(result):
    """スコア結果から「自動改善プロンプト」を組み立てる。"""
    weak = result.get('weak_items', [])
    if not weak:
        return ''
    lines = ['以下の品質基準を満たすように、記事を改善してください:']
    for it in weak:
        if it.get('advice'):
            lines.append(f'- 【{it["name"]}】{it["advice"]}')
    lines.append('')
    lines.append('既存の良い箇所は維持し、上記の改善を追加・強化してください。')
    return '\n'.join(lines)


def _pass(name, score, msg=''):
    return {'name': name, 'score': score, 'max': score, 'pass': True, 'advice': '', 'detail': msg}


def _partial(name, score, max_score, advice):
    return {'name': name, 'score': score, 'max': max_score, 'pass': False, 'advice': advice, 'detail': ''}


def _fail(name, score, max_score, advice):
    return {'name': name, 'score': score, 'max': max_score, 'pass': False, 'advice': advice, 'detail': ''}


def _count_substr(text, sub):
    if not text or not sub:
        return 0
    return text.count(sub)
