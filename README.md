# ThreadsCrawler

抓取 threads.com 公開貼文的爬蟲，輸出 JSON / JSONL / CSV。

## 可以抓到什麼

| 目標 | 可行性 | 方式 |
| --- | --- | --- |
| 公開帳號的貼文（文字、時間、按讚/回覆/轉發數、圖片影片網址、連結預覽） | 可以 | 第一頁來自伺服器端渲染的 HTML；後續頁面走 GraphQL 或瀏覽器滾動 |
| 單篇貼文與它下面的回覆 | 可以 | 貼文頁同樣是伺服器端渲染 |
| 不公開帳號 | 不行 | 需要該帳號同意你追蹤，本工具不繞過任何權限 |
| 搜尋、推薦時間軸 | 多半需要登入 | 可用 `--cookies` 帶自己的登入狀態 |
| 發文 / 讀取自己帳號的數據 | 建議改用官方 API | Meta 的 Threads API（`graph.threads.net`）只開放自己的內容，但穩定且合規 |

Threads 沒有公開的「讀別人貼文」API，所以這裡走的是網頁擷取。網頁結構由 Meta
控制，隨時可能改版；本專案的解析器是**走訪整份 JSON 找出貼文物件**，而不是綁死
在固定路徑上，所以對外層結構調整有一定耐受度。

## 安裝

```bash
pip install -r requirements.txt

# 只有要用 --mode browser 時才需要：
pip install playwright && playwright install chromium
```

## 使用

```bash
# 抓某帳號最新 50 篇，輸出 JSON 到 stdout
python -m threads_crawler @zuck -n 50

# 存成 CSV
python -m threads_crawler @zuck -n 200 -f csv -o zuck.csv

# 單篇貼文（含當頁回覆）
python -m threads_crawler "https://www.threads.com/@zuck/post/C9xAbCdEfGh"

# 用瀏覽器模式（比較慢，但不依賴 doc_id，抓得比較深）
python -m threads_crawler @zuck -n 300 --mode browser

# 帶登入狀態（cookie 字串或瀏覽器匯出的 cookies.txt）
python -m threads_crawler @zuck --cookies cookies.txt
```

常用參數：

- `-n/--limit`：最多抓幾篇（預設 50）
- `-m/--mode`：`auto`（先 HTTP，不夠再開瀏覽器）、`http`、`browser`
- `-f/--format`：`json` / `jsonl` / `csv`
- `--delay`：每次請求間隔秒數，預設 1.5，抓多請調大
- `--raw`：連同原始 payload 一起輸出，方便撈本工具還沒對應的欄位

## 當程式庫用

```python
from threads_crawler import ThreadsClient

client = ThreadsClient(delay=2.0)
for post in client.crawl_profile("zuck", limit=100):
    print(post.published_at, post.like_count, post.text[:40])
```

## 兩種模式的差別

**HTTP 模式**（`threads_crawler/client.py`）：直接請求個人頁，從 HTML 內嵌的
`<script type="application/json">` 取出第一批貼文，之後用網頁版同一個 GraphQL
端點翻頁。快、不用瀏覽器，但翻頁依賴 persisted query 的 `doc_id`，Meta 會不定期
換掉。

換掉之後翻頁會停在第一頁並印出 warning。要更新 `doc_id`：用瀏覽器開某個 Threads
個人頁 → DevTools → Network → 往下滾動 → 找到送往 `/graphql/query` 的請求 →
複製表單裡的 `doc_id`，然後：

```bash
python -m threads_crawler @zuck --doc-id 1234567890123456
# 或 export THREADS_PROFILE_DOC_ID=1234567890123456
```

**瀏覽器模式**（`threads_crawler/browser.py`）：用 Playwright 開 Chromium、模擬
滾動，並直接攔截頁面自己發出的 GraphQL 回應。慢很多，但不需要知道 `doc_id`，
改版時通常還能動。`auto` 模式會在 HTTP 抓不滿 `--limit` 時自動接手。

## 測試

```bash
python -m pytest tests -q
```

測試用本地假伺服器與離線 fixture，不會連到 threads.com，所以 CI 也能跑。

## 使用須知

- 只抓公開內容，不繞過登入牆、不破解權限。
- Threads 服務條款對自動化擷取有限制；請自行確認你的用途（研究、備份自己的內容
  等）是否合規，並遵守 `robots.txt` 與當地個資法規。
- 預設有請求間隔，請不要把 `--delay` 調到 0 去打對方伺服器。收到 HTTP 429 代表
  被限速了，程式會直接中止並提示。
