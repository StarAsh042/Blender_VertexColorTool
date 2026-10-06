/*
 * 顶点色复制工具 —— 原生加速核心实现
 *
 * 两个热点内核:
 *   1. KDTree 最近邻取色 —— 替代 Python 逐顶点循环 + 逐元素 RNA 写入
 *   2. 物体匹配相似度   —— 替代 O(T×S) 的 Python 双重循环
 *
 * 数值语义必须与 Python 版严格一致，否则优化会变成「静默改行为」。
 * 对应 Python 实现:
 *   core/matching.py::calculate_similarity_score
 *   core/vertex_color_ops.py 的 KDTree 取色路径
 */

#include "vct_native.h"

#include <algorithm>
#include <cfloat>
#include <cmath>
#include <cstring>
#include <limits>
#include <thread>
#include <vector>

namespace {

// ---------------------------------------------------------------------------
// KDTree（构建时按最大跨度轴取中位数分割，查询为标准分支定界）
// ---------------------------------------------------------------------------

struct KDNode {
    int point;   // 点在源数组中的下标
    int axis;    // 该节点的分割轴
    int left;    // 左子节点下标，-1 为空
    int right;   // 右子节点下标，-1 为空
};

class KDTree {
public:
    bool build(const float* points, int count) {
        points_ = points;
        count_ = count;
        root_ = -1;
        nodes_.clear();
        order_.clear();

        if (count <= 0 || !points) {
            count_ = 0;
            return true;
        }

        order_.resize(count);
        for (int i = 0; i < count; ++i) {
            order_[i] = i;
        }

        nodes_.reserve(count);
        root_ = build_range(0, count);
        return true;
    }

    // 返回最近邻下标；无点时返回 -1
    int nearest(const float* query, float* out_dist_sq) const {
        int best = -1;
        float best_d2 = std::numeric_limits<float>::max();
        if (root_ >= 0) {
            search(root_, query, best, best_d2);
        }
        if (out_dist_sq) {
            *out_dist_sq = best_d2;
        }
        return best;
    }

    int size() const { return count_; }

private:
    int build_range(int lo, int hi) {
        if (lo >= hi) {
            return -1;
        }

        // 选跨度最大的轴做分割，避免退化成长链
        float low[3] = {FLT_MAX, FLT_MAX, FLT_MAX};
        float high[3] = {-FLT_MAX, -FLT_MAX, -FLT_MAX};
        for (int i = lo; i < hi; ++i) {
            const float* p = points_ + 3 * order_[i];
            for (int a = 0; a < 3; ++a) {
                low[a] = std::min(low[a], p[a]);
                high[a] = std::max(high[a], p[a]);
            }
        }

        int axis = 0;
        float best_span = -1.0f;
        for (int a = 0; a < 3; ++a) {
            const float span = high[a] - low[a];
            if (span > best_span) {
                best_span = span;
                axis = a;
            }
        }

        const int mid = lo + (hi - lo) / 2;
        const int ax = axis;
        std::nth_element(
            order_.begin() + lo, order_.begin() + mid, order_.begin() + hi,
            [this, ax](int a, int b) {
                return points_[3 * a + ax] < points_[3 * b + ax];
            });

        // 注意: 递归会继续 push_back，nodes_ 可能重新分配，
        // 因此这里只能用下标索引，不能持有引用。
        const int node_index = static_cast<int>(nodes_.size());
        nodes_.push_back(KDNode{order_[mid], axis, -1, -1});

        const int left = build_range(lo, mid);
        const int right = build_range(mid + 1, hi);
        nodes_[node_index].left = left;
        nodes_[node_index].right = right;
        return node_index;
    }

