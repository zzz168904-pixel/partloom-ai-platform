# PartLoom AI Platform

[![Public CI](https://github.com/zzz168904-pixel/partloom-ai-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/zzz168904-pixel/partloom-ai-platform/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/zzz168904-pixel/partloom-ai-platform?include_prereleases)](https://github.com/zzz168904-pixel/partloom-ai-platform/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11--3.13-blue.svg)](INSTALL.md)
[![Platform](https://img.shields.io/badge/Platform-Windows%2011-0078D4.svg)](INSTALL.md)

PartLoom AI Platform 是一套面向 Windows 桌面 CAD 的开源自动化平台。
它使用确定性的 CAD 中间语言 **CAD-IR** 连接任务规划、参数校验、Skill
路由、几何引用、分阶段执行、SolidWorks、AutoCAD、PDF2CAD、文件转换
和结果报告。

项目的重点不是让语言模型直接操作 CAD，而是让任何输入先变成可检查的
工程计划，再经过门禁进入本地 CAD 执行器：

```text
用户输入 / PDF / CAD 文件
  -> 候选设计计划
  -> 标准 CAD-IR
  -> 确定性 Validator
  -> 用户确认
  -> Skill Planner
  -> Pipeline
  -> SolidWorks / AutoCAD / PDF2CAD 连接器
  -> 几何和文件校验
  -> 保存门禁与报告
```

> **Beta 可靠性边界**：公开 CI 验证 CAD-IR、规划门禁、Registry、
> Geometry Resolver、Pipeline、Gateway 和连接器逻辑，但云端 CI 不会启动
> 有许可证的桌面 CAD。真实 SLDPRT、SLDDRW、DWG 和 PDF 交付仍必须在本机
> SolidWorks/AutoCAD 环境验收。未支持、信息不完整或目标几何有歧义的任务
> 应在修改 CAD 文档前失败关闭，不能被包装成成功。

## 项目入口

| 入口 | 适合用户 | 当前用法 |
|---|---|---|
| PartLoom 桌面 GUI | 希望预览计划、确认阶段、查看日志和输出的机械工程师 | 从源码运行 `app.py` |
| CAD-IR 直接输入 | 需要确定性演示、测试或集成的开发者 | 加载 `examples/cad_ir/*.json` |
| Agent Gateway | 需要通过 localhost HTTP 集成 TaskPane 或其他客户端 | `python -m cad_agent.gateway` |
| SolidWorks TaskPane | 希望把入口嵌入 SolidWorks 的实验用户 | 编译 `src/SolidWorksCadAgentAddin` |
| Codex Skill | 希望让 Codex 直接调用建模脚本的用户 | 使用独立仓库 [partloom-solidworks-modeling](https://github.com/zzz168904-pixel/partloom-solidworks-modeling) |

当前完整平台仓库没有宣称提供 MCP Server。GUI、Gateway 和独立 Codex
Skill 是三个不同入口，但它们应遵守同一套 CAD-IR 与执行门禁。

## 当前公开能力

### 确定性平台核心

- CAD-IR operation 标准化、别名映射、mm/deg 单位统一和参数冲突检查；
- Feature 依赖排序、阶段门禁、`allowed_skills` 和 `expected_outputs` 守卫；
- Planner Provider 适配器与 Planner Validator，模型输出只能作为候选计划；
- Skill Registry、Feature Reference Registry 和持久化引用接口；
- Geometry Resolver 与局部 O/U/V/N 坐标系数据结构；
- `prepare -> execute -> verify -> export -> cleanup` Pipeline 生命周期；
- CAD 执行锁、检查点、失败恢复、保存门禁和 `pipeline_report.json`；
- PySide6 桌面 GUI、实时日志、计划确认和阶段化执行；
- 仅监听 localhost 的 Agent Gateway。

### SolidWorks 连接器

仓库包含 Python COM 生产执行器源码，覆盖有明确参数和受支持几何上下文
的以下特征族：

- 基础实体：底板、任意闭合二维轮廓拉伸、旋转、扫掠、放样；
- 加料/切除：凸台、侧凸台、筋、口袋、槽、通孔、侧孔、螺纹孔；
- 修饰特征：圆角、倒角、拔模、抽壳、圆顶；
- 复制特征：线性阵列、圆周阵列、孔圆阵列和镜像；
- 参数化：参考几何、方程、配置；
- 专项建模：渐开线直齿轮、齿轮副、钣金、焊件、受限自由曲面；
- 装配与文档：装配配合、工程图、剖视图、局部详图和常见导出。

这是一组**受参数契约约束的执行器**，不是 SolidWorks 全功能替代品，也
不代表任意机械模型都能一次成功。每个 operation 的详细状态见
[CAPABILITIES.md](CAPABILITIES.md)。

### AutoCAD、PDF 与文件转换

- 通过 AutoCAD COM 打开兼容 DWG 并创建原生尺寸对象；
- 分析 Line、Circle、Arc、Polyline 和 Block 等实体；
- 提供外形、孔径、孔定位、中心线、中心标记、圆角和倒角等标注策略；
- 将文本型 PDF、MinerU Markdown/JSON 或已配置 OCR 结果转换为 Drawing IR；
- 将 DWG、DXF、DWT、SLDPRT、SLDASM、SLDDRW、STEP、IGES、STL、X_T
  和 X_B 路由到对应连接器；
- 输出 DWG、DXF、PDF、解析报告和 Pipeline 报告。

扫描 PDF 的识别质量取决于用户配置的 OCR/MinerU 环境。尺寸不足时应返回
`unresolved` 并等待确认，不能猜测生产尺寸。

## CAD-IR 标准 operation

Beta.1 注册了 37 个标准 operation：

| 分组 | Operation |
|---|---|
| 基础几何 | `base_plate`, `profile_extrude`, `revolve`, `sweep`, `loft` |
| 加料与切除 | `boss`, `side_boss`, `rib`, `pocket`, `slot`, `through_hole`, `side_hole` |
| 螺纹与孔 | `threaded_hole`, `external_thread`, `bolt_circle_pattern` |
| 修饰与复制 | `fillet`, `chamfer`, `dome`, `draft`, `shell`, `linear_pattern`, `circular_pattern`, `mirror` |
| 参数与引用 | `reference_geometry`, `equation`, `configuration` |
| 专项建模 | `gear`, `gear_pair`, `sheet_metal`, `weldment`, `freeform_surface`, `source_part_clone` |
| 装配与工程图 | `assembly_mate`, `drawing`, `section_view`, `detail_view`, `autocad_annotation` |

“已注册”表示 CAD-IR 认识该 operation；真正允许执行还取决于参数契约、
依赖、目标引用、执行阶段、Skill 注册和当前 CAD 环境。格式说明与完整示例
见 [CAD-IR 使用指南](docs/CAD_IR_GUIDE.md)。

## 快速开始

### 1. 克隆并创建环境

```powershell
git clone https://github.com/zzz168904-pixel/partloom-ai-platform.git
cd partloom-ai-platform
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

### 2. 运行发布审计与测试

```powershell
.\.venv\Scripts\python.exe scripts\release_audit.py
.\.venv\Scripts\python.exe -m pytest
```

当前公开基线为：

```text
417 passed, 5 skipped
```

5 个跳过项依赖真实专有 CAD 模型，用于 SolidWorks 持久化引用验收。干净
克隆不会伪造或捆绑这些模型。

### 3. 启动 GUI

1. 使用同一 Windows 用户启动 SolidWorks；
2. 关闭未处理的 SolidWorks 弹窗，并保存重要文档；
3. 启动 PartLoom：

```powershell
.\.venv\Scripts\python.exe app.py
```

4. 在 GUI 中选择 **CAD-IR JSON**；
5. 加载：

```text
examples\cad_ir\gui_cad_ir_plate_with_center_hole.json
```

6. 检查计划只包含 `base_plate -> through_hole -> save_sldprt`；
7. 确认后执行，并在输出目录检查 SLDPRT 与 `pipeline_report.json`。

### 4. 运行 Gateway

```powershell
$env:PYTHONPATH = (Resolve-Path .\src)
.\.venv\Scripts\python.exe -m cad_agent.gateway `
  --host 127.0.0.1 `
  --port 8765 `
  --token local-development-token
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
```

受保护接口使用：

```text
Authorization: Bearer local-development-token
```

Gateway 支持计划、确认、取消、重试、任务状态和事件流。它拒绝绑定公网
地址，不能作为未经加固的远程服务部署。

## 合成示例

| 文件 | 演示内容 | 默认输出 |
|---|---|---|
| `gui_cad_ir_plate_with_center_hole.json` | 底板与中心通孔 | SLDPRT |
| `gui_cad_ir_spur_gear.json` | 参数化渐开线直齿轮 | SLDPRT |
| `gui_cad_ir_swept_elbow.json` | 圆截面沿路径扫掠 | SLDPRT |
| `gui_cad_ir_round_to_rect_loft.json` | 圆形到矩形过渡放样 | SLDPRT |
| `gui_cad_ir_sheet_metal_l_bracket.json` | 基础钣金 L 支架 | SLDPRT |

所有公开示例均为合成数据，不对应客户零件或生产图纸。

## 项目结构

```text
partloom-ai-platform/
├── app.py                         # PySide6 桌面入口
├── examples/cad_ir/               # 合成 CAD-IR 示例
├── packaging/                     # PyInstaller 与 Inno Setup 配置
├── scripts/                       # 发布审计、启动和构建脚本
├── src/
│   ├── cad_agent/
│   │   ├── cad_ir.py              # CAD-IR 编译和确定性门禁
│   │   ├── planner_validator.py   # 候选计划校验
│   │   ├── skill_planner.py       # Skill 路由和依赖排序
│   │   ├── registry.py            # 运行时 Skill Manager
│   │   ├── feature_reference_registry/
│   │   ├── geometry_resolver/
│   │   ├── pipeline/
│   │   ├── gateway/
│   │   ├── pdf2cad/
│   │   └── file2cad/
│   ├── SolidWorksCadAgentAddin/   # 实验性 TaskPane Add-in 源码
│   ├── sw_connector.py            # 基础 SolidWorks COM 封装
│   └── commands/                  # 文档和工程图命令
├── tests/                         # 合成数据回归测试
├── CAPABILITIES.md                # 能力与验证状态
├── INSTALL.md                     # 安装、配置和故障排查
├── OPEN_SOURCE_SCOPE.md           # 开源与数据边界
└── DISCLAIMER.md                  # 工程免责声明
```

架构职责和数据流见 [架构说明](docs/ARCHITECTURE.md)。

## Provider 与自然语言规划

直接 CAD-IR 模式不需要云端 API Key，也是当前最确定的演示入口。可选
Provider 适配器包括 OpenAI、DeepSeek、Claude、Gemini、Qwen 和 Ollama。

Provider 只能生成候选计划：

- 不能直接调用 Skill、Pipeline 或 CAD API；
- 不能自行宣布建模成功；
- 缺失尺寸必须进入 `unresolved`；
- 不支持的 operation 必须在启动 CAD 前被拦截；
- 最终是否执行由确定性 Validator、阶段门禁和用户确认决定。

发布包不包含任何 API Key。配置方式见 [INSTALL.md](INSTALL.md)。

## 输出与本地数据

默认路径：

- 日志和本地状态：`%LOCALAPPDATA%\PartLoomAI`
- 生成文件：`%USERPROFILE%\Documents\PartLoom_AI_Output`

可使用环境变量覆盖：

```text
PARTLOOM_APP_DATA=
PARTLOOM_OUTPUT_DIR=
```

每个任务使用独立目录，并记录 CAD-IR、Pipeline 状态、输出路径和失败原因。

## 安装与构建状态

当前 GitHub Release 提供仓库源码归档，尚未上传经过签名的预编译 Windows
安装包。源码用户可以直接运行 GUI，维护者也可以执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_release.ps1
```

该脚本会重新执行隐私审计和测试，再构建 PyInstaller 目录与 ZIP；本机安装
Inno Setup 6 时还会构建安装程序。完整要求见 [INSTALL.md](INSTALL.md)。

## 常见问题

### 为什么计划通过但 SolidWorks 特征仍可能失败？

CAD-IR 校验的是参数、依赖和执行边界。复杂模型还需要在真实 SolidWorks
文档中正确解析 Body、Face、Edge、Axis 和局部坐标系。模型越复杂，几何
引用和版本差异越需要本机验证。

### 为什么不能直接输入任意一句话生成所有零件？

自然语言经常缺少孔位、基准、方向、制造公差和目标面。PartLoom 选择返回
`need_confirmation`，而不是猜测生产几何。支持 operation 也不等于支持该
operation 的所有参数组合。

### 没有 AutoCAD 能否使用？

可以。AutoCAD 只在用户明确请求 DWG 标注或对应导出时启动。SolidWorks
建模和直接 CAD-IR 流程可以独立运行。

### 没有云模型 API Key 能否使用？

可以。直接 CAD-IR 输入不需要任何云模型。Provider 仅用于可选候选规划。

### 是否可以直接用于生产制造？

不能跳过人工审核。制造前必须检查尺寸、材料、特征树、重建错误、几何、
工程图和导出文件，并完成企业自己的设计评审与质量流程。

更多排障步骤见 [INSTALL.md](INSTALL.md)。

## 验证、贡献与安全

- 公共 CI：Windows + Python 3.11/3.13；
- 发布审计：阻止密钥、客户文件、个人路径、日志和生成 CAD 文件；
- 主分支：需要 CI 状态检查和 Pull Request；
- Issue/PR：只能使用合成 CAD-IR 和脱敏诊断信息；
- 安全问题：见 [SECURITY.md](SECURITY.md)；
- 贡献规则：见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 开源边界与许可证

平台核心源码已经公开。仓库仍不会包含客户资料、运行输出、API Key、厂商
二进制、MinerU 权重或未获再分发授权的模型库。这些是隐私和许可证边界，
不是隐藏的平台执行服务。

- 开源范围：[OPEN_SOURCE_SCOPE.md](OPEN_SOURCE_SCOPE.md)
- 工程免责声明：[DISCLAIMER.md](DISCLAIMER.md)
- 第三方说明：[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
- 许可证：[MIT License](LICENSE)

PartLoom 是独立开源项目，与 Dassault Systemes、SOLIDWORKS、Autodesk、
AutoCAD 或模型 Provider 厂商不存在附属、认证或背书关系。

## 相关项目

- [PartLoom SolidWorks Modeling Skill](https://github.com/zzz168904-pixel/partloom-solidworks-modeling)：面向 Codex 的独立确定性建模 Skill。
- [wzyn20051216/solidworks-automation-skill](https://github.com/wzyn20051216/solidworks-automation-skill)：另一个功能丰富的 SolidWorks 自动化开源项目，PartLoom 的文档层次与能力边界说明参考了其公开 README 的表达方式，但代码和能力声明相互独立。
