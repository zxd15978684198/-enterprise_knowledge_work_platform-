# WeKnora 参考源码基线

核验日期：2026-10-27（Asia/Shanghai）。仅记录本地实际状态，不声称已同步远端最新版本或完成源码审计。

## 已核验基线

| 项目 | 本地证据 |
| --- | --- |
| 官方 origin（fetch/push） | https://github.com/Tencent/WeKnora |
| 目录 | `references/WeKnora`，已有独立 `.git` 及源码，本轮无需克隆 |
| 分支 | `main` |
| 完整 commit SHA | `662abaf9f944e37689d04c42160034b2cdec6829` |
| commit 日期 | 2026-10-26 23:43:30 +0800 |
| commit 标题 | `fix(feishu): checkpoint sync rounds that skip every node (#4736)` |
| Tree SHA | `67780d3c5e67eddadbd7d6e830f48a6b8b7afe21`，与 commit 对象区分 |
| `git describe --tags --always` | `v0.8.3-202-g662abaf9f` |
| `VERSION` | `0.8.3`；不表示当前 commit 等于 v0.8.3 标签 |
| 工作树 / 历史 | 干净；非 shallow repository |
| 父仓库 gitlink | 已记录相同 commit，mode `160000`，本轮保持不变 |

PRD 的 v0.8.3 是需求参考版本；实际本地研究基线为上表 commit。本轮不切换分支、不拉取更新、不改动参考源码。此前调研报告的 `97d7f0ad7b2b9fbd8bd717d737b9e79be6ace4c9` 明确为 tree SHA，不能认定其结论已对应本轮 commit；后续审计需重新核验。

## 许可证与边界

已读取本地 `LICENSE` 与 `THIRD_PARTY_NOTICES.md`：WeKnora 自有代码声明 MIT，第三方组件保留各自许可证。复制或分发适用代码时需保留版权和许可声明；不是所有组件都可以只标 MIT。

- `LICENSE` 记录 Apache-2.0 的 OpenCC 及 MIT 的 anydoc/Wails 等。
- `THIRD_PARTY_NOTICES.md` 补充 MySQL Driver 与 go-m1cpu 的 MPL-2.0、OpenCC 字典的 Apache-2.0、构建工具 cbindgen 的条件说明。
- DocReader OCR 组件涉及 RapidOCR/模型、OpenCV 的 Apache-2.0、ONNX Runtime 的 MIT、NumPy 的 BSD-3-Clause，并有额外第三方通知。
- `licenses/` 存放对应文本与模型许可说明；通知指出部分对应源码归档在打包时下载，并非已全部随 Git 保存。离线打包需提前准备对应归档。

这只是初始化许可清单，并非完整依赖审计或再分发批准；BrowserSkill、DocReader 实际复用及模型产物需后续逐项审查。PyKnora 自身尚未设置根许可证，本轮不擅自指定。

固定版本证据：[LICENSE](https://github.com/Tencent/WeKnora/blob/662abaf9f944e37689d04c42160034b2cdec6829/LICENSE)、[THIRD_PARTY_NOTICES.md](https://github.com/Tencent/WeKnora/blob/662abaf9f944e37689d04c42160034b2cdec6829/THIRD_PARTY_NOTICES.md)。上述结论来自本地文件读取。

## 获取与复核

原父仓库已有 gitlink，但缺少 `.gitmodules`，且 `.gitignore` 忽略 `references/**`。本轮仅补充 `.gitmodules` 官方 URL，不改变已有 gitlink 或忽略规则。Gitlink 固定 commit；分支名仅为核验时的本地状态，新克隆默认可能 detached HEAD。

新克隆 PyKnora 后，在有网络环境执行：

```powershell
git submodule update --init -- references/WeKnora
git -C references/WeKnora rev-parse HEAD
git -C references/WeKnora status --short
```

维护者可复核：

```powershell
git ls-files --stage references/WeKnora
git -C references/WeKnora remote -v
git -C references/WeKnora branch --show-current
git -C references/WeKnora show -s --format=fuller HEAD
git -C references/WeKnora describe --tags --always
git -C references/WeKnora rev-parse --is-shallow-repository
```

本轮未测试从零联网克隆；没有源码获取阻塞。离线恢复可由联网机器导出包含该 commit 的 `git bundle`，传输后在空目标目录克隆并检出固定 SHA，再核对父仓库 gitlink；或提供含许可证、通知和该 commit 证明的源码归档（无 `.git` 的归档不能冒充可独立验证的 Git 克隆）。已有非空目录不得覆盖。
