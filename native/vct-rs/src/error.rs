//! ABI 错误类型与返回码。
//!
//! # 约定
//!
//! 所有导出函数返回 `i32`：`0` 表示成功，负值表示失败。
//! 调用方（Python 侧）只需判断是否为 0，不必理解具体错误。

/// 内核向调用方报告失败的方式。
///
/// 本枚举是 `#[non_exhaustive]` 的：将来新增错误变体不会破坏下游的匹配。
/// 但**新增变体必须追加在末尾**，因为返回码是对外契约。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub enum VctError {
    /// 必需的指针参数为空，而长度非零。
    NullInput,
    /// 数组长度与同组的计数参数不一致。
    LengthMismatch {
        /// 计数参数声明的长度。
        declared: i32,
        /// 实际可用的元素数。
        available: usize,
    },
    /// 点集为空，无法构建空间索引。
    EmptyPointSet,
    /// 内部不变量被破坏（例如索引越界）。出现即视为内核缺陷。
    Internal,
}

impl VctError {
    /// 返回本错误对应的 ABI 返回码。
    ///
    /// 返回码一经发布即是对外契约，只可追加，不可重排。
    #[must_use]
    pub const fn code(self) -> i32 {
        match self {
            Self::NullInput => -1,
            Self::LengthMismatch { .. } => -2,
            Self::EmptyPointSet => -3,
            Self::Internal => -4,
        }
    }
}

impl core::fmt::Display for VctError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            Self::NullInput => write!(f, "必需的指针参数为空"),
            Self::LengthMismatch {
                declared,
                available,
            } => write!(f, "长度不一致：声明 {declared}，实际 {available}"),
            Self::EmptyPointSet => write!(f, "点集为空，无法建树"),
            Self::Internal => write!(f, "内部不变量被破坏"),
        }
    }
}

impl std::error::Error for VctError {}

#[cfg(test)]
mod tests {
    use super::VctError;

    #[test]
    fn error_codes_are_stable() {
        // 这些数字是对外契约，改动会让已发布的 Python 侧误判失败原因。
        assert_eq!(VctError::NullInput.code(), -1);
        assert_eq!(
            VctError::LengthMismatch {
                declared: 3,
                available: 5
            }
            .code(),
            -2
        );
        assert_eq!(VctError::EmptyPointSet.code(), -3);
        assert_eq!(VctError::Internal.code(), -4);
    }

    #[test]
    fn codes_are_all_negative_and_distinct() {
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
            assert!(*code < 0, "错误码必须为负：{code}");
            assert!(!codes[..i].contains(code), "错误码重复：{code}");
        }
    }
}
