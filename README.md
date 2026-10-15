# Blender 顶点色复制工具

在两组模型之间按**位置接近度**与**几何相似度**匹配物体，并批量复制顶点色。
适用于 LOD 分级、资产变体等场景。

- Blender 3.2 及以上（使用 `mesh.color_attributes`，该 API 自 3.2 引入）
- Python 3.7 及以上

---

## 工作流

1. **分析集合** —— 记录源集合与目标集合的顶点色统计
2. **查找匹配** —— 按相似度把目标物体配到源物体，匹配结果可逐条复核与剔除
3. **复制顶点色** —— 按匹配结果把源颜色写到目标上，期间可按 `ESC` 取消

匹配有四档预设：**快速 / 精确 / 宽松 / 聚类**，也可自定义权重。

另有手动工具：筛选无顶点色或不纯顶点色的物体、选择与填充颜色、清除颜色、
单通道预览（可逐通道预览后再恢复 RGBA）、手动复制。

---

## 约束

**取色距离上限** —— 防止超出源范围被「拉色」，超距顶点保留原色（`0` 为关闭）。

**法线夹角阈值** —— 防止薄壁模型跨面取色（`0` 为关闭）。
命中非法线时会在距离上限内逐步扩大搜索半径，找朝向一致的替代顶点；
八轮仍找不到就退回纯距离最近的顶点。

---

## 原生内核

最近邻取色、相似度匹配、约束取色、聚类由一个纯 C ABI 动态库承担，
通过 ctypes 加载。**库缺失、ABI 不符或自检失败时一律回退纯 Python**，
所以任何平台都能用——二进制只是加速项，不是硬依赖。

面板上的「原生加速」区会显示**实际加载到的库文件名与 ABI 版本**，
而不是笼统地说「已启用」：静默降级时用户唯一能察觉到的现象就是变慢，
给出具体产物才能让「变慢了」变成「哦，原来加载的是它」。

### 导出符号

| 符号 | 用途 |
|---|---|
| `vct_api_version` | ABI 版本查询 |
| `vct_self_test` | 加载后自检 |
| `vct_kdtree_create` / `vct_kdtree_free` | 空间索引的建立与释放 |
| `vct_kdtree_query_indices` | 批量最近邻查询 |
| `vct_kdtree_pick_constrained` | 带距离与法线约束的取色 |
| `vct_match_best` | 逐目标最佳匹配 |
| `vct_cluster_targets` | 目标分组 |

### 构建

```bash
cd native/vct-rs
cargo build --release     # 产物在 target/<target>/release/vct_native.dll
cargo test                # 单元测试
```

零第三方依赖，产物只依赖系统 DLL。

---

## 参与开发

> **给外部贡献者的重要说明**
>
> `main` 分支受保护,且仅维护者可直接推送。
> **请向 `dev` 分支提交 Pull Request;直接向 `main` 提交的 PR 大概率不会受理。**

推荐流程:

1. Fork 本仓库到你自己账号
2. 从 `dev` 分支切出特性分支(`git checkout -b feature/xxx`)
3. 提交改动并推送到你的 Fork
4. 向本仓库的 **`dev` 分支**开 Pull Request,等待 CI 通过与合并

分支流向:`feature/xxx` → `dev`(日常集成)→ `main`(发布版本)

## 开发

```bash
python scripts/run_ci_checks.py      # 快速检查，无需 Blender
```

原生内核的改动必须同时满足下面两条：

```bash
cd native/vct-rs
cargo fmt --all --check
cargo clippy --all-targets --all-features -- -D warnings
cargo build  --workspace --all-targets
cargo test   --workspace --all-features
cargo test   --workspace --doc
cargo doc    --workspace --no-deps
```

详见 [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) 与 `native/vct-rs` 的模块文档。

---

## 许可

MIT。见 [LICENSE](LICENSE)。
