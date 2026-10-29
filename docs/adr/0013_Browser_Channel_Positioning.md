# ADR-0013：双浏览器通道的产品定位（BrowserSkill / Playwright）

- 日期：2026-10-29
- 状态：**提议**（产品定位需产品裁决；工程侧只固定不放宽判定口径）
- 依据：`Phase0_Full_Gate_Review.md` §3（PoC-05 `PARTIAL`）、§6.1 ADR-NEW-7、§6.3 DG-5；`docs/poc/evidence/poc05-report.md`；`wave-w5-record.md`（T17 真配对）；`05_Phase0_Acceptance.md:80`（PoC-05 要求双端真实证据）

## 背景

P0 的浏览器层设计了双后端：BrowserSkill（真扩展 + Rust daemon `bsk`，T17 已跑通真配对）与 Playwright。实际情况是：**只有 BrowserSkill 一侧有真实证据**——`bsk.exe` 已构建、扩展已加载、端到端配对通过；Playwright 未安装、未跑真实 read。能力矩阵覆盖率也只是 31 个动作映射 13 个（未映射项按 `NOT_RUN` 处理）。

`05:80` 的 G-FULL 明确要求「PoC-05 真扩展/daemon **+** Playwright 真实证据与安全能力矩阵齐全」。因此当前不能把「单后端真实」写成 PoC-05 通过，这一条是 G-FULL 不成立的直接原因之一。T32 把它列为必须由产品定性的范围问题：Playwright 到底是 P0 必需，还是 P1 可选。

## 决策提议

| 选项 | 内容 | 后果 | 适用条件 |
| --- | --- | --- | --- |
| A | **Playwright 降为 P1 可选**，P0 只承诺 BrowserSkill 单后端；DG-5 生效 | 需把 `05:80` 的双端要求走正式范围变更（写进受限准入）；发布文档标注「单后端、禁真实登录/外部写」 | 首发浏览器能力仅用于受限站点采集（推荐） |
| B | **Playwright 为 P0 必需**，补装 + 真实 read + 安全矩阵补齐 | G-FULL 才能推进；需新增环境依赖（浏览器与驱动）、补跑 PoC-05、重评 T16 的 SSRF/网络策略 | 首发即要求跨浏览器兼容 |

无论选哪个，工程侧固定三条不变量：

1. **端口契约一致**：BrowserSkill 与 Playwright 都必须实现 T15 定义的 Browser port（同一组动作语义），不得各自长出一套 API；
2. **判定文案禁止冒充**：`NOT_RUN` 的能力不得写裸 `PASS`，单后端状态必须在结论里标注「单后端」；
3. **安全边界不因后端切换放宽**：域名/网络/速率/资源预算与每跳 SSRF 校验对两个后端同样生效；真实登录与验证码始终交用户处理。

## 替代方案

1. 只保留 BrowserSkill，删除 Playwright 相关代码与配置：会丢失「可替换后端」的架构价值，且与 AGENTS.md「保留双后端方向」冲突，拒绝。
2. 用 Playwright 替代 BrowserSkill：BrowserSkill 是唯一已拿到真扩展/daemon 证据的路径，且承担用户真实浏览器会话的定位，拒绝。
3. 两个后端都装但只测一个，文档写「双后端已支持」：属于 `05:80` 明令禁止的冒充，拒绝。

## 后果与验证条件

后果：选 A 会把「跨浏览器」推迟到 P1，并把 DG-5 写进受限准入的禁用能力清单；选 B 会增加一条环境依赖与一轮 PoC 重跑。验证条件：(1) PoC-05 判定文案与所选选项一致，单后端时不得写双后端 PASS；(2) 两个后端对同一 port 契约的适配测试存在（当前只有 BrowserSkill 侧）；(3) 安全矩阵对两个后端给出同一组拒绝证据；(4) 若选 B，Playwright 真实 read 必须有可复现的脚本与输出。

实现契约：[ADR-0004](0004_Parser_Browser_Reuse.md)（复用方向）、[05](../tdd/05_Browser_Adapter.md) 浏览器安全、`docs/poc/evidence/Phase0_Full_Gate_Review.md` §3/§7。
