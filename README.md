# Threads Crawler — Threads 貼文收集器

固定收集多個 Threads **帳號**或**關鍵字**「過去 N 小時」的貼文，
用瀏覽器操作、下載 CSV（Excel 可直接開）。

| 工具 | 用途 |
|------|------|
| **app.py（網頁版，推薦）** | 設定目標、立即收集、每日排程、下載歷史結果 |
| crawler.py（CLI） | 登入 Threads、或不開網頁直接收集一次 |

---

## 安裝與啟動

需求：Python 3.9 以上，電腦上已安裝 Chrome 或 Edge
（都沒有的話，安裝套件後再執行 `playwright install chromium`）。

```powershell
pip install -r requirements.txt
python app.py
```

瀏覽器開 <http://localhost:8014>：

1. **收集目標**：每行一個，按「儲存設定」
   - `@zuck` 或 `https://www.threads.com/@zuck` → 收集該帳號的貼文
   - 其他文字（如 `生成式AI`）→ 當關鍵字，搜尋最新貼文
2. **立即收集**：填要往回抓幾小時（1～168），即時看進度
3. **歷史結果**：下載每次收集的 CSV / JSON
4. **每日自動收集**（選用）：設定時間點，程式保持開啟就會自動跑

## 要不要登入？

| | 未登入 | 已登入 |
|---|---|---|
| 帳號目標 | 每個帳號最近約 15 串 | 可一直往回捲到時間窗起點 |
| 關鍵字目標 | 只有約 20 篇「熱門」結果，不是最新 | 「最新」分頁，按時間排序 |
| 帳號風險 | 無 | 有被限制的風險，建議用副帳號 |

只追蹤少數帳號、每天收一兩次的話，不登入就夠用。要用關鍵字或抓更長的時間範圍，
在網頁按「主機登入 Threads」（或執行 `python crawler.py login`），
在跳出的視窗用 Instagram 帳號登入一次，session 會存在 `browser_profile/`。
登入視窗會等 10 分鐘（夠過驗證碼），登入成功後自動關閉。
「立即收集」跳出的視窗是自動瀏覽用的，跑完就關，不要在那個視窗登入。

## 輸出欄位

`output/threads_日期_時分.csv`（+ `.json`）：

| 欄位 | 意義 |
|------|------|
| `source` | 來源目標（`@帳號` 或關鍵字；同一篇被多個目標撈到時以 ` \| ` 串接） |
| `post_id` / `url` | 貼文 ID 與連結 |
| `time` | 發文時間（台北時間） |
| `author` | 作者帳號 |
| `text` | 內文。作者自己接續回覆的「串文」會併成同一篇，段落間空一行 |
| `likes` / `replies` / `reposts` / `quotes` / `shares` | 讚、回覆、轉發、引用、分享數（串文以第一則為準） |
| `is_reply` | 這篇是不是一則回覆（關鍵字搜尋會撈到回覆） |
| `images` | 附圖網址（影片為縮圖）；網址有時效，要用請趁早下載 |

## 設定都在 config.json（改完重啟 app.py）

| 欄位 | 意義 |
|------|------|
| `targets` | 收集目標，每個一行（也可以在網頁上改） |
| `schedule_times` | 每日自動收集的時間點，可多個，如 `["01:00", "13:00"]` |
| `schedule_enabled` | `true` 才會自動收集，否則只手動按「立即收集」 |
| `schedule_hours` | 每次自動收集往回抓幾小時（1～168） |
| `max_scrolls` | 每個目標最多捲動幾次；時間窗 > 24 小時時程式會自動提高 |
| `save_json` | `false` 就只輸出 CSV，不另外寫一份 JSON |
| `headless` | `true` 收集時不開瀏覽器視窗（登入帳號時較容易被偵測） |
| `host` | 預設 `127.0.0.1` 只有本機能開；改 `0.0.0.0` 讓內網同仁連線 |
| `port` | 網頁服務的埠號（預設 8014） |
| `upload_urls` / `upload_api_key` | 選用：收集完把 JSON POST 到這些端點（header `x-api-key`）；不設就不上傳 |

## CLI

```powershell
python crawler.py login                       # 開瀏覽器手動登入 Threads
python crawler.py crawl                       # 依 config.json 的 targets 收集過去 24 小時
python crawler.py crawl --hours 48 --headless
```

## 注意事項

- 收集在主機執行，預設會開瀏覽器視窗自動瀏覽，請勿關閉。
- 只能收集**公開**帳號。
- 同一個 `browser_profile/` 一次只能有一個程式使用：網頁服務正在收集時，不要同時跑 CLI。
- 讓同仁連線時（`host` 設 `0.0.0.0`），Windows 防火牆跳出詢問請選「允許」。
- 收集結果突然變少、日誌出現「Threads 未登入」→ session 過期了，重新登入即可。
- 所有目標都失敗時會顯示錯誤而不產生空檔，通常是網路問題或 Threads 改版。
- **`browser_profile/` 存有登入 session（等同登入憑證），把專案交給別人或打包時務必排除。**
  `config.json`、`output/`、`logs/` 是個人設定與收集結果，也不要一起給（都已列入 .gitignore）。

原理：Playwright 操作系統 Chrome 瀏覽 Threads，攔截內部 GraphQL 回應抽取貼文。
請遵守 Threads 服務條款與個資規範，資料僅供研究分析使用。
