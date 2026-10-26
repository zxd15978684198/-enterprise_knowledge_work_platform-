# ADR-0004：DocReader 服务复用与浏览器双适配

- 日期：2026-10-27
- 状态：**提议**
- 依据：PRD F-04/11、AC-01、AC-E02；审计 C01/C06、B05/B07/B11

## 背景

Python DocReader 不覆盖原 Go simple reader 全部格式，RPC 不负责图片授权/存储/分块/引用。BrowserSkill 接入网关、app 侧 daemon、用户扩展是不同部署单元，固定构建输入不能证明真实互通。

## 决策提议

优先独立容器复用固定 DocReader proto/镜像，TXT/MD 用简单适配；HTML 受控先抓 bytes，最终抽取器经 golden set 选择。图片与定位变换在摄取服务负责。P1 保留 BrowserSkillAdapter 与 PlaywrightAdapter；验证期推荐受控独立 Go bridge 接 pinned daemon/extension，Python 直连作为可逆替代，真实 H7 比较后再锁路线。Windows 用户扩展支持和原生 Windows daemon 支持分开记录。

## 替代方案

全量重写 parser 成本高；直接内嵌原包会带 OCR/native/浏览器依赖；只留 Playwright 无法访问已授权用户会话；直接 Python 接管协议运维简单但扩大同时重写安全面的风险。

## 后果与验证条件

H4 五格式/图/定位/断流真实 golden set；H7 成套版本真扩展、owner crash、多实例 epoch、超时未知动作、不自动重放。SBOM、权重/第三方许可、bridge 提取范围、原生 Windows/用户网络控制能力尚待核验，未通过则禁用相应能力。Mock 不可作为协议通过证据。

契约：[05](../tdd/05_Browser_Adapter.md)、[06](../tdd/06_Knowledge_Ingestion.md)。替代关系：无。
