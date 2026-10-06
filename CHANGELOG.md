# 更新日志

本文件记录本项目的重要变更。

版本号说明：项目早期使用开发期编号（最后一个为 `22.0.0`），未遵循语义化版本，  
且此前没有任何变更记录。**本版本将编号重置为语义化版本 `1.0.0`**，  
并自本版本起开始维护本文件。

---

## [1.0.0] — 2026-10-03

本版本既是**首个语义化版本**，也是一次**审计修复版本**：依据一次全维度代码审计报告  
（代码质量与结构 / 性能 / 安全 / 可维护性 / 可扩展性 / 用户体验 / 文档），  
按优先级修复了全部已识别问题。所有修复均在 **Blender 3.4.1** 实机验证，  
新增无头冒烟测试 **35 项全部通过**。

### 🔴 修复（阻塞级）

- **修复「使用聚类」必然崩溃**（`operators/match_ops.py`）  
  `find_best_match_for_cluster` 返回 4 个值，调用方却只解包 2 个变量，  
  触发 `ValueError: too many values to unpack`。该异常此前被外层 `except`  
  吞掉，界面只显示「找到 0 个匹配」，用户无法察觉功能已完全失效。
- **修复带修改器时颜色静默错配**（`core/cache.py`）  
  顶点位置取自 `to_mesh()` 的求值网格（含修改器结果），而颜色却取自原始网格的  
  `loop.vertex_index`。物体带细分/布尔/镜像等修改器时两者索引错位，  
  导致颜色被**错误地复制且不报任何错**。现统一从同一个求值网格读取。
- **修复通道预览可能导致原始顶点色永久丢失**（`operators/edit_ops.py`）  
  原实现把原始颜色序列化为 JSON 字符串存入 Scene 的 `StringProperty`，  
  `json.loads` 失败时被静默兜底为空字典，且预览状态下保存 `.blend` 或崩溃  
  都会丢失原始色。现改为在网格上创建真实备份颜色层  
  （`__vct_preview_backup__`），随物体持久化，恢复后自动清理。

### 🟠 修复（重要）

- **缓存污染**（`core/cache.py`）：缓存键原为 `(物体名, 层名)`，重命名、  
  重建同名物体、Linked Duplicate、跨文件会话都会命中错误缓存。  
  现改为「内存地址分桶 + 条目内对象引用做 `is` 身份校验」，  
  并新增 `load_post` 钩子清空缓存。  
  *注：实测 Blender 3.4 的 Object 无 `session_uid`、bpy_struct 不支持 weakref，  
  故采用身份校验方案——校验失败只会重新计算，绝不会返回错误数据。*
- **缓存自我失效**（`operators/copy_ops.py`）：移除了每次批量复制开头无条件调用的  
  `clear_cache()`，该调用使「使用缓存」开关形同虚设。
- **性能悬崖兜底**（`core/cache.py`、`core/vertex_color_ops.py`）：  
  顶点数超过 20000 时强制构建 KDTree；暴力搜索路径增加运算量上限，  
  超限时自动构建 KDTree，避免退化为 O(N×M) 的纯 Python 循环  
  （5 万×5 万 ≈ 25 亿次距离计算会把 Blender 卡死）。
- **多物体填充的模式切换开销**（`core/vertex_color_ops.py`）：  
  从「每个物体切换两次模式」改为「整体只切换一次」。
- **新增匹配结果预览面板**（`ui/results_panel.py`）：  
  此前 `match_results` 在界面中完全没有呈现，用户看不到匹配了谁、置信度多少，  
  只能盲目点「复制」。现以 UIList 展示 `源 → 目标 + 置信度 + 聚类分组`，  
  并支持移除选中匹配 / 清空结果。
- **批量操作进度与取消**（`operators/match_ops.py`、`operators/copy_ops.py`）：  
  新增进度条，可用 ESC 取消；同时修复 `cancelled` 标志跨实例残留的问题。
- **清除顶点色新增二次确认**（`operators/edit_ops.py`）：  
  该操作会删除选中物体的全部颜色层，此前无任何确认，误点影响整个选择集。

### 🟡 修复（清理与工程化）

- 删除 4 个全项目零调用的死函数：`get_active_vertex_color_layer`、  
  `has_multiple_colors`、`save_color_attribute_data`、`restore_color_attribute_data`。
- 移除未使用的 `bmesh` / `gc` / `json` 导入，以及函数体内重复的 `import traceback`。
- 合并三个高度雷同的颜色算子（填充 / 应用 / 修改）的执行逻辑。
- 匹配权重改为 `SIMILARITY_METRICS` 注册表驱动；参数预设改为  
  `MATCH_PRESETS` 数据表驱动（新增预设/参数无需再改 if-elif 链）。
- 插件注册改为**按模块自动收集类**，不再手工维护易漏改的 `classes` 列表；  
  注册失败时回滚，避免留下半注册状态。
- 新增 `utils/logging_utils.py` 统一日志与错误上报：错误同时进入控制台  
  与面板状态栏，解决「操作失败但用户不知道原因」的问题。
- 修正最低 Blender 版本声明：`3.0.0` → `3.2.0`（`color_attributes` 自 3.2 引入）。
- 新增 `README.md`（安装/快速上手/参数说明/算法/FAQ）与本 `CHANGELOG.md`。
- 新增 `tests/blender_smoke_test.py` 无头冒烟测试（35 项）。
- 纳入 git 版本管理，并补充 `.gitignore`。

### ⚠️ 尚未处理

- **国际化（i18n）**：界面仍为硬编码中文，未接入 Blender 翻译系统。  
  属产品决策，需配套翻译资源，留待后续版本。
- 聚类算法仍为 O(n²)，仅增加了廉价距离粗筛；超大目标组（数千物体）仍会较慢。
- 批量操作采用进度条而非 `modal` 模态循环，取消粒度以批为单位。