    void search(int node_index, const float* q, int& best, float& best_d2) const {
        const KDNode& node = nodes_[node_index];
        const float* p = points_ + 3 * node.point;

        const float dx = p[0] - q[0];
        const float dy = p[1] - q[1];
        const float dz = p[2] - q[2];
        const float d2 = dx * dx + dy * dy + dz * dz;

        if (d2 < best_d2) {
            best_d2 = d2;
            best = node.point;
        }

        const int axis = node.axis;
        const float diff = q[axis] - p[axis];
        const int near_child = (diff < 0.0f) ? node.left : node.right;
        const int far_child = (diff < 0.0f) ? node.right : node.left;

        if (near_child >= 0) {
            search(near_child, q, best, best_d2);
        }
        // 只有超平面距离可能更近时才需要搜索远端
        if (far_child >= 0 && diff * diff < best_d2) {
            search(far_child, q, best, best_d2);
        }
    }

    std::vector<KDNode> nodes_;
    std::vector<int> order_;
    const float* points_ = nullptr;
    int count_ = 0;
    int root_ = -1;
};

// ---------------------------------------------------------------------------
// 轻量并行工具（std::thread，不依赖 OpenMP，便于交叉编译）
// ---------------------------------------------------------------------------

int thread_count() {
    unsigned hc = std::thread::hardware_concurrency();
    if (hc == 0) {
        hc = 1;
    }
    return static_cast<int>(std::min(hc, 8u));
}

// 把 [0, count) 切成若干块并行执行 fn(lo, hi)。
// 任务量不足 min_per_thread 时直接单线程执行，避免线程创建反而更慢。
template <typename Fn>
void parallel_for(int count, int min_per_thread, Fn&& fn) {
    if (count <= 0) {
        return;
    }

    int threads = thread_count();
    if (threads <= 1 || count < min_per_thread) {
        fn(0, count);
        return;
    }

    threads = std::min(threads, std::max(1, count / min_per_thread));
    if (threads <= 1) {
        fn(0, count);
        return;
    }

    const int chunk = (count + threads - 1) / threads;
    std::vector<std::thread> pool;
    pool.reserve(threads);

    for (int t = 0; t < threads; ++t) {
        const int lo = t * chunk;
        const int hi = std::min(count, lo + chunk);
        if (lo >= hi) {
            break;
        }
        pool.emplace_back([&fn, lo, hi]() {
            // 线程内不允许异常逃逸，否则会触发 std::terminate
            try {
                fn(lo, hi);
            } catch (...) {
                // 内核不使用会抛异常的容器，这里只是兜底
            }
        });
    }

    for (std::thread& worker : pool) {
        worker.join();
    }
}

}  // namespace

// ---------------------------------------------------------------------------
// 导出接口
// ---------------------------------------------------------------------------

extern "C" VCT_API int vct_api_version(void) {
    return VCT_API_VERSION;
}

extern "C" VCT_API void* vct_kdtree_create(const float* points, int count) {
    if (!points || count <= 0) {
        return nullptr;
    }
    try {
        KDTree* tree = new KDTree();
        if (!tree->build(points, count)) {
            delete tree;
            return nullptr;
        }
        return static_cast<void*>(tree);
    } catch (...) {
        return nullptr;
    }
}

extern "C" VCT_API void vct_kdtree_free(void* handle) {
    delete static_cast<KDTree*>(handle);
}

extern "C" VCT_API int vct_kdtree_query_colors(void* handle, const float* colors,
                                               const float* target_points,
                                               int target_count, float* out_colors) {
    KDTree* tree = static_cast<KDTree*>(handle);
    if (!tree || !colors || !target_points || !out_colors || target_count < 0) {
        return -1;
    }
    if (target_count == 0) {
        return 0;
    }

    // 与 Python 版一致：找不到最近邻时使用白色
    const float fallback[4] = {1.0f, 1.0f, 1.0f, 1.0f};

    parallel_for(target_count, 4096, [&](int lo, int hi) {
        for (int i = lo; i < hi; ++i) {
            float dist_sq = 0.0f;
            const int idx = tree->nearest(target_points + 3 * i, &dist_sq);
            if (idx < 0) {
                std::memcpy(out_colors + 4 * i, fallback, sizeof(fallback));
            } else {
                std::memcpy(out_colors + 4 * i, colors + 4 * idx, 4 * sizeof(float));
            }
        }
    });

    return 0;
}

