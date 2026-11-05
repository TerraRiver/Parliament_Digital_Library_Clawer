# Parliament Digital Library 爬虫

用 Playwright 爬取 [印度议会数字图书馆](https://elibrary.sansad.in) 搜索结果：
翻页 → 逐条打开详情页（等价于点 **View**）→ 抓元数据 → 点 **Download** 下载附件。
全程断点续跑，进度写在 `data/state.json`。

## 快速开始

```bash
uv sync

# 安装 Playwright 浏览器内核（若本机已装有 Google Chrome 可跳过，程序会自动回退调用本机 Chrome）
uv run playwright install chromium

# 先小跑一下看看（只抓元数据，不下载文件）
uv run pdl-crawl --max-items 20 --no-download

# 正式跑
uv run pdl-crawl
```

## 浏览器配置

Playwright 直接通过 CDP 协议控制浏览器，**不需要 `chromedriver`**，彻底避免了浏览器与驱动版本不匹配的问题。

启动优先级如下：
1. 若指定了 `--chrome-binary`（或环境变量 `PDL_CHROME_BINARY`），直接使用该可执行文件；
2. 否则默认启动 `uv run playwright install chromium` 安装的自带 Chromium；
3. 若未安装自带 Chromium（例如国内网络下载不便），会自动回退尝试启动本机已安装的正式版 Google Chrome。

如果 Chrome 装在非默认位置，可显式指定路径：

```powershell
uv run pdl-crawl --chrome-binary "C:\Program Files\Google\Chrome\Application\chrome.exe"

# 或者设一次环境变量，之后照常跑
$env:PDL_CHROME_BINARY = "C:\Program Files\Google\Chrome\Application\chrome.exe"
uv run pdl-crawl
```

## 断点续跑

直接重跑同一条命令即可，已完成的条目会跳过：

```bash
uv run pdl-crawl                       # 从上次断的地方继续
uv run pdl-crawl --retry-failed        # 顺带重试之前失败的条目
uv run pdl-crawl --recheck-no-file     # 重新核实之前标记为"无附件"的条目
```

Ctrl+C 会在当前条目结束后干净退出并保存进度（连按两次强制退出）。
即使进程被 `kill -9`，`state.json` 也是原子写入的，最多丢掉最后几条的记录。

## 产物

```
data/
├── state.json          # 进度：翻过哪些页、每个条目的状态与落盘路径
├── metadata.jsonl      # 每行一个条目的完整元数据（追加写）
└── files/<uuid>/*.pdf  # 下载的附件，按条目 UUID 分目录避免重名
```

`state.json` 里每个条目的 `status`：

| 状态         | 含义                                          |
| ------------ | --------------------------------------------- |
| `pending`  | 列表页已发现，还没处理（或上次失败待重试）    |
| `metadata` | 元数据抓到了，文件还没下完                    |
| `done`     | 元数据 + 文件都完成                           |
| `no_file`  | 该条目没有可下载的附件                        |
| `failed`   | 重试次数用尽，需要`--retry-failed` 才会再碰 |

`metadata.jsonl` 是追加写的，重跑同一条目会追加新行；以 `uuid` + `scraped_at` 去重即可。

## 两种下载模式

| 模式                              | 说明                                                                                         |
| --------------------------------- | -------------------------------------------------------------------------------------------- |
| `--download-mode click`（默认） | 真的去点页面上的 Download 按钮，由 Chrome 下载，行为跟人工完全一致                           |
| `--download-mode api`           | 从详情页拿到条目 UUID 后走 DSpace REST 直链下载，**明显更快，且大文件支持 Range 续传** |

元数据始终是 Playwright 从渲染后的 DOM 抓的，两种模式没有区别。批量跑建议用 `api`。

下载过程会用 `tqdm` 显示实时进度条（速度、百分比、ETA）。api 模式下用 `--concurrency`
控制**同一条目内**多个文件的并发下载数（默认 3）——多数条目只有 1 个附件，并发数在这种条目上
不会体现出来，只有像 `AU1665.pdf` / `AU1665_hindi.pdf` 这种一个条目挂多个文件的情况才会用上。
这不是"同时处理多个条目"的并发，翻页/条目级别始终是顺序处理的。

## 常用参数

```
--search-url URL      搜索结果页 URL（默认是 query=china 那一条）
--data-dir DIR        产物目录（默认 data）
--page-size N         每页条数，走 spc.rpp（默认 100）
--start-page N        从第几页开始（默认 1）
--max-pages N         本次最多翻几页
--max-items N         本次最多处理几条
--no-download         只抓元数据
--concurrency N       api 模式下，同一条目内并发下载的文件数（默认 3；click 模式恒为顺序下载）
--delay SEC           每条之间的间隔（默认 1.0，带 30% 抖动）
--retry-failed        重试标记为 failed 的条目
--recheck-no-file     重新核实标记为 no_file 的条目（修复过去误判"无附件"的老数据用）
--restart-every N     每 N 条重启一次浏览器释放内存（默认 200）
--headed              显示浏览器窗口，调试用
--chrome-binary PATH  手动指定 Chrome/Chromium 可执行文件（等价环境变量 PDL_CHROME_BINARY）
```

完整列表见 `uv run pdl-crawl --help`。

换搜索词只要改 `--search-url`，并配一个新的 `--data-dir`：

```bash
uv run pdl-crawl --search-url "https://elibrary.sansad.in/search?query=tibet&..." --data-dir data/tibet
```

## 规模提醒

默认那条 china 查询有 **36,263 条结果**，单个 PDF 常见 30–80 MB，
全量下载体量在 **1–2 TB** 量级，串行跑需要以「周」计。建议：

- 先 `--no-download` 把 36k 条元数据抓全（几小时量级），再按元数据挑要下的文件；
- 或者用 `--max-pages` 分批跑，反正断点续跑不怕中断。

## 代码结构

| 文件                                      | 职责                                                         |
| ----------------------------------------- | ------------------------------------------------------------ |
| [crawler/run.py](crawler/run.py)           | 主流程、CLI、重试与浏览器重启策略                            |
| [crawler/pages.py](crawler/pages.py)       | 列表页 / 详情页解析（用`page.evaluate` 一次取回整块数据）    |
| [crawler/download.py](crawler/download.py) | click 与 api 两种下载实现                                    |
| [crawler/state.py](crawler/state.py)       | `state.json` 的原子读写与状态机                            |
| [crawler/browser.py](crawler/browser.py)   | Playwright 浏览器会话创建、隐私弹窗处理                      |
| [crawler/stealth.py](crawler/stealth.py)   | 浏览器指纹隐匿补丁、贝塞尔曲线鼠标轨迹与滚轮拟真操作         |
