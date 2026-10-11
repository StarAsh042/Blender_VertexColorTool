//! 相似度匹配的 C ABI 导出。
//!
//! 特征与参数都以 `#[repr(C)]` 结构体数组传入，Python 侧按同样的字段顺序
//! 声明 `ctypes.Structure` 即可直接解读，无需任何解析。

use crate::error::VctError;
use crate::matching::{best_match, MatchParams};
use crate::types::ObjectFeatures;

use super::{as_f64_slice_mut, as_i32_slice_mut};

/// 由调用方给出的结构体数组转成切片。
///
/// # Safety
///
/// `ptr` 必须指向至少 `len` 个连续、可读、正确对齐的 [`ObjectFeatures`]。
unsafe fn features_from<'a>(
    ptr: *const ObjectFeatures,
    len: usize,
) -> Result<&'a [ObjectFeatures], VctError> {
    if len == 0 {
        return Ok(&[]);
    }
    if ptr.is_null() {
        return Err(VctError::NullInput);
    }
    // SAFETY: 调用方已承诺指针有效且长度足够。
    Ok(unsafe { core::slice::from_raw_parts(ptr, len) })
}

/// 为每个目标物体找出得分最高的源物体。
///
/// 写出 `out_best_index`（源数组下标，无匹配为 `-1`）与 `out_best_score`。
/// 返回 `0` 表示成功，负值见 [`VctError::code`]。
///
/// # Safety
///
/// 调用方必须保证：`params` 指向一个可读的 [`MatchParams`]；
/// `s_features` 与 `t_features` 分别指向至少 `s_count` / `t_count` 个连续可读的
/// [`ObjectFeatures`]；两个输出数组各可写至少 `t_count` 个元素。
#[no_mangle]
pub unsafe extern "C" fn vct_match_best(
    s_features: *const ObjectFeatures,
    s_count: i32,
    t_features: *const ObjectFeatures,
    t_count: i32,
    params: *const MatchParams,
    out_best_index: *mut i32,
    out_best_score: *mut f64,
) -> i32 {
    if params.is_null() {
        return VctError::NullInput.code();
    }
    if s_count < 0 || t_count < 0 {
        return VctError::LengthMismatch {
            declared: if s_count < 0 { s_count } else { t_count },
            available: 0,
        }
        .code();
    }

    let targets = match unsafe { features_from(t_features, t_count as usize) } {
        Ok(s) => s,
        Err(e) => return e.code(),
    };
    if targets.is_empty() {
        return 0;
    }
    let sources = match unsafe { features_from(s_features, s_count as usize) } {
        Ok(s) => s,
        Err(e) => return e.code(),
    };
    // SAFETY: 调用方承诺 params 指向可读的一个 MatchParams。
    let p = unsafe { &*params };

    let idx_out = match unsafe { as_i32_slice_mut(out_best_index, targets.len()) } {
        Ok(s) => s,
        Err(e) => return e.code(),
    };
    let score_out = match unsafe { as_f64_slice_mut(out_best_score, targets.len()) } {
        Ok(s) => s,
        Err(e) => return e.code(),
    };

    for (i, t) in targets.iter().enumerate() {
        let Some((idx, score)) = best_match(sources, t, p) else {
            idx_out[i] = -1;
            score_out[i] = 0.0;
            continue;
        };
        idx_out[i] = idx as i32;
        score_out[i] = score;
    }
    0
}

#[cfg(test)]
mod tests {
    use super::vct_match_best;
    use crate::matching::MatchParams;
    use crate::types::ObjectFeatures;

    fn obj(x: f64, size: f64) -> ObjectFeatures {
        ObjectFeatures {
            location: [x, 0.0, 0.0],
            dimensions: [size, size, size],
            volume: size * size * size,
            vertex_count: 10.0,
            bounding_radius: size,
        }
    }

    fn params() -> MatchParams {
        MatchParams {
            distance_threshold: 50.0,
            position_decay_factor: 1.0,
            w_distance: 1.0,
            w_size: 1.0,
            w_volume: 1.0,
            w_vertex: 1.0,
            similarity_threshold: 0.0,
        }
    }

    #[test]
    fn picks_the_closer_source() {
        let sources = [obj(0.0, 1.0), obj(5.0, 1.0)];
        let targets = [obj(4.5, 1.0)];
        let mut idx: i32 = -9;
        let mut score: f64 = -1.0;
        let rc = unsafe {
            vct_match_best(
                sources.as_ptr(),
                2,
                targets.as_ptr(),
                1,
                &params(),
                &mut idx,
                &mut score,
            )
        };
        assert_eq!(rc, 0);
        assert_eq!(idx, 1, "应命中更近的那个源");
        assert!(score > 0.0 && score <= 1.0, "得分应在 (0,1]，实际 {score}");
    }

    #[test]
    fn null_params_are_rejected() {
        let sources = [obj(0.0, 1.0)];
        let targets = [obj(0.0, 1.0)];
        let mut idx: i32 = 0;
        let mut score: f64 = 0.0;
        let rc = unsafe {
            vct_match_best(
                sources.as_ptr(),
                1,
                targets.as_ptr(),
                1,
                core::ptr::null(),
                &mut idx,
                &mut score,
            )
        };
        assert_ne!(rc, 0, "空参数指针必须报错");
    }
}