extern "C" VCT_API int vct_kdtree_query_indices(void* handle, const float* target_points,
                                                int target_count, int* out_indices,
                                                float* out_dist_sq) {
    KDTree* tree = static_cast<KDTree*>(handle);
    if (!tree || !target_points || !out_indices || target_count < 0) {
        return -1;
    }

    parallel_for(target_count, 4096, [&](int lo, int hi) {
        for (int i = lo; i < hi; ++i) {
            float dist_sq = 0.0f;
            out_indices[i] = tree->nearest(target_points + 3 * i, &dist_sq);
            if (out_dist_sq) {
                out_dist_sq[i] = dist_sq;
            }
        }
    });

    return 0;
}

extern "C" VCT_API int vct_match_best(
    const float* s_loc, const float* s_dim, const float* s_vol,
    const float* s_vcount, const float* s_radius, int s_count,
    const float* t_loc, const float* t_dim, const float* t_vol,
    const float* t_vcount, const float* t_radius, int t_count,
    float distance_threshold, float position_decay_factor,
    float w_distance, float w_size, float w_volume, float w_vertex,
    float similarity_threshold,
    int* out_best_index, float* out_best_score) {

    if (!s_loc || !s_dim || !s_vol || !s_vcount || !s_radius || s_count <= 0) {
        return -1;
    }
    if (!t_loc || !t_dim || !t_vol || !t_vcount || !t_radius || t_count < 0) {
        return -1;
    }
    if (!out_best_index || !out_best_score) {
        return -1;
    }
    if (t_count == 0) {
        return 0;
    }

    // 与 Python 版一致：权重先求和再归一化（和为 0 时不归一化）
    const double total_weight =
        static_cast<double>(w_distance) + w_size + w_volume + w_vertex;
    const bool normalize = total_weight > 0.001;
    const double inv_total_weight = normalize ? (1.0 / total_weight) : 1.0;

    const double max_distance = static_cast<double>(distance_threshold);
    const double max_distance_sq = max_distance * max_distance;

    const double wd = w_distance;
    const double ws = w_size;
    const double wv = w_volume;
    const double wvc = w_vertex;
    const double decay = position_decay_factor;
    const double threshold = similarity_threshold;

    // 注意: 内核内部统一使用 double 计算。
    // Python 版 calculate_similarity_score 使用 Python float（双精度），
    // 若这里用 float32，会在阈值边界上产生足以翻转匹配结果的偏差。
    // 输入数组本身是 float32（Blender 的 mathutils 也是单精度），因此不损失信息。
    parallel_for(t_count, 256, [&](int lo, int hi) {
        for (int t = lo; t < hi; ++t) {
            const float* tl = t_loc + 3 * t;
            const float* td = t_dim + 3 * t;
            const double tv = static_cast<double>(t_vol[t]);
            const double tvc = static_cast<double>(t_vcount[t]);
            const double tr = static_cast<double>(t_radius[t]);

            int best_index = -1;
            double best_score = 0.0;

            for (int s = 0; s < s_count; ++s) {
                const float* sl = s_loc + 3 * s;
                const double dx = static_cast<double>(sl[0]) - tl[0];
                const double dy = static_cast<double>(sl[1]) - tl[1];
                const double dz = static_cast<double>(sl[2]) - tl[2];
                const double dist_sq = dx * dx + dy * dy + dz * dz;

                // 距离过远直接跳过，与 Python 版的提前返回等价
                if (dist_sq > max_distance_sq) {
                    continue;
                }

                const double distance = std::sqrt(dist_sq);
                const double avg_size = static_cast<double>(s_radius[s]) + tr;
                double distance_score;
                if (avg_size > 0.001) {
                    distance_score = std::exp(-(distance / avg_size) * decay);
                } else {
                    distance_score = 1.0;
                }

                // 尺寸相似度
                const float* sd = s_dim + 3 * s;
                double size_diff = 0.0;
                for (int a = 0; a < 3; ++a) {
                    const double m = std::max(static_cast<double>(sd[a]),
                                              static_cast<double>(td[a]));
                    if (m > 0.001) {
                        size_diff += std::fabs(static_cast<double>(sd[a]) - td[a]) / m;
                    }
                }
                const double size_score = 1.0 - size_diff / 3.0;

                // 体积相似度
                const double sv = static_cast<double>(s_vol[s]);
                const double max_vol = std::max(std::max(sv, tv), 0.001);
                const double volume_score = 1.0 - std::fabs(sv - tv) / max_vol;

                // 顶点数相似度
                const double svc = static_cast<double>(s_vcount[s]);
                const double max_vc = std::max(std::max(svc, tvc), 1.0);
                const double vertex_score = 1.0 - std::fabs(svc - tvc) / max_vc;

                double total = distance_score * wd + size_score * ws +
                               volume_score * wv + vertex_score * wvc;
                if (normalize) {
                    total *= inv_total_weight;
                }

                // 先归一化再钳制，顺序与 Python 版一致
                if (total < 0.0) {
                    total = 0.0;
                } else if (total > 1.0) {
                    total = 1.0;
                }

                if (total < threshold) {
                    continue;
                }
                if (total > best_score) {
                    best_score = total;
                    best_index = s;
                }
            }

            out_best_index[t] = best_index;
            out_best_score[t] = static_cast<float>(best_score);
        }
    });

    return 0;
}

