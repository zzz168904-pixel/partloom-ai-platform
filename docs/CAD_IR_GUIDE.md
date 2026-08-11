# CAD-IR 使用指南

CAD-IR 是 PartLoom 的确定性机械建模输入。它不是 SolidWorks API 调用列表，
而是描述 Feature、参数、依赖、目标引用、执行阶段和预期输出的中间语言。

## 最小结构

```json
{
  "version": "cad.ir.v1",
  "unit_system": "mm",
  "task_type": "model_3d",
  "part_type": "plate",
  "outputs": ["SLDPRT"],
  "features": []
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `version` | 当前标准版本必须为 `cad.ir.v1` |
| `unit_system` | 当前公开示例统一为 `mm` |
| `task_type` | `model_3d`、`modify_3d`、`create_drawing`、`annotate_drawing`、`export_files` 或 `full_pipeline` |
| `part_type` | 用于分类与 Registry 检索，不替代 Feature 定义 |
| `outputs` | 只列用户明确要求的文件类型 |
| `features` | 按依赖描述的 Feature 数组 |

## Feature 结构

```json
{
  "id": "center_hole",
  "name": "CenterHole",
  "operation": "through_hole",
  "required": true,
  "parameters": {
    "diameter_mm": 20.0,
    "count": 1
  },
  "options": {
    "position": "center"
  },
  "target": {
    "resolution": "semantic_reference",
    "body_ref": "primary_solid",
    "feature_ref": "base",
    "face_role": "outer_horizontal_face"
  },
  "dependencies": [
    {
      "kind": "feature",
      "feature_id": "base"
    }
  ]
}
```

规则：

- `id` 在任务内唯一；
- `operation` 必须来自标准 operation；
- `parameters` 使用 canonical 参数名和 mm/deg；
- required Feature 失败后 Pipeline 必须停止；
- `dependencies` 只能引用更早或可拓扑排序的 Feature；
- 修改现有模型的 Feature 必须描述目标 Body 和语义引用；
- 缺少目标位置时应阻断，不能默认猜测 `top/front/right`。

## 完整底板示例

```json
{
  "version": "cad.ir.v1",
  "unit_system": "mm",
  "task_type": "model_3d",
  "part_type": "plate",
  "outputs": ["SLDPRT"],
  "features": [
    {
      "id": "base",
      "name": "BasePlate",
      "operation": "base_plate",
      "required": true,
      "parameters": {
        "length_mm": 100.0,
        "width_mm": 60.0,
        "thickness_mm": 10.0
      },
      "options": {},
      "target": {
        "resolution": "new_body",
        "body_ref": "primary_solid"
      },
      "dependencies": []
    },
    {
      "id": "center_hole",
      "name": "CenterHole",
      "operation": "through_hole",
      "required": true,
      "parameters": {
        "diameter_mm": 20.0,
        "count": 1
      },
      "options": {
        "position": "center"
      },
      "target": {
        "resolution": "semantic_reference",
        "body_ref": "primary_solid",
        "feature_ref": "base",
        "face_role": "outer_horizontal_face"
      },
      "dependencies": [
        {
          "kind": "feature",
          "feature_id": "base"
        }
      ]
    }
  ]
}
```

同一内容位于：

```text
examples/cad_ir/gui_cad_ir_plate_with_center_hole.json
```

## 执行阶段

默认最小执行原则：

- `model_3d`：只建模并保存 SLDPRT；
- `create_drawing`：使用当前模型生成 SLDDRW，不重新建模；
- `annotate_drawing`：只执行 DWG/AutoCAD 标注阶段；
- `export_files`：只导出明确列出的格式；
- `full_pipeline`：只有用户明确请求完整流程时使用。

即使 `outputs` 中出现历史字段，Router 仍应以 `requested_stages`、
`allowed_skills` 和用户确认后的执行政策为准。

## 目标引用

新实体通常使用：

```json
{
  "resolution": "new_body",
  "body_ref": "primary_solid"
}
```

后续特征使用语义引用：

```json
{
  "resolution": "semantic_reference",
  "body_ref": "primary_solid",
  "feature_ref": "base",
  "face_role": "outer_horizontal_face"
}
```

执行阶段由 Feature Reference Registry 和 Geometry Resolver 将语义角色解析
为真实 COM 对象。解析结果有多个等价候选时应返回歧义错误。

## 常见拦截

### 缺少必填参数

```json
{
  "id": "bad_hole",
  "operation": "through_hole",
  "parameters": {},
  "dependencies": []
}
```

应在启动 SolidWorks 前返回缺少 `diameter_mm` 等结构化错误。

### 依赖不存在

```json
{
  "id": "hole",
  "operation": "through_hole",
  "parameters": {"diameter_mm": 8},
  "dependencies": [{"kind": "feature", "feature_id": "missing_base"}]
}
```

应由 CAD-IR 依赖门禁阻断。

### 输出越权

如果任务只请求 `SLDPRT`，Pipeline 不得自动生成或发布 STEP、SLDDRW、
DWG、Annotated DWG 或 PDF。

## 如何新增 operation

不要只在 JSON 中写一个新字符串。新增 operation 需要同时补齐：

1. CAD-IR canonical 名称和别名；
2. 参数、单位、必填组、冲突和上下文规则；
3. Skill Registry 的输入输出引用与副作用；
4. Pipeline 执行器与几何验证；
5. 合成单元测试和真实 CAD 验收；
6. `CAPABILITIES.md` 状态。
