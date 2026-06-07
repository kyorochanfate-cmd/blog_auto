# 商品カード用 Hatena カスタムCSS

下記CSS を **はてなブログ管理画面 → デザイン → カスタマイズ (スパナアイコン) → デザインCSS** に貼り付けて保存してください。

PC・スマホ両方で見やすい商品カードになります。

```css
/* ===== 商品カード (Amazon/楽天) ===== */
.product-card {
  display: flex;
  align-items: center;
  gap: 16px;
  margin: 24px 0;
  padding: 16px;
  border: 1px solid #e0e0e0;
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
  transition: box-shadow 0.2s ease;
}
.product-card:hover {
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.08);
}

.product-card .product-image {
  width: 160px;
  height: 160px;
  object-fit: contain;
  flex-shrink: 0;
  background: #f8f8f8;
  border-radius: 8px;
}

.product-card .product-info {
  flex: 1;
  min-width: 0;
}

.product-card .product-name {
  font-size: 16px;
  font-weight: 700;
  line-height: 1.4;
  margin: 0 0 6px 0;
  color: #222;
  /* 2行で省略 */
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.product-card .product-brand {
  font-size: 12px;
  color: #888;
  margin: 0 0 6px 0;
}

.product-card .product-price {
  font-size: 18px;
  font-weight: 700;
  color: #d32f2f;
  margin: 0 0 12px 0;
}

.product-card .product-buttons {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.product-card .btn {
  display: inline-block;
  padding: 10px 16px;
  border-radius: 6px;
  font-size: 14px;
  font-weight: 700;
  text-align: center;
  text-decoration: none !important;
  color: #fff !important;
  flex: 1;
  min-width: 110px;
  border: none;
  cursor: pointer;
  transition: opacity 0.15s ease;
}
.product-card .btn:hover {
  opacity: 0.85;
  text-decoration: none !important;
}

.product-card .btn-amazon {
  background: #FF9900;
}
.product-card .btn-rakuten {
  background: #BF0000;
}

/* スマホ縦長レイアウト */
@media (max-width: 480px) {
  .product-card {
    flex-direction: column;
    align-items: stretch;
    gap: 12px;
    padding: 12px;
  }
  .product-card .product-image {
    width: 100%;
    height: 200px;
    align-self: center;
  }
  .product-card .product-name {
    font-size: 15px;
  }
  .product-card .product-price {
    font-size: 17px;
    margin-bottom: 10px;
  }
  .product-card .btn {
    padding: 12px;
    font-size: 15px;
  }
}
```

## 確認ポイント

- ボタン色: Amazon = オレンジ (#FF9900)、楽天 = 赤 (#BF0000)
- 画像サイズ: PC 160x160、スマホ全幅 200px 高
- 商品名は最大2行で省略
- スマホでは縦並び (画像 → 名前 → 価格 → ボタン2つ)

## 動作確認

CSS反映後、自動投稿された記事を確認:
1. 商品カードが画像入りで表示されるか
2. 「Amazon」「楽天市場」ボタンをタップしてアフィリエイトURLに飛ぶか
3. スマホで縦並びレイアウトになるか
