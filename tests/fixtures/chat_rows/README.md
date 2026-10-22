# 联系人行夹具

本目录目前只有 **synthetic 合成样本**，不是来自真实账号或真实抖音 DOM。没有真实脱敏样本，不能据此声称平台选择器已验证。

`synthetic.html` 用于相邻行、火花/未读/预览/未知值隔离。`expected.json` 记录假名及预期契约。data属性是合成契约标记；已有 commonStreak 类名仅供回归，并无真实平台来源证明。头像为内嵌SVG，不访问网络。

后续真实样本须由用户先脱敏（名称、头像、ID、URL和嵌入内容），保留行与列表层级，并另写来源/日期/预期。不得把本目录合成文件改称真实证据。

真实样本入口为 `real/YYYY-MM-DD-编号/`，每份只含 `meta.json` 与 `row.html`。示例meta：

```json
{"source":"real","collected_at":"2026-10-18","row_type":"with_streak","expected":{"name":"合成甲","streak_status":"confirmed","streak_days":12}}
```

row_type可用with_streak、no_streak、unread、streak_unread、ending、list。list的expected用`{"rows":0}`记录外层结构的预期行数。名称和预览文本须以合成/匿名/测试/假名/示例或synthetic/fixture/placeholder/sample开头；头像改为`placeholder-avatar.svg`等本地占位文件名。不得含外部链接、HTTP头像、邮箱、手机号、长数字ID、cookie/token字样或可执行脚本。检查器采用保守格式规则，不能替代人工脱敏审查，也不能证明来源真实。

运行 `python tools/check_fixtures.py`。没有real目录时通过并报告“尚无真实样本”，platform_dom_verified继续为false；通过格式检查也不自动开启缺节点推断。`NONE_BY_ABSENCE_ENABLED`本轮保持False，U01 B等待用户提供真实脱敏样本并人工核对。
