# PartLoom 能力与验证状态

本文档描述 v0.1.0 Beta.1 公开仓库的真实能力边界。README 中出现的
operation 必须以本页状态、CAD-IR 参数契约和当前环境门禁为准。

## 状态定义

| 状态 | 含义 |
|---|---|
| CI verified | 不启动 CAD 即可验证的编译、校验、路由、状态机或报告逻辑，已由公共 CI 覆盖 |
| Local CAD required | 包含生产执行器源码，但需要用户在受支持的本机 CAD 版本中验收 |
| Experimental | 有受限实现或适配器，参数范围和几何组合尚未形成稳定无人值守基线 |
| Blocked | 当前公开版本会失败关闭，不应被描述为可交付能力 |

“Local CAD required”不表示所有参数组合都已验证，也不表示厂商认证。

## 公开测试基线

```text
Windows / Python 3.11: passed
Windows / Python 3.13: passed
Regression: 417 passed, 5 skipped
Release audit: 0 issues
Distribution build: passed
```

5 个跳过测试需要真实专有 CAD 模型来捕获和重放 SolidWorks 持久化引用。
公开仓库不会捆绑这些模型。

## 平台核心

| 模块 | 状态 | 说明 |
|---|---|---|
| CAD-IR 编译与标准化 | CI verified | operation、别名、单位、必填参数、冲突和依赖校验 |
| Direct CAD-IR | CI verified | GUI 接收一个 JSON 对象或 JSON 文件路径，不经过 LLM |
| Planner Validator | CI verified | unresolved、assumptions、低置信度、未知 operation 和依赖错误会阻断 |
| Skill Planner | CI verified | 根据阶段与 `allowed_skills` 生成拓扑排序步骤 |
| Stage Gate | CI verified | 默认最小执行；未请求工程图、AutoCAD 和导出不会执行 |
| Output Guard | CI verified | 未请求文件不会被发布为任务产物 |
| Feature Reference Registry | CI verified | Skill/operation/参数/输入输出引用契约与可插拔 Provider |
| Persistent Reference API | CI verified + Local CAD required | token 编解码和错误处理由 CI 覆盖，真实引用需 SolidWorks 验收 |
| Geometry Resolver | CI verified + Local CAD required | 候选面打分和 O/U/V/N 坐标系由 CI 覆盖，真实 COM 面需本机验证 |
| Pipeline 生命周期 | CI verified | prepare、execute、verify、export、cleanup、失败停止和报告 |
| CAD Execution Lock | CI verified | 限制并发 CAD 任务，降低多文档和资源竞争风险 |
| Model Integrity Gate | CI verified + Local CAD required | Body、体积、包围盒、重建错误和必要特征检查 |
| Agent Gateway | CI verified | localhost API、认证、计划、确认、取消、重试、状态和事件流 |
| PySide6 GUI | CI verified + Local CAD required | 启动、预览和线程生命周期由 CI 覆盖；CAD 交互需本机验收 |
| SolidWorks TaskPane Add-in | Experimental | C#/.NET Framework 源码已公开，需用户配置 Interop 并编译注册 |

## SolidWorks CAD-IR operation

### 基础实体

| Operation | 状态 | 当前边界 |
|---|---|---|
| `base_plate` | Local CAD required | 明确长、宽、厚和新模型模式的矩形基础实体 |
| `profile_extrude` | Local CAD required | 显式闭合线/圆弧/圆轮廓；支持加料或切除语义 |
| `revolve` | Local CAD required | 显式半剖面、旋转轴、角度和 base/boss/cut |
| `sweep` | Local CAD required | 受限圆截面与显式路径；复杂扭转不保证 |
| `loft` | Local CAD required | 有序闭合截面；复杂导引线和高阶连续性不保证 |
| `source_part_clone` | Local CAD required | 仅复制用户明确提供且有权使用的源 SLDPRT，并执行一致性门禁 |

### 加料、切除和孔

| Operation | 状态 | 当前边界 |
|---|---|---|
| `boss` | Local CAD required | 在已解析目标面上创建明确轮廓凸台 |
| `side_boss` | Local CAD required | 侧面轴承座/法兰凸台，依赖正确局部坐标和实体合并 |
| `rib` | Local CAD required | 显式位置和厚度的加强筋 |
| `pocket` | Local CAD required | 矩形或受支持轮廓型腔，要求目标面可解析 |
| `slot` | Local CAD required | 受支持直槽/腰型槽参数 |
| `through_hole` | Local CAD required | 中心孔、显式坐标或已支持的边距布置 |
| `side_hole` | Local CAD required | 侧面孔和受限法兰孔圆，必须明确目标引用 |
| `threaded_hole` | Local CAD required | Hole Wizard 攻丝孔的受支持公制规格和深度 |
| `external_thread` | Local CAD required | ISO 装饰外螺纹；加工级实体牙型不作为通用默认 |
| `bolt_circle_pattern` | Local CAD required | 已注册孔圆语义，实际执行取决于种子特征和轴引用 |

### 修饰、阵列和参数化

