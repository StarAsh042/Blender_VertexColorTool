/*
 * 顶点色复制工具 —— 原生加速核心 ABI
 *
 * 设计原则:
 *   - 只暴露「纯 C ABI」（extern "C" + 基本类型指针），不触碰任何 Python 对象。
 *     因此同一份 DLL 可被任意 Python 3.x 版本加载（Blender 3.4 / 4.x 通用），
 *     也便于通过 ctypes 调用，无需 Python 开发头文件。
 *   - 所有缓冲区由调用方（Python/numpy）分配，本库不负责生命周期，
 *     避免跨运行时的内存分配/释放不匹配问题。
 *   - 输入数组约定为行主序（row-major）连续 float32:
 *       points : N x 3
 *       colors : N x 4  (RGBA)
 *
 * 错误约定: 所有返回 int 的函数，0 表示成功，负值表示失败。
 */

#ifndef VCT_NATIVE_H
#define VCT_NATIVE_H

#ifdef __cplusplus
extern "C" {
#endif

/* ABI 版本号。Python 侧加载后会校验，避免新旧不匹配导致的越界访问。 */
#define VCT_API_VERSION 1

#if defined(_WIN32)
#  define VCT_API __declspec(dllexport)
#else
#  define VCT_API __attribute__((visibility("default")))
#endif

/* 返回本库的 ABI 版本号 */
VCT_API int vct_api_version(void);

/* 自检：执行内部一致性测试，返回 0 表示通过。用于构建后验证。 */
VCT_API int vct_self_test(void);

/*
 * 构建 KDTree。
 *   points: N x 3 顶点坐标
 *   count : 顶点数 N
 * 返回不透明句柄；失败返回 NULL。
 * 句柄在 build 完成后为只读，可被多线程并发查询。
 */
VCT_API void* vct_kdtree_create(const float* points, int count);

/* 释放 KDTree 句柄 */
VCT_API void vct_kdtree_free(void* handle);

/*
 * 批量最近邻查询并取色（顶点色复制的核心）。
 *   handle       : vct_kdtree_create 返回的句柄
 *   colors       : N x 4 源颜色（调用方需保证稠密，缺失处填默认色）
 *   target_points: M x 3 目标顶点坐标
 *   target_count : M
 *   out_colors   : M x 4 输出颜色（调用方分配）
 * 返回 0 表示成功。
 */
VCT_API int vct_kdtree_query_colors(void* handle, const float* colors,
                                    const float* target_points, int target_count,
                                    float* out_colors);

/*
 * 批量最近邻查询（只要索引与距离，用于诊断/测试）。
 *   out_indices : M，最近邻在源点中的下标
 *   out_dist_sq : M，最近邻距离的平方（可为 NULL）
 */
VCT_API int vct_kdtree_query_indices(void* handle, const float* target_points,
                                     int target_count, int* out_indices,
                                     float* out_dist_sq);

/*
 * 物体匹配：为每个目标物体找出最佳源物体（相似度加权模型）。
 *
 * 各特征数组长度: s_* 为 s_count，t_* 为 t_count
 *   loc    : x3（位置）      dim : x3（包围盒尺寸）
 *   vol    : x1（体积）      vcount : x1（顶点数）
 *   radius : x1（包围球半径）
 *
 * 权重与阈值语义必须与 Python 版 calculate_similarity_score 完全一致。
 *
 * 输出:
 *   out_best_index : t_count，最佳源下标；无匹配为 -1
 *   out_best_score : t_count，最佳相似度 [0,1]；无匹配为 0
 * 返回 0 表示成功。
 */
VCT_API int vct_match_best(
    const float* s_loc, const float* s_dim, const float* s_vol,
    const float* s_vcount, const float* s_radius, int s_count,
    const float* t_loc, const float* t_dim, const float* t_vol,
    const float* t_vcount, const float* t_radius, int t_count,
    float distance_threshold, float position_decay_factor,
    float w_distance, float w_size, float w_volume, float w_vertex,
    float similarity_threshold,
    int* out_best_index, float* out_best_score);

#ifdef __cplusplus
}
#endif

#endif /* VCT_NATIVE_H */
