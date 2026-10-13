# 桌面版 2.6（Windows）构建与使用说明

将单账号控制台封装为双击即用的 Windows 本机托盘程序，面板直接打开即可使用。

## 一、产物形态

```
desktop\_build\dist\DouyinStreak\
├── DouyinStreak.exe        ← 双击这个
├── data\                   ← 你的数据（登录态/台账/配置/日志）
├── .env.example            ← 可选配置模板（不含真实凭据）
├── _internal\
│   ├── static\             ← 管理后台前端
│   └── playwright-browsers\← 随包分发的 Chromium 内核
└── ...（Python 运行时与依赖）
```

**分发时必须整个目录一起拷贝，不能只拷 exe。**
体积约 400~700MB，其中绝大部分是 Chromium。

构建过程不会把项目根目录的 `.env`、`data/state.json` 或台账打入产物。首次启动
只监听 `127.0.0.1`，打开地址直接进入面板；后台任务沿用既有开关与配置。
如需 SMTP 等可选设置，可把 `.env.example` 复制为 exe 同级 `.env` 后编辑；
不填写邮件设置也可以使用面板。后端保持回环监听；如需域名访问，需自行配置同机反代，
并在 `.env` 设置匹配地址栏来源的 `QR_PUBLIC_ORIGIN`，例如 `https://example.com`。
Nginx 的上游端口需与实际启动端口一致。此版本没有管理员登录门，使用者需自行限制后台访问范围。

## 二、为什么必须是 `--onedir`

`--onefile` 会把程序自身解压到临时目录再运行，导致两个问题：

1. Chromium 的相对路径失效，Playwright 报 `Executable doesn't exist`；
2. 每次启动都要解压几百 MB，启动要等很久。

所以用 `--onedir`：一个文件夹 + 里面的 exe。想要"单个绿色 exe"就只能改成
首次启动联网下载 Chromium，离线环境不可用。

## 三、怎么构建

在项目根目录执行：

```
python desktop\build.py
```

脚本会依次做四件事：

1. **建独立虚拟环境** `desktop\_build\.venv` 并切换进去 —— 用全局环境打包会把
   无关的包一起扫进去，产物体积暴涨；
2. 安装 `requirements.lock` + 桌面版额外依赖（`pystray` / `pillow` / `psutil` / `pyinstaller`）；
3. **确保 Chromium 存在** —— 缺失就自动 `playwright install chromium`，
   然后复制到暂存区准备随包分发；
4. 调用 PyInstaller 打包并报告产物体积。

首次构建约 5~15 分钟，主要花在下载 Chromium 和安装依赖上。

## 四、怎么运行

双击 `DouyinStreak.exe`：

- 托盘出现图标（优先用 `static/fire-icon.png`，读不到就画一个火焰色圆点）；
- 后台服务起在 `127.0.0.1:8000`（被占用会自动顺延到 8001~8009）；
- 自动打开浏览器进入本机面板，无需管理登录。
- 后台任务沿用既有开关与配置；项目根目录的本机 BAT 才会默认设置 `SPARKKEEPER_BACKGROUND_TASKS=0`。

**托盘菜单**：

| 菜单项 | 作用 |
|---|---|
| 打开管理后台（双击图标同效） | 打开 `http://127.0.0.1:8000/` |
| 提取登录凭证（扫码） | 打开后台，用「凭证」页扫码登录 |
| 重启服务 | 停掉再拉起 uvicorn（改完配置不想重启程序时用） |
| 退出 | 停服务 + 清理本程序拉起的 Chromium |

## 五、路径处理的三个坑（已在代码里处理）

这几个问题不处理会导致"打包成功但一运行就报错"，记录备查：

1. **`BASE_DIR` 会漂到 `_internal`**
   `core/config.py` 的 `BASE_DIR = Path(__file__).parent.parent`，打包后
   `core` 被收进 `_internal`，于是 `BASE_DIR` 指向 `_internal`，而不是 exe 旁边。
   所以只读的 `static/` 用 `--add-data` 放到 `_internal` 下；可编辑 `.env` 由
   `ENV_FILE_PATH` 固定到 exe 同级，不能随包带入构建机凭据。

2. **用户数据必须留在 exe 旁边**
   数据若跟着 `BASE_DIR` 进了 `_internal`，用户根本找不到自己的
   `state.json` / `ledger.json`。解决办法是运行时钩子
   `runtime_hook_data_dir.py` 设置 `DATA_DIR` 与 `ENV_FILE_PATH` 指向 exe 同级目录。
   为此给 `core/config.py` 加了对 `DATA_DIR` 的支持（该变量原本用于多账号隔离）。

3. **工作目录不能靠 `cwd`**
   双击 exe 时 `cwd` 可能是 `C:\Windows\System32`。`launcher.py` 启动时
   会 `os.chdir()` 锚定到 exe 所在目录。

## 六、调试

准备好桌面依赖后，可用 `desktop\_build\.venv\Scripts\python.exe desktop\launcher.py`
直接运行启动器，跳过重新打包。

## 七、已知限制

- **控制台窗口会保留**（`--console`）。确认稳定后可把 `desktop/build.py` 里的
  `--console` 改成 `--noconsole`，但那样出问题就看不到报错了。
- **桌面产物需要在目标 Windows 机器上验证**：Chromium 路径解析、托盘图标、
  杀毒软件是否拦截 PyInstaller 产物（误报较常见）。
- **托盘菜单的"提取登录凭证"目前只打开后台首页**，没有直接切到凭证页 ——
  管理后台是单页应用，tab 切换没有走 URL 路由，要直达得改前端。
