# InteractiveHtmlBom 项目架构分析文档

> KiCad/PCB 交互式 HTML BOM 生成工具

---

## 1. 项目概述

**InteractiveHtmlBom** 是一个为 KiCad、Eagle、EasyEDA、Fusion360 等 PCB 设计软件生成交互式 BOM（物料清单）的工具。它将 BOM 生成为单个自包含的 HTML 文件，用户可以在浏览器中直观地查看元件分布、搜索定位、高亮网络等。

- **仓库**: https://github.com/openscopeproject/InteractiveHtmlBom
- **Star**: 4,330 | **Fork**: 548
- **许可证**: MIT
- **主要语言**: Python (78.7%), JavaScript (15.5%), CSS (3.0%)

---

## 2. 目录结构

```
InteractiveHtmlBom/
├── __init__.py                        # KiCad 插件注册入口
│
├── InteractiveHtmlBom/                # 主包
│   ├── generate_interactive_bom.py     # CLI 入口和主程序
│   ├── compat.py                      # Python 2/3 兼容性工具
│   ├── errors.py                      # 错误处理
│   ├── version.py                     # 版本信息
│   │
│   ├── core/                          # 核心 BOM 生成模块
│   │   ├── ibom.py                    # BOM 生成主逻辑
│   │   ├── config.py                  # 配置管理
│   │   ├── fontparser.py              # KiCad 字体解析
│   │   ├── newstroke_font.py          # KiCad 内置字体数据
│   │   ├── lzstring.py                # LZ 压缩（内嵌）
│   │   └── units.py                   # 单位转换工具
│   │
│   ├── ecad/                          # ECAD 文件解析层（插件化）
│   │   ├── __init__.py                # 解析器工厂
│   │   ├── common.py                  # 通用基类和工具
│   │   ├── kicad.py                   # KiCad PCB 解析器
│   │   ├── easyeda.py                 # EasyEDA 解析器
│   │   ├── genericjson.py             # 通用 JSON 格式解析器
│   │   ├── fusion_eagle.py            # Eagle/Fusion 解析器
│   │   ├── svgpath.py                 # SVG 路径解析工具
│   │   └── kicad_extra/               # KiCad 专用解析工具
│   │       ├── netlistparser.py       # 网表解析
│   │       ├── sexpressions.py        # S-Expression 解析
│   │       └── xmlparser.py           # XML 解析
│   │
│   ├── dialog/                        # wxPython 设置对话框
│   │   ├── settings_dialog.py         # 设置对话框实现
│   │   └── bitmaps/                  # 按钮图标资源
│   │
│   ├── web/                           # HTML 前端资源
│   │   ├── ibom.html                  # 主 HTML 模板
│   │   ├── ibom.js                    # BOM 表格交互逻辑
│   │   ├── ibom.css                   # 样式表
│   │   ├── render.js                  # PCB Canvas 渲染引擎
│   │   ├── util.js                    # 工具函数
│   │   ├── table-util.js              # 表格拖拽排序
│   │   ├── split.js                    # 面板分割库（内嵌）
│   │   ├── lz-string.js                # 压缩库（内嵌）
│   │   ├── pep.js                      # 触摸事件库（内嵌）
│   │   └── user-file-examples/         # 用户扩展示例
│   │
│   └── i18n/                          # 国际化脚本
│
├── icons/                             # 工具栏图标
├── settings_dialog.fbp                # wxFormBuilder UI 定义
├── DATAFORMAT.md                      # 数据格式文档
├── pyproject.toml                     # 项目配置
└── README.md
```

---

## 3. 系统架构

### 3.1 整体分层架构

