# 第三方使用范围与许可核对草案

核对日期：2026-10-18。主项目许可证及使用范围仍待用户决定；本文不改变 [NOTICE.md](../NOTICE.md) 的待核验结论，也不修改任何第三方许可。

## Python 依赖

安装依据是仓库 `requirements.lock`；`requirements.txt` 只描述直接依赖。下表记录本轮核对时锁定版本对应的 PyPI 元数据；它们由安装器下载，不代表源码包内已经携带每个依赖的分发文件与许可。若将来分发 venv、独立可执行程序、Playwright 浏览器或其他二进制，应按实际包含的依赖 / 浏览器版本另行核对全部许可与第三方声明；本轮干净源码包不包含 venv。

“元数据”只表示指定版本的 `license_expression`、`license` 或许可证分类声明。除单独列出的原文核对外，不将元数据核对表述为已逐一审阅发行文件中的完整许可。库名称、版本、来源 URL、元数据原值与结果保存在 `dist/evidence/T15/pypi-license-metadata.json`。

| 依赖与版本 | 来源证据 / 许可声明 | 用途与本项目位置 |
|---|---|---|
| fastapi 0.128.8 | [指定版本元数据](https://pypi.org/pypi/fastapi/0.128.8/json)，MIT | HTTP 接口；`app.py`、`routers/`、受保护登录模块的既有依赖 |
| uvicorn 0.39.0 | [指定版本元数据](https://pypi.org/pypi/uvicorn/0.39.0/json)，BSD-3-Clause | ASGI 服务启动；`app.py` 及启动脚本 |
| playwright 1.60.0 | [指定版本元数据](https://pypi.org/pypi/playwright/1.60.0/json) 确认官方来源；另核对 [官方 v1.60.0 LICENSE](https://raw.githubusercontent.com/microsoft/playwright-python/v1.60.0/LICENSE)，Apache-2.0 | 浏览器 API；`core/browser.py`、`core/automation.py`、登录模块。浏览器及其捆绑组件不视为已由此单一许可涵盖。 |
| APScheduler 3.11.3 | [指定版本元数据](https://pypi.org/pypi/APScheduler/3.11.3/json)，MIT 分类 | 计划与补发调度；`core/scheduler.py` |
| python-multipart 0.0.20 | [指定版本元数据](https://pypi.org/pypi/python-multipart/0.0.20/json)，Apache 分类；另核对 [官方 0.0.20 LICENSE.txt](https://raw.githubusercontent.com/Kludex/python-multipart/0.0.20/LICENSE.txt)，Apache-2.0 声明 | FastAPI 的文件上传 / multipart 解析；`routers/credentials.py` |
| pydantic 2.13.5 | [指定版本元数据](https://pypi.org/pypi/pydantic/2.13.5/json)，MIT | 请求模型与校验；`models.py` |
| annotated-doc 0.0.5 | [指定版本元数据](https://pypi.org/pypi/annotated-doc/0.0.5/json)，MIT | 间接依赖；声明于 `requirements.lock` |
| annotated-types 0.7.0 | [指定版本元数据](https://pypi.org/pypi/annotated-types/0.7.0/json)，MIT 分类 | 间接依赖；声明于 `requirements.lock` |
| anyio 4.12.1 | [指定版本元数据](https://pypi.org/pypi/anyio/4.12.1/json)，MIT | 异步运行基础依赖；声明于 `requirements.lock` |
| click 8.1.8 | [指定版本元数据](https://pypi.org/pypi/click/8.1.8/json)，BSD 分类；另核对 [官方 8.1.8 LICENSE.txt](https://raw.githubusercontent.com/pallets/click/8.1.8/LICENSE.txt)，BSD-3-Clause 文本 | 命令行间接依赖；声明于 `requirements.lock` |
| eval_type_backport 0.4.0 | [指定版本元数据](https://pypi.org/pypi/eval_type_backport/0.4.0/json)，MIT 分类 | 类型兼容间接依赖；声明于 `requirements.lock` |
| exceptiongroup 1.3.1 | [指定版本元数据](https://pypi.org/pypi/exceptiongroup/1.3.1/json)，MIT 分类 | 异常组兼容依赖；声明于 `requirements.lock` |
| greenlet 3.3.0（Windows）/ 3.2.5（其他平台） | [3.3.0 元数据](https://pypi.org/pypi/greenlet/3.3.0/json) / [3.2.5 元数据](https://pypi.org/pypi/greenlet/3.2.5/json)，均声明 `MIT AND Python-2.0` | Playwright 相关执行依赖；`requirements.lock` 中按平台选择 |
| h11 0.16.0 | [指定版本元数据](https://pypi.org/pypi/h11/0.16.0/json)，MIT 分类 | HTTP 协议间接依赖；声明于 `requirements.lock` |
| idna 3.20 | [指定版本元数据](https://pypi.org/pypi/idna/3.20/json)，BSD-3-Clause | 网络名称处理间接依赖；声明于 `requirements.lock` |
| pydantic_core 2.46.5 | [指定版本元数据](https://pypi.org/pypi/pydantic_core/2.46.5/json)，MIT | Pydantic 校验核心；声明于 `requirements.lock` |
| pyee 13.0.1 | [指定版本元数据](https://pypi.org/pypi/pyee/13.0.1/json)，MIT 分类 | 事件分发间接依赖；声明于 `requirements.lock` |
| starlette 0.49.3 | [指定版本元数据](https://pypi.org/pypi/starlette/0.49.3/json)，BSD-3-Clause | ASGI / FastAPI 基础依赖；声明于 `requirements.lock` |
| typing-inspection 0.4.2 | [指定版本元数据](https://pypi.org/pypi/typing-inspection/0.4.2/json)，MIT | 类型检查间接依赖；声明于 `requirements.lock` |
| typing_extensions 4.16.0 | [指定版本元数据](https://pypi.org/pypi/typing_extensions/4.16.0/json)，PSF-2.0 | 类型扩展间接依赖；声明于 `requirements.lock` |
| tzlocal 5.3.1 | [指定版本元数据](https://pypi.org/pypi/tzlocal/5.3.1/json)，MIT 分类 | 调度器本地时区支持；声明于 `requirements.lock` |
| tzdata 2026.5（Windows） | [指定版本元数据](https://pypi.org/pypi/tzdata/2026.5/json)，`license` 声明 Apache-2.0 | Windows 时区数据传递依赖；R20 固定当前干净环境已安装版本 |
| colorama 0.4.6（Windows） | [指定版本元数据](https://pypi.org/pypi/colorama/0.4.6/json)，BSD License 分类，未指定 SPDX 表达式 | Windows 终端颜色传递依赖；R20 固定当前干净环境已安装版本 |

这两项新增锁定条目的元数据核对记录在 `dist/evidence/T15/added-transitive-license-metadata.json`；元数据声明不等于已经审阅发行文件的全部第三方许可。

`playwright-stealth 2.0.3` 出现在整改前清单，本轮历史核对元数据也保留在 dist 证据中；R18 要求移除它，不将它列为整改后依赖或重新加入安装。若后续锁定版本变化，应更新上表并保留新的核对证据。

## 未决来源与授权信息

1. `core/sparkflow_login/manifest.json` 能追踪本地移植，但没有上游公开 URL、提交或许可授予者身份。本轮保留随附 PolyForm Noncommercial 1.0.0 原文；不能从项目名称猜测来源，也不能将许可中的示例 Required Notice 当成真正版权人。
2. `core/automation.py` 的 douyin-cloud-streak 参考声明缺精确来源、版权与许可正文。未替用户为这个来源编造 URL 或补写作者名。
3. Axios 内嵌 regenerator-runtime、Element Plus 内嵌 Modernizr 片段的精确来源版本与许可正文待核验；当前注释全部保留。没有声称已穷尽扫描打包依赖图或所有未保留头注释的内嵌代码。
4. 当前 vendor 文件与官方发行文件尚未逐字节比对，Element Plus CSS 也没有独立版本声明；官方相应版本的许可核对不替代这一步。
5. 主项目许可证及使用范围仍待用户决定。本轮不生成根目录 LICENSE、不更改受保护目录、不将当前许可草案当作商用 / 再分发授权。

## 已核对的前端库许可原文

以下文本于 2026-10-18 从对应官方版本的 LICENSE 取得，作为已经随源码携带的前端文件的许可参考与声明副本。原文件头声明仍保留；当前文件与上游发布文件的对应关系及上述未决项仍须按 NOTICE 核验。文本转录不为主项目新增许可。


### Axios v1.19.0

来源：[官方版本许可](https://raw.githubusercontent.com/axios/axios/v1.19.0/LICENSE)。

```text
# Copyright (c) 2014-present Matt Zabriskie & Collaborators

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

### Vue v3.5.41

来源：[官方版本许可](https://raw.githubusercontent.com/vuejs/core/v3.5.41/LICENSE)。

```text
The MIT License (MIT)

Copyright (c) 2018-present, Yuxi (Evan) You and Vue contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

### @vue/shared v3.5.25

来源：[官方版本许可](https://raw.githubusercontent.com/vuejs/core/v3.5.25/LICENSE)。

```text
The MIT License (MIT)

Copyright (c) 2018-present, Yuxi (Evan) You and Vue contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

### Element Plus v2.14.4

来源：[官方版本许可](https://raw.githubusercontent.com/element-plus/element-plus/2.14.4/LICENSE)。

```text
MIT License

Copyright (c) 2020-PRESENT Element Plus

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### Three.js r160

来源：[官方版本许可](https://raw.githubusercontent.com/mrdoob/three.js/r160/LICENSE)。

```text
The MIT License

Copyright © 2010-2023 three.js authors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```
