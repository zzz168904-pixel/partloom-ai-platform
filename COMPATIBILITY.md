# 兼容性与验收矩阵

本表区分“公共 CI 运行环境”和“维护者真实 CAD 验收基线”。未列版本可能兼容，
但在完成对应验收前不得声称受支持。

| 组件 | 当前基线 | 验收级别 |
|---|---|---|
| Windows | Windows 11 x64 | 本机 CAD 基线 |
| Python | 3.11、3.13 | GitHub CI |
| SolidWorks | 2025 | 本机 COM 验收；非厂商认证 |
| AutoCAD | 2025 | 本机 COM 验收；非厂商认证 |
| .NET Framework | 4.8 | TaskPane 实验构建 |
| Inno Setup | 6 | 可选安装包构建 |

## 兼容性规则

- Python、CAD 或 Windows 主版本变化必须作为独立兼容性变更记录；
- 公共 CI 不启动有许可证的桌面 CAD，不能替代真实保存、关闭和重开验收；
- SolidWorks/AutoCAD API、模板、语言和许可证差异可能导致同一 CAD-IR 行为不同；
- Issue 必须提供准确版本和最小合成复现，不接受客户文件作为公共测试夹具；
- “Local CAD required”只表示存在执行器源码，不表示所有参数组合已通过。
