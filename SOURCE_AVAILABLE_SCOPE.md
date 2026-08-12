# 源码公开范围

PartLoom AI Platform 从 `v0.1.0-beta.2` 起采用
[PolyForm Noncommercial License 1.0.0](LICENSE)。源码可以查看、审计，并可在
许可证允许的非商业目的下使用、修改和分发；这不是 OSI 批准的开源许可证。

## 仓库包含

- CAD-IR、确定性校验、Planner、Skill Registry 和 Pipeline；
- Feature Reference Registry、Geometry Resolver 和恢复/报告机制；
- SolidWorks、AutoCAD、PDF2CAD 与 File2CAD 连接器源码；
- GUI、localhost Gateway、实验性 Add-in 源码；
- 合成 CAD-IR 示例、测试、打包与安装器源码。

## 仓库不包含

- 客户或供应商 PDF、DWG、DXF、SLDPRT、SLDASM、SLDDRW、STEP 和实测数据；
- 运行日志、生成模型、截图、缓存和个人绝对路径；
- API Key、Token、许可证文件和账号信息；
- SolidWorks、AutoCAD、MinerU 权重、Interop DLL 等第三方专有内容；
- 未确认再分发权的标准件库、模型库和厂商资源。

## 使用边界

允许与禁止的典型场景见 [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md)。任何商业
使用都必须在使用前取得单独书面授权。历史 MIT 版本的权利见
[LICENSE_HISTORY.md](LICENSE_HISTORY.md)。

公开 CI 只使用合成数据验证确定性逻辑。真实 CAD 验收应在隔离的、具备合法许可证的
Windows CAD 环境进行，并且不得把客户资产提交到本仓库。
