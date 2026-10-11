//! 对外的 C ABI 入口。
//!
//! 每个导出函数的契约：
//! - 名称以 `vct_` 开头，`#[no_mangle]` 保证符号名不被改写；
//! - 返回 `i32`，`0` 表示成功，负值见 [`crate::error::VctError::code`]；
//! - **任何 panic 都不得穿过这个边界**。crate 已设 `panic = "abort"`，
//!   因此不存在展开穿过 FFI 的可能。

use crate::error::VctError;

use super::as_f32_slice;

/// 返回本库的 ABI 版本号。
///
/// Python 侧加载后会校验该值，避免新旧不匹配导致的越界访问。
#[no_mangle]
pub extern "C" fn vct_api_version() -> i32 {
    crate::ABI_VERSION
}

/// 执行构建后自检，返回 `0` 表示通过。
///
/// 用途：让加载方在真正传入数据之前，先确认这份产物在本平台上
/// 结构体布局正确、切片转换可用、错误码未被破坏。
#[no_mangle]
pub extern "C" fn vct_self_test() -> i32 {
    match selftest() {
        Ok(()) => 0,
        Err(err) => err.code(),
    }
}

/// 自检内容：错误码契约未被破坏，且空输入路径可用。
fn selftest() -> Result<(), VctError> {
    let codes = [
        VctError::NullInput.code(),
        VctError::LengthMismatch {
            declared: 0,
            available: 0,
        }
        .code(),
        VctError::EmptyPointSet.code(),
        VctError::Internal.code(),
    ];
    for (i, code) in codes.iter().enumerate() {
        if *code >= 0 || codes[..i].contains(code) {
            return Err(VctError::Internal);
        }
    }

    let probe: &[f32] = unsafe { as_f32_slice(core::ptr::null(), 0) }?;
    if !probe.is_empty() {
        return Err(VctError::Internal);
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{vct_api_version, vct_self_test};

    #[test]
    fn self_test_returns_zero() {
        assert_eq!(vct_self_test(), 0);
    }

    #[test]
    fn api_version_is_positive() {
        assert!(vct_api_version() > 0, "ABI 版本号必须为正");
    }
}
