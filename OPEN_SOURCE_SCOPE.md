# 完整开源范围

PartLoom AI Platform v0.1.0 Beta.1 采用 MIT License 公开平台核心源码。

## 已公开

- CAD-IR 编译器、标准 operation、别名、单位和依赖校验；
- Planner Provider、Planner Validator、Skill Planner 和 Skill Registry；
- Feature Reference Registry、持久化引用和 Geometry Resolver；
- Pipeline 生命周期、阶段门禁、输出守卫、检查点、恢复与报告；
- SolidWorks COM 建模执行器、工程图与导出适配代码；
- AutoCAD Automation、Annotation Engine、PDF2CAD 和 File2CAD；
- PySide6 桌面 GUI、localhost Agent Gateway、SolidWorks Add-in 源码；
- 合成 CAD-IR 示例、自动测试、打包脚本和安装器源码。

## 不进入公开仓库

- 客户或供应商提供的 PDF、DWG、DXF、SLDPRT、SLDASM、SLDDRW、STEP、
  图片和实测数据；
- 运行日志、生成模型、截图、报告、缓存和个人绝对路径；
- API Key、`.env.local`、访问令牌和账号信息；
- SolidWorks、AutoCAD、MinerU 模型权重、Interop DLL 等第三方专有内容；
- 未确认再分发权的标准件库、测试模型库和厂商资源。

这些排除项属于隐私、版权和许可证边界。公开仓库不依赖隐藏服务才能
读取 CAD-IR 或运行已支持的本地 CAD 执行链；但用户必须自行安装并授权
对应 CAD 软件，并自行提供可合法使用的测试数据。

## 验证原则

公开 CI 使用合成数据验证确定性逻辑。需要真实 SolidWorks/AutoCAD 或专有
参考模型的验收不会在公共云 Runner 中自动启动，而应由维护者在隔离的
Windows CAD 环境中执行并发布脱敏结果。
