//! FFI 边界：裸指针与安全切片之间的转换。
//!
//! # 为什么这里允许 `unsafe`
//!
//! ctypes 传入的是裸指针，其长度信息由**另一个参数**携带。
//! 编译器无法验证两者一致，因此把裸指针转成切片是使用它的唯一途径，
//! 而这一步的安全性完全依赖调用方的契约。
//!
//! 这也是本 crate 根声明 `#![deny(unsafe_code)]`、却在此处
//! `#![allow(unsafe_code)]` 的原因：unsafe 收敛到本模块，
//! 其余代码（算法、类型、测试）全部由编译器保证安全。
//!
//! 若不这样做的后果：无法从 ctypes 边界接收任何裸指针数组，
//! 整个 crate 失去与 Python 通信的能力，等于无法完成移植。

#![allow(unsafe_code)]
// as_f64_slice（法线用）要到约束取色切片才被调用；
// 其余切片辅助函数也尚未被 kdtree_exports 全部用到。
#![allow(dead_code)]

use core::slice;

use crate::error::VctError;

pub mod cluster_exports;
pub mod kdtree_exports;
pub mod matching_exports;
pub mod pick_exports;

/// 按 `len` 个元素构造只读 `f32` 切片。
///
/// 长度为 0 时返回空切片，**不要求** `ptr` 非空——
/// 这与 Python 侧传入空 numpy 数组（其数据指针可能是任意值）的行为一致。
///
/// # Safety
///
/// 当 `len > 0` 时，调用方必须保证：
/// - `ptr` 非空；
/// - `ptr` 指向至少 `len` 个连续、可读、对齐正确的 `f32`；
/// - 在返回切片存活期间，调用方不会修改或释放这块内存。
pub unsafe fn as_f32_slice<'a>(ptr: *const f32, len: usize) -> Result<&'a [f32], VctError> {
    if len == 0 {
        return Ok(&[]);
    }
    if ptr.is_null() {
        return Err(VctError::NullInput);
    }
    // SAFETY: 调用方已承诺指针有效且指向至少 len 个元素；
    // 长度非零已由上面的分支排除，满足 from_raw_parts 的前提。
    Ok(slice::from_raw_parts(ptr, len))
}

/// 按 `len` 个元素构造只读 `f64` 切片。
///
/// 法线必须以 `f64` 传入：它归一化后直接参与夹角阈值判定，
/// `f32` 的有效数字不足以在阈值边界保持判定稳定。
///
/// # Safety
///
/// 当 `len > 0` 时，调用方必须保证 `ptr` 非空、指向至少 `len` 个
/// 连续可读且对齐正确的 `f64`，并在切片存活期间不修改或释放该内存。
pub unsafe fn as_f64_slice<'a>(ptr: *const f64, len: usize) -> Result<&'a [f64], VctError> {
    if len == 0 {
        return Ok(&[]);
    }
    if ptr.is_null() {
        return Err(VctError::NullInput);
    }
    // SAFETY: 同 as_f32_slice。
    Ok(slice::from_raw_parts(ptr, len))
}

/// 按 `len` 个元素构造可变 `i32` 切片。
///
/// # Safety
///
/// 当 `len > 0` 时，调用方必须保证 `ptr` 非空、指向至少 `len` 个
/// 连续可写且对齐正确的 `i32`，并在切片存活期间不并发访问该内存。
pub unsafe fn as_i32_slice_mut<'a>(ptr: *mut i32, len: usize) -> Result<&'a mut [i32], VctError> {
    if len == 0 {
        return Ok(&mut []);
    }
    if ptr.is_null() {
        return Err(VctError::NullInput);
    }
    // SAFETY: 调用方已承诺可写且容量足够；长度非零已由上面的分支排除。
    Ok(slice::from_raw_parts_mut(ptr, len))
}

/// 按 `len` 个元素构造可变 `f32` 切片。
///
/// 用于承接 FFI 的输出缓冲（如距离平方数组）。
///
/// # Safety
///
/// 当 `len > 0` 时，调用方必须保证 `ptr` 非空、指向至少 `len` 个
/// 连续可写且对齐正确的 `f32`，并在切片存活期间不并发访问该内存。
pub unsafe fn as_f32_slice_mut<'a>(ptr: *mut f32, len: usize) -> Result<&'a mut [f32], VctError> {
    if len == 0 {
        return Ok(&mut []);
    }
    if ptr.is_null() {
        return Err(VctError::NullInput);
    }
    // SAFETY: 调用方已承诺可写且容量足够；长度非零已由上面的分支排除。
    Ok(slice::from_raw_parts_mut(ptr, len))
}

/// 按 `len` 个元素构造可变 `f64` 切片。
///
/// 匹配得分以 `f64` 输出：得分要参与 0.7 这类阈值比较，
/// 用 `f32` 传输会在阈值边界引入舍入。
///
/// # Safety
///
/// 当 `len > 0` 时，调用方必须保证 `ptr` 非空、指向至少 `len` 个
/// 连续可写且对齐正确的 `f64`，并在切片存活期间不并发访问该内存。
pub unsafe fn as_f64_slice_mut<'a>(ptr: *mut f64, len: usize) -> Result<&'a mut [f64], VctError> {
    if len == 0 {
        return Ok(&mut []);
    }
    if ptr.is_null() {
        return Err(VctError::NullInput);
    }
    // SAFETY: 调用方已承诺可写且容量足够；长度非零已由上面的分支排除。
    Ok(slice::from_raw_parts_mut(ptr, len))
}

#[cfg(test)]
mod tests {
    use super::{as_f32_slice, as_f64_slice, as_i32_slice_mut};
    use crate::error::VctError;

    // 测试内允许 expect：规范 A5 约束的是库代码，不含 test。
    // 且这里的指针全部来自本地数组，长度必然匹配，expect 不会触发。

    #[test]
    fn empty_length_accepts_null_pointer() {
        // 空数组的指针值不可依赖，因此长度 0 时必须直接接受。
        let empty = unsafe { as_f32_slice(core::ptr::null(), 0) }.expect("长度为 0 时不应报错");
        assert_eq!(empty, []);
        let empty = unsafe { as_f64_slice(core::ptr::null(), 0) }.expect("长度为 0 时不应报错");
        assert_eq!(empty, []);
    }

    #[test]
    fn null_pointer_with_nonzero_length_is_rejected() {
        let got = unsafe { as_f32_slice(core::ptr::null(), 4) };
        assert_eq!(got.err(), Some(VctError::NullInput));
    }

    #[test]
    fn roundtrip_reads_back_values() {
        let data: [f32; 3] = [1.0, 2.0, 3.0];
        let got = unsafe { as_f32_slice(data.as_ptr(), data.len()) }
            .expect("指针来自本地数组，非空且长度匹配");
        assert_eq!(got, &[1.0, 2.0, 3.0]);
    }

    #[test]
    fn roundtrip_writes_back_values() {
        let mut data: [i32; 2] = [0, 0];
        let out = unsafe { as_i32_slice_mut(data.as_mut_ptr(), data.len()) }
            .expect("指针来自本地数组，非空且长度匹配");
        out[0] = 7;
        out[1] = 9;
        assert_eq!(data, [7, 9]);
    }
}