```
┌─────────────────────────────────────────────────────────────┐
│                   UI Layer (wxPython)                       │
│         Settings Dialog · Pcbnew Action Plugin              │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                Entry Point Layer                            │
│        generate_interactive_bom.py (CLI + GUI 双入口)       │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                   Core Layer (ibom.py)                       │
│         BOM 生成逻辑 · 组件过滤/分组 · 文件输出              │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│              ECAD Parser Layer (插件化设计)                  │
│   ┌─────────┬──────────┬─────────────┬──────────────────┐  │
│   │  KiCad  │ EasyEDA  │Eagle/Fusion │  Generic JSON    │  │
│   └─────────┴──────────┴─────────────┴──────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│              Frontend Layer (HTML/JS/CSS)                   │
│        BOM Table · Canvas Renderer · Event Handling        │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 模块职责

| 模块 | 路径 | 职责 |
|------|------|------|
| **Plugin** | `__init__.py` | KiCad ActionPlugin 注册 |
| **Entry** | `generate_interactive_bom.py` | CLI 参数解析、GUI 启动 |
| **Core** | `core/ibom.py` | BOM 生成核心逻辑、过滤/分组 |
| **Config** | `core/config.py` | 配置加载/保存/序列化 |
| **Parser** | `ecad/*` | 各 ECAD 格式文件解析 |
| **Dialog** | `dialog/settings_dialog.py` | wxPython 配置界面 |
| **Frontend** | `web/*` | 生成的 HTML BOM 交互界面 |

---

## 4. 数据流

### 4.1 完整处理流程

```
┌─────────────────────────────────────────────────────────────────────┐
│                              输入阶段                                │
│   ┌───────────────┐  ┌────────────────┐  ┌────────────────────┐   │
│   │  .kicad_pcb   │  │  .xml / .net    │  │  其他 ECAD 格式     │   │
│   │   (KiCad)    │  │   (网表/原理图) │  │   (EasyEDA 等)     │   │
│   └───────┬───────┘  └───────┬────────┘  └─────────┬──────────┘   │
└───────────┼──────────────────┼────────────────────┼───────────────┘
            │                  │                    │
            ▼                  ▼                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     解析阶段 (ECAD Parser Layer)                    │
│  1. 加载 PCB 文件 (pcbnew.LoadBoard)                                 │
│  2. 解析封装: pads, drawings, bbox                                  │
│  3. 解析铜层走线/区域 (可选)                                         │
│  4. 解析边框/轮廓                                                    │
│  5. 从网表/XML 导入额外字段                                          │
│  6. 合并组件数据                                                     │
└─────────────────────────────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     数据结构化 (pcbdata)                             │
│  {                                                                       │
│    "edges_bbox": {...},       # 板框边界                              │
│    "edges": [...],            # 边框线                                 │
│    "drawings": {              # 丝印/装配层                           │
│      "silkscreen": {"F": [], "B": []},                               │
│      "fabrication": {"F": [], "B": []}                               │
│    },                                                                     │
│    "footprints": [...],        # 封装数据                              │
│    "tracks": {"F": [], "B": []}, # 走线(可选)                         │
│    "zones": {"F": [], "B": []},   # 区域(可选)                         │
│    "nets": [...],              # 网络列表(可选)                        │
│    "metadata": {...},           # PCB 元数据                            │
│    "font_data": {...}          # 字体数据                              │
│  }                                                                       │
└─────────────────────────────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     BOM 生成阶段 (core/ibom.py)                      │
│  1. 组件过滤 (skip_component):                                        │
│     - 黑名单过滤 (ref 前缀)                                          │
│     - DNP 过滤                                                        │
│     - 板卡变体过滤                                                    │
│  2. 组件分组 (generate_bom):                                          │
│     - 按 Value/Footprint/额外字段分组                                 │
│     - 分离正反面                                                      │
│     - 自然排序 (natural sort)                                         │
│  3. BOM 数据结构:                                                     │
│     {both: [...], F: [...], B: [...], skipped: [...]}                │
└─────────────────────────────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                              输出阶段                                │
│  1. LZ 压缩 PCB 数据 (可选, 减少文件大小)                             │
│  2. 模板替换: ibom.html + CSS + JS + PCBDATA + CONFIG                │
│  3. 输出单个 HTML 文件(自包含, 无需网络)                              │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 5. ECAD 解析层（插件化设计）

### 5.1 解析器基类

所有解析器继承自 `EcadParser` 基类，定义统一接口：

```python
class EcadParser:
    def parse(self) -> (pcbdata, components)  # 核心解析方法
    def get_extra_field_data() -> ExtraFieldData
    def latest_extra_data() -> str
    def extra_data_file_filter() -> str
```

### 5.2 解析器工厂

```python
# ecad/__init__.py
def get_parser_by_extension(file_name, config, logger):
    ext = os.path.splitext(file_name)[1]
    if ext == '.kicad_pcb':
        return get_kicad_parser(...)
    elif ext == '.json':
        return get_generic_json_parser(...)
    elif ext in ['.fbrd', '.brd']:
        return get_fusion_eagle_parser(...)
    else:
        return get_easyeda_parser(...)
```

### 5.3 支持的格式

| ECAD | 文件格式 | 解析器 | 备注 |
|------|----------|--------|------|
| KiCad | `.kicad_pcb` | `kicad.py` | 使用 pcbnew API |
| KiCad | `.xml`, `.net` | `kicad_extra/xmlparser.py` | BOM 额外字段 |
| EasyEDA | `.json`, `.json` | `easyeda.py` | 立创EDA |
| Eagle/Fusion | `.fbrd`, `.brd` | `fusion_eagle.py` | 原生支持 |
| Generic | `.json` | `genericjson.py` | 通用 JSON Schema |

---

## 6. KiCad 插件集成

### 6.1 ActionPlugin 实现

```python
class InteractiveHtmlBomPlugin(pcbnew.ActionPlugin):
    def __init__(self):
        self.name = "Generate Interactive HTML BOM"
        self.category = "Read PCB"
        self.icon_file_name = ...
    
    def Run(self):
        board = pcbnew.GetBoard()
        parser = PcbnewParser(...)
        config = Config(...)
        ibom.run_with_dialog(parser, config, logger)
```

### 6.2 注册机制

KiCad 启动时会扫描 `~/.kicad_plugins/` 目录并自动加载 ActionPlugin。`__init__.py` 中的注册语句确保插件在 PCBnew 的 "External Plugins" 菜单中可见。

---

## 7. 前端架构

### 7.1 HTML 模板结构

生成的 HTML BOM 使用模板占位符机制嵌入数据：

```
ibom.html
├── CSS 层 (ibom.css + user.css)
├── JS 层 (模块化加载)
│   ├── ///SPLITJS///       - 面板分割库
│   ├── ///LZ-STRING///    - 解压缩库
│   ├── ///CONFIG///        - BOM 配置
│   ├── ///PCBDATA///       - PCB 数据
│   ├── ///RENDERJS///      - 渲染引擎
│   ├── ///IBOMJS///        - BOM 交互
│   └── ///USERJS///        - 用户扩展
└── DOM 结构
    ├── top (控制栏+按钮)
    └── bot (分割面板)
        ├── bomdiv (BOM 表格)
        └── canvasdiv (PCB 画布)
            ├── frontcanvas (正面)
            └── backcanvas (背面)
```

### 7.2 Canvas 多层渲染架构

每个 PCB 画布使用 **4 层 Canvas 叠加**：

```
┌────────────────────┐
│  bg   - 背景层      │  ← Tracks/Zones/Footprints/Edges
├────────────────────┤
│  fab  - 装配层      │  ← Fabrication drawings
├────────────────────┤
│  silk - 丝印层      │  ← Silkscreen
├────────────────────┤
│  hl   - 高亮层      │  ← 交互高亮
└────────────────────┘
```

### 7.3 前端模块

| 模块 | 文件 | 功能 |
|------|------|------|
| 渲染引擎 | `render.js` | Canvas 绑定、图形绘制、坐标变换 |
| BOM 交互 | `ibom.js` | 表格生成、排序、过滤、复选框 |
| 工具函数 | `util.js` | 存储、本地化、值解析、导入/导出 |
| 表格操作 | `table-util.js` | 列拖拽重排序、宽度调整 |

---

## 8. 配置系统

### 8.1 配置字段分类

```python
# HTML 显示配置
dark_mode: bool              # 暗模式
show_pads: bool              # 显示焊盘
show_fabrication: bool       # 显示装配层
show_silkscreen: bool        # 显示丝印
highlight_pin1: str          # 高亮引脚1
board_rotation: int          # 板卡旋转角度
checkboxes: str              # 复选框列
bom_view: str                # BOM 布局 (bom-only/left-right/top-bottom)
layer_view: str              # 层视图 (F/FB/B)
compression: bool            # 启用压缩

# BOM 生成配置
bom_dest_dir: str            # 输出目录
bom_name_format: str         # 文件名格式
component_sort_order: list   # 排序顺序
component_blacklist: list     # 黑名单
include_tracks: bool         # 包含走线
include_nets: bool           # 包含网络

# 字段配置
show_fields: list            # 显示字段
group_fields: list           # 分组字段
extra_data_file: str        # 额外数据文件
dnp_field: str               # DNP 字段名
```

### 8.2 配置持久化

- **文件**: `ibom.config.ini` (与 PCB 同目录)
- **前端**: `localStorage` / `sessionStorage`
- **导出/导入**: JSON 格式

---

## 9. 核心数据结构

### 9.1 pcbdata

```python
pcbdata = {
    # 板框
    "edges_bbox": {"minx", "miny", "maxx", "maxy"},
    "edges": [drawing, ...],
    
    # 绘图层
    "drawings": {
        "silkscreen": {"F": [], "B": []},
        "fabrication": {"F": [], "B": []}
    },
    
    # 封装
    "footprints": [{
        "ref": "R1",
        "layer": "F",
        "bbox": {"pos": [], "relpos": [], "size": [], "angle": 0},
        "pads": [{...}, ...],
        "drawings": [{"layer": "F", "drawing": {...}}]
    }],
    
    # 可选数据
    "tracks": {"F": [], "B": []},
    "zones": {"F": [], "B": []},
    "nets": ["GND", "VCC", ...],
    
    # 元数据
    "metadata": {"title", "revision", "company", "date", "variant"},
    
    # BOM
    "bom": {
        "both": [[("R1", 0), ("R2", 1)], ...],  # 两面分组
        "F": [...],   # 正面
        "B": [...],   # 背面
        "skipped": [3, 5],  # DNP 索引
        "fields": {0: ["10k", "0805", ...], ...}
    },
    
    # 字体
    "font_data": {"A": {"w": 0.6, "l": [[[0,0], [1,0]]]}}
}
```

### 9.2 Drawing 类型

| type | 属性 | 描述 |
|------|------|------|
| segment | start, end, width | 线段 |
| rect | start, end, width | 矩形 |
| circle | start, radius, width | 圆形 |
| arc | start, radius, startangle, endangle, width | 圆弧 |
| polygon | pos, angle, polygons/filled | 多边形 |
| curve | start, end, cpa, cpb | 贝塞尔曲线 |
| text | pos, text, height, width, thickness, attr | 文字 |

---

## 10. 扩展机制

### 10.1 用户自定义文件

```
web/user-file-examples/
├── user.js           # 自定义 JavaScript
├── user.css          # 自定义 CSS 样式
├── userheader.html   # HTML 头部自定义
└── userfooter.html   # HTML 底部自定义
```

### 10.2 事件系统

前端提供事件回调供用户扩展：

```javascript
EventHandler.registerCallback(IBOM_EVENT_TYPES.HIGHLIGHT_EVENT, callback);
EventHandler.registerCallback(IBOM_EVENT_TYPES.CHECKBOX_CHANGE_EVENT, callback);
EventHandler.registerCallback(IBOM_EVENT_TYPES.BOM_BODY_CHANGE_EVENT, callback);
```

---

## 11. 依赖库

| 库名 | 用途 | 来源 |
|------|------|------|
| pcbnew | KiCad PCB API | KiCad 内置 |
| wxPython | GUI 对话框 | 可选（CLI 模式不需要） |
| json | 数据序列化 | Python 标准库 |
| jsonschema | JSON Schema 验证 | genericjson 格式验证 |
| Split.js | 面板分割 | 内嵌 (1.3.5, MIT) |
| lz-string.js | 数据压缩 | 内嵌（无依赖） |
| PEP.js | 触摸事件支持 | 内嵌 (MIT) |

---

## 12. 架构特点总结

1. **插件化解析层**: 通过 EcadParser 基类支持多种 ECAD 格式，易于扩展新格式
2. **配置与逻辑分离**: Config 类统一管理所有选项，支持 INI/对话框/命令行三种方式
3. **前后端分离**: Python 后端生成数据，JavaScript 前端渲染交互
4. **自包含输出**: 所有依赖内嵌到单个 HTML 文件，无需网络连接
5. **事件驱动扩展**: 提供事件回调供用户 JavaScript 扩展
6. **KiCad 深度集成**: ActionPlugin 无缝集成到 Pcbnew 界面

---

*文档生成时间: 2026-03-22*
