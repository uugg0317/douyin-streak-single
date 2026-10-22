# 第三方来源与许可说明（草案）

核对日期：2026-10-18。对应整改任务书 §5 T15。

**状态：来源与许可核对草案；主项目许可证及使用范围待用户决定。** 本文件记录当前可验证的第三方事实，不为整个项目授予新许可证，也不表示已经完成商业使用或再分发授权核验。不得把个别依赖的 MIT 许可写成“本项目全部采用 MIT”。原有第三方许可、版权声明和受保护目录保持原样。

## 已随源码携带的模块与前端文件

| 名称 / 当前文件标识 | 位置与用途 | 实际来源证据 | 许可核对结果与未决事项 |
|---|---|---|---|
| SparkFlow 登录组件 | `core/sparkflow_login/`；桌面扫码、身份识别、Cookie 解析及外围适配 | `__init__.py` 声明直接移植；`manifest.json` 的 `source_kind` 为 `existing local 8003 source files`，记录源文件名、修改说明、文件 / 函数 SHA-256；README 记录来自本机 8003 项目 | 已随附 [PolyForm Noncommercial License 1.0.0 全文](core/sparkflow_login/LICENSE)，原样保留。manifest 未给出公开来源 URL、上游提交与许可授予者身份；这些信息和其他 Required Notice 是否齐全仍待核验。不能仅凭本地来源哈希推定上游授权链完整；不推定覆盖整个主项目。 |
| douyin-cloud-streak 参考逻辑 | `core/automation.py` 模块开头；私信发送流程的参考来源 | 源码注释仅写“发送逻辑参考 douyin-cloud-streak（MIT）” | **MIT 是现存注释的声明，尚未独立确认**。未提供明确仓库 URL、来源版本、版权人及许可正文；待补齐。没有把网上同名项目认定为来源，也没有伪造版权归属。 |
| Axios v1.19.0 | `static/vendor/axios.min.js`；面板 HTTP 请求，由 `static/index.html` 引用 | 文件头含版本及 `Copyright (c) 2026 Matt Zabriskie and contributors` | 已核对 [Axios v1.19.0 官方许可](https://raw.githubusercontent.com/axios/axios/v1.19.0/LICENSE)，MIT；许可全文转录在下方链接文档。尚未将当前压缩文件与官方发行文件做逐字节比对，不能据此证明下载来源。 |
| Vue v3.5.41 | `static/vendor/vue.global.prod.js`；面板响应式界面，由 `static/index.html` 引用 | 文件头含版本、Yuxi (Evan) You and Vue contributors、`@license MIT` | 已核对 [Vue v3.5.41 官方许可](https://raw.githubusercontent.com/vuejs/core/v3.5.41/LICENSE)，MIT；保留文件头，全文见下方链接文档。当前压缩文件来源完整性尚未逐字节核验。 |
| Element Plus v2.14.4 | `static/vendor/element-plus.full.min.js` 与 `static/vendor/element-plus.css`；面板组件和样式，由 `static/index.html` 引用 | JS 文件头含版本；CSS 无独立版本 / 版权头，二者在页面中配套加载 | 已核对 [Element Plus 2.14.4 官方许可](https://raw.githubusercontent.com/element-plus/element-plus/2.14.4/LICENSE)，MIT；全文见下方链接文档。CSS 与该版本的对应关系和当前文件来源完整性待核验。 |
| Three.js r160 | `static/vendor/three.min.js`；保留的三维渲染库文件，本次未在非 vendor 源文件中找到加载或调用 | 文件内 `REVISION` 为 160，版权头为 Three.js Authors，SPDX 标注 MIT | 已核对 [Three.js r160 官方许可](https://raw.githubusercontent.com/mrdoob/three.js/r160/LICENSE)，MIT；全文见下方链接文档。未擅自删除该保留文件；来源完整性尚未逐字节核验。 |
| regenerator-runtime | 内嵌于 `static/vendor/axios.min.js`；打包运行时辅助代码 | 文件内保留 `Facebook, Inc.`、MIT 及 [Babel 许可链接](https://github.com/babel/babel/blob/main/packages/babel-helpers/LICENSE) | 当前证据为内嵌声明；对应版本 / 提交、完整版权与适用许可正文待核验。上游 `main` 链接不等于已固定的历史版本。 |
| @vue/shared v3.5.25 | 内嵌于 `static/vendor/element-plus.full.min.js`；组件库共享工具代码 | 内嵌文件头含版本、Vue 版权人与 MIT | 已核对 [Vue v3.5.25 官方许可](https://raw.githubusercontent.com/vuejs/core/v3.5.25/LICENSE)，MIT；全文见下方链接文档。内嵌版本与独立 Vue 运行时版本不同，分别登记。 |
| Modernizr 3.0.0pre (Custom Build) 参考片段 | 内嵌于 `static/vendor/element-plus.full.min.js`；事件支持检测 | 代码注释含 `Borrows from Modernizr` 与 `@license Modernizr 3.0.0pre (Custom Build) \| MIT` | 当前证据为内嵌声明；具体定制构建来源、版权人与适用许可正文待核验。未把 Element Plus 自身许可当作该片段来源已经核验的证据。 |

已核对的前端许可原文及 Python 依赖明细见 [第三方使用范围与许可核对草案](docs/third-party-usage-draft.md)。原文件中的版权与许可注释必须保留；本文不能替代第三方许可正文。

## 使用范围与决定边界

- 登录模块附有 PolyForm Noncommercial 1.0.0，其允许目的与分发条件以[随附原文](core/sparkflow_login/LICENSE)为准。本文没有授予该模块商业用途许可；“依赖中存在 MIT”不意味着整包可任意商用或再许可。
- 主项目尚未在本轮选择许可证。保留当前源码供本机检查、修复和合成测试，不将本文解释为开放源代码授权或对外商业交付批准。
- 对外分发前，补齐登录组件的原始授予者 / 来源及 Required Notice 核对，补齐发送参考逻辑的可追溯来源与许可正文，并核验内嵌代码声明及最终打包内容。当前草案未声称这些未决项已经解决。
- R18 移除 `playwright-stealth` 的方向已经确定；不因第三方说明保留或重新引入该依赖。最终依赖集合以整改后的 `requirements.lock` 为准。
- 本轮没有更改任何 `LICENSE` 文件、版权归属或 `core/sparkflow_login/` 内容，也没有为解决许可问题而重写或替换模块。主项目许可证和使用范围待用户决定；不自动扩大任务范围。

## 证据范围

本次仅读取白名单源码、依赖清单、vendor 注释与随附许可，并访问官方项目许可文件和 PyPI 指定版本元数据。没有读取用户 `.env`、真实 `data/`、日志或浏览器资料。没有运行账号服务、连接抖音、登录、发送消息或部署。

本轮可追溯核对记录保存在仓库 `dist/evidence/T15/`，该目录为本机整改证据，不递归收入源码发布包。外部许可文档的核对能证明该官方版本公布的文本，不能单独证明当前 vendor 文件的下载来源，也不能替代缺失的原始移植来源。
