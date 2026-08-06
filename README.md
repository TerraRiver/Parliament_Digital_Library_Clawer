# Parliament Digital Library 爬虫

用 Selenium 爬取 [印度议会数字图书馆](https://elibrary.sansad.in) 搜索结果：
翻页 → 逐条打开详情页（等价于点 **View**）→ 抓元数据 → 点 **Download** 下载附件。
全程断点续跑，进度写在 `data/state.json`。

## 快速开始

```bash
uv sync

# 先小跑一下看看（只抓元数据，不下载文件）
uv run pdl-crawl --max-items 20 --no-download

# 正式跑
uv run pdl-crawl
```

Chrome 和 chromedriver 由 Selenium Manager 首次运行时自动下载，不用手动装。
**如果网络访问不了 `googlechromelabs.github.io`（国内常见），见下面「chromedriver 版本对不上」。**

## chromedriver 版本对不上

报错长这样：

```
There was an error managing chromedriver (error sending request for url (https://googlechromelabs.github.io/...))
using driver found in the cache
SessionNotCreatedException: This version of ChromeDriver only supports Chrome version 135
Current browser version is 150.0.7871.187
```

Selenium Manager 联网失败，退回用了缓存里的旧 chromedriver，跟本机 Chrome 版本不匹配。
手动指定一个匹配的 driver 即可：

```powershell
# 1. 看本机 Chrome 版本：chrome://version，比如 150.0.7871.187
# 2. 从镜像下同大版本的 chromedriver-win64.zip 并解压
#    https://registry.npmmirror.com/binary.html?path=chrome-for-testing/
# 3. 指定路径跑
uv run pdl-crawl --driver-path "C:\tools\chromedriver.exe"

# 或者设一次环境变量，之后照常跑
$env:PDL_CHROMEDRIVER = "C:\tools\chromedriver.exe"
uv run pdl-crawl
```

Chrome 装在非默认位置的话，再加 `--chrome-binary`（或环境变量 `PDL_CHROME_BINARY`）。

另一条路是让 Selenium Manager 能联网（挂代理），并清掉旧缓存后重试：
`rmdir /s /q "%USERPROFILE%\.cache\selenium"`（Linux 是 `rm -rf ~/.cache/selenium`）。

## 断点续跑

直接重跑同一条命令即可，已完成的条目会跳过：

```bash
uv run pdl-crawl                    # 从上次断的地方继续
uv run pdl-crawl --retry-failed     # 顺带重试之前失败的条目
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

元数据始终是 Selenium 从渲染后的 DOM 抓的，两种模式没有区别。批量跑建议用 `api`。

## 常用参数

```
--search-url URL      搜索结果页 URL（默认是 query=china 那一条）
--data-dir DIR        产物目录（默认 data）
--page-size N         每页条数，走 spc.rpp（默认 100）
--start-page N        从第几页开始（默认 1）
--max-pages N         本次最多翻几页
--max-items N         本次最多处理几条
--no-download         只抓元数据
--delay SEC           每条之间的间隔（默认 1.0，带 30% 抖动）
--retry-failed        重试标记为 failed 的条目
--restart-every N     每 N 条重启一次浏览器释放内存（默认 200）
--headed              显示浏览器窗口，调试用
--driver-path PATH    手动指定 chromedriver（等价环境变量 PDL_CHROMEDRIVER）
--chrome-binary PATH  手动指定 Chrome 可执行文件（等价环境变量 PDL_CHROME_BINARY）
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
| [crawler/pages.py](crawler/pages.py)       | 列表页 / 详情页解析（用`execute_script` 一次取回整块数据） |
| [crawler/download.py](crawler/download.py) | click 与 api 两种下载实现                                    |
| [crawler/state.py](crawler/state.py)       | `state.json` 的原子读写与状态机                            |
| [crawler/browser.py](crawler/browser.py)   | Chrome 驱动创建、隐私弹窗处理                                |
