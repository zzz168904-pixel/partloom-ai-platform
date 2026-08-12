# 参与贡献

感谢改进 PartLoom AI Platform。项目接受文档、测试、CAD-IR、连接器和生产执行器方面的
贡献，但所有改动都必须保留确定性执行边界：

```text
input -> candidate plan -> CAD-IR -> validator -> confirmation
      -> Pipeline -> CAD connector -> geometry verification -> save gate
```

## 开始之前

1. 先阅读 [README.md](README.md)、[CAPABILITIES.md](CAPABILITIES.md) 和
   [CAD-IR 指南](docs/CAD_IR_GUIDE.md)；
2. 搜索现有 Issue，避免重复实现；
3. 较大的 operation 或架构变更先提交设计 Issue；
4. 只使用你有权公开的合成数据和代码。

## 贡献授权状态

PartLoom 采用非商业源码可见许可，并保留提供单独商业授权的能力。为了避免外部
贡献的版权状态阻断后续商业授权，在正式 Contributor License Agreement（CLA）
发布前：

- 欢迎通过 Issue 提交缺陷报告、设计建议和最小合成复现；
- 外部 Pull Request 可以用于讨论，但不会在未签署单独贡献协议前合并；
- 提交 Issue 或勾选 PR 模板不构成版权转让或商业再授权；
- 维护者确认贡献协议后，才会把该 PR 标记为可合并。

详见 [CONTRIBUTOR_POLICY.md](CONTRIBUTOR_POLICY.md)。

## 隐私与许可证

Pull Request 不得包含：

- 客户 PDF、DWG、DXF、SLDPRT、SLDASM、SLDDRW 或 STEP；
- 微信、桌面、网盘等个人绝对路径；
- API Key、Token、Cookie、许可证文件或机器标识；
- 日志、截图、生成模型、OCR 权重或厂商二进制文件；
- 没有再分发授权的模板、标准件库或第三方源码。

测试必须使用合成 CAD-IR 和合成几何。提交 PR 表示你有权提供该内容，但不会替代
项目要求的单独贡献协议。

## 开发环境

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

提交前运行：

```powershell
.\.venv\Scripts\python.exe scripts\release_audit.py
.\.venv\Scripts\python.exe -m pytest
```

## 新增或修改 CAD operation

不要只添加一个函数入口。完整 operation 至少需要：

1. canonical operation 名称和兼容别名；
2. 参数 Schema、单位、必填组、冲突和错误返回；
3. Skill Registry 中的 capability、side effects 和 output types；
4. Planner 的阶段、依赖和 `allowed_skills` 路由；
5. `prepare/execute/verify/export/cleanup` 生命周期；
6. Body/Feature/Face/Edge/Axis 引用的产生和消费声明；
7. 执行前几何预检和执行后结果验证；
8. 合成单元测试与至少一个组合回归；
9. 本机 CAD 保存、关闭、重开验收记录；
10. [CAPABILITIES.md](CAPABILITIES.md) 状态更新。

`execute` 没有抛异常不代表建模成功。验证应尽可能检查 Feature Tree、Body 数量、体积、
重建错误和预期尺寸。

## Pull Request 要求

- 改动范围小而明确，不混入无关重构；
- 不绕过 CAD-IR、Planner Validator、阶段门禁或保存门禁；
- 缺少尺寸或目标几何有歧义时必须 fail closed；
- CAD API 修改只发生在 Pipeline 生命周期内部；
- 正式入口不得调用 demo、fixture、acceptance 或测试模型生成器；
- 描述已运行的测试，以及哪些真实 CAD 验收无法在当前环境完成；
- 不得声称厂商认证、任意零件生成或未经验证的生产能力。

CI 通过后仍可能需要维护者在隔离的本机 CAD 环境做人工验收。

## 报告问题

请提供脱敏后的：

- Windows、Python、SolidWorks/AutoCAD 版本；
- canonical CAD-IR 或最小合成复现；
- `pipeline_report.json` 中的失败步骤和错误类别；
- 期望与实际行为。

不要上传原始客户文件或完整个人日志。安全问题请遵循 [SECURITY.md](SECURITY.md)。