extern "C" VCT_API int vct_self_test(void) {
    // 1. KDTree 最近邻正确性
    const float points[9] = {0.0f, 0.0f, 0.0f, 1.0f, 0.0f, 0.0f, 0.0f, 1.0f, 0.0f};
    const float query[3] = {0.9f, 0.05f, 0.0f};

    void* handle = vct_kdtree_create(points, 3);
    if (!handle) {
        return -1;
    }
    int index = -1;
    float dist_sq = -1.0f;
    if (vct_kdtree_query_indices(handle, query, 1, &index, &dist_sq) != 0) {
        vct_kdtree_free(handle);
        return -2;
    }
    vct_kdtree_free(handle);
    if (index != 1) {
        return -3;
    }

    // 2. 取色正确性
    const float colors[12] = {1.0f, 0.0f, 0.0f, 1.0f, 0.0f, 1.0f, 0.0f, 1.0f,
                              0.0f, 0.0f, 1.0f, 1.0f};
    void* handle2 = vct_kdtree_create(points, 3);
    if (!handle2) {
        return -4;
    }
    float out_color[4] = {0.0f, 0.0f, 0.0f, 0.0f};
    const int rc_color = vct_kdtree_query_colors(handle2, colors, query, 1, out_color);
    vct_kdtree_free(handle2);
    if (rc_color != 0) {
        return -5;
    }
    if (std::fabs(out_color[1] - 1.0f) > 1e-6f) {
        return -6;
    }

    // 3. 匹配内核正确性（权重只给距离 → 应匹配到最近的源）
    const float s_loc[3] = {0.0f, 0.0f, 0.0f};
    const float s_dim[3] = {1.0f, 1.0f, 1.0f};
    const float s_vol[1] = {1.0f};
    const float s_vc[1] = {8.0f};
    const float s_rad[1] = {0.5f};
    const float t_loc[3] = {0.1f, 0.0f, 0.0f};
    const float t_dim[3] = {1.0f, 1.0f, 1.0f};
    const float t_vol[1] = {1.0f};
    const float t_vc[1] = {8.0f};
    const float t_rad[1] = {0.5f};

    int best_index = -99;
    float best_score = -1.0f;
    const int rc_match = vct_match_best(
        s_loc, s_dim, s_vol, s_vc, s_rad, 1, t_loc, t_dim, t_vol, t_vc, t_rad, 1,
        1000.0f, 0.5f, 1.0f, 0.0f, 0.0f, 0.0f, 0.0f, &best_index, &best_score);
    if (rc_match != 0) {
        return -7;
    }
    if (best_index != 0) {
        return -8;
    }
    if (!(best_score > 0.0f && best_score <= 1.0f)) {
        return -9;
    }

    return 0;
}