| Operation | 状态 | 当前边界 |
|---|---|---|
| `fillet` | Local CAD required | 明确半径和可解析边集合 |
| `chamfer` | Local CAD required | 明确距离/角度和外边集合 |
| `dome` | Local CAD required | 单个已解析平面上的原生 Dome |
| `draft` | Local CAD required | 明确中性面、方向和目标面集合 |
| `shell` | Local CAD required | 明确壁厚和移除面；复杂多实体需额外验收 |
| `linear_pattern` | Local CAD required | 明确种子特征、方向、数量和间距 |
| `circular_pattern` | Local CAD required | 明确种子特征、轴、数量和角度 |
| `mirror` | Local CAD required | 明确种子特征和镜像基准面 |
| `reference_geometry` | Local CAD required | 受限偏置基准面和两平面参考轴 |
| `equation` | Local CAD required | 明确变量名、表达式和作用域 |
| `configuration` | Local CAD required | 创建或切换明确名称的配置 |

### 专项模型

| Operation | 状态 | 当前边界 |
|---|---|---|
| `gear` | Local CAD required | 参数化渐开线直齿轮；不是完整齿轮设计软件 |
| `gear_pair` | Local CAD required | 两个直齿轮、中心距定位和受限 Gear Mate |
| `sheet_metal` | Local CAD required | 基体法兰、边线法兰、受限折弯和展开路径 |
| `weldment` | Local CAD required | 显式 3D 路径和已安装 ISO 截面；复杂修剪需验收 |
| `freeform_surface` | Experimental | 显式闭合 3D 样条边界的受限填充曲面 |

### 装配与工程图

| Operation | 状态 | 当前边界 |
|---|---|---|
| `assembly_mate` | Experimental / Local CAD required | 受限组件插入和常见配合；复杂引用链仍需人工复核 |
| `drawing` | Local CAD required | 标准视图、布局和基础工程图生成 |
| `section_view` | Local CAD required | 受支持剖切线和工程图上下文 |
| `detail_view` | Local CAD required | 受支持局部详图上下文 |
| `autocad_annotation` | Experimental / Local CAD required | 在 SolidWorks 导出 DWG 上创建原生 AutoCAD 标注 |

## AutoCAD Annotation Engine

| 能力 | 状态 | 说明 |
|---|---|---|
| DWG 打开、保存与 COM 会话 | Local CAD required | 需要合法 AutoCAD 安装和交互式用户会话 |
| Entity Analyzer | CI verified + Local CAD required | 逻辑可测试，真实 DWG 实体需本机验收 |
| 外形尺寸、孔径和孔位置策略 | Experimental | 图层、比例和投影视图差异可能需要人工调整 |
| 中心线与中心标记 | Experimental | 依赖圆/圆弧识别和视图语义 |
| 圆角与倒角标注 | Experimental | 复杂投影或断裂视图可能存在歧义 |
| 自动避让和最佳位置 | Experimental | 当前不是完整人工制图排版替代品 |
| Annotated DWG 与 PDF | Local CAD required | PDF 失败时可由 Pipeline 记录 fallback 原因 |

## PDF2CAD 与 File2CAD

| 能力 | 状态 | 说明 |
|---|---|---|
| 文本型 PDF 解析 | CI verified | 标题栏、尺寸、材料、比例和注释的受限解析 |
| MinerU Markdown/JSON 适配 | CI verified + external dependency | 不捆绑 MinerU 或模型权重 |
| 扫描 PDF OCR | Experimental | 需要用户配置 OCR；识别结果必须人工确认 |
| Drawing IR / Design JSON | CI verified | 统一 geometry、dimensions、views、notes 等结构 |
| AutoCAD 二维重建 | Local CAD required | 只在信息完整且通过门禁时生成 DWG/DXF |
| PDF 转三维 | Experimental | 必须明确选择，且尺寸/视图/特征完整度通过 |
| DWG/DXF 输入路由 | CI verified + Local CAD required | 解析和转换能力取决于本机连接器 |
| STEP/IGES/STL/X_T/X_B 路由 | CI verified + Local CAD required | 本版本没有独立无头 B-Rep 内核 |

## 文件输出

| 格式 | 负责连接器 | 状态 |
|---|---|---|
| SLDPRT | SolidWorks | Local CAD required |
| SLDASM | SolidWorks | Experimental / Local CAD required |
| SLDDRW | SolidWorks | Local CAD required |
| STEP / IGES / Parasolid / STL | SolidWorks | Local CAD required |
| DWG / DXF | SolidWorks 或 AutoCAD | Local CAD required |
| Annotated DWG | AutoCAD | Experimental / Local CAD required |
| PDF | SolidWorks 或 AutoCAD | Local CAD required |
| brain/design/pipeline/annotation report JSON | Platform | CI verified |

## 当前不应宣称的能力

- 任意自然语言都能一次生成正确机械零件；
- 任意 STEP 自动恢复原始可编辑特征树；
- 从低清扫描图自动获得生产级完整尺寸；
- SolidWorks 全部页面、全部 API 和全部插件功能；
- 完整 Class-A 曲面、模具、Routing、Simulation/FEA 或 CAM；
- 无 SolidWorks/AutoCAD 时写入其专有原生格式；
- 未经工程师审核即可直接制造。

新增能力时，应同时更新 CAD-IR 契约、Skill Registry、执行器、验证器、
合成回归测试和本文件，避免“存在入口”被误写成“生产验证通过”。
