use std::fmt;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlatformError {
    pub code: &'static str,
    pub message: String,
    pub retry: &'static str,
}

impl PlatformError {
    pub fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
            retry: "never",
        }
    }

    pub fn with_retry(mut self, retry: &'static str) -> Self {
        self.retry = retry;
        self
    }

    pub fn invalid_argument(message: impl Into<String>) -> Self {
        Self::new("INVALID_ARGUMENT", message)
    }

    pub fn schema_unsupported(message: impl Into<String>) -> Self {
        Self::new("SCHEMA_UNSUPPORTED", message)
    }

    pub fn session_unavailable(message: impl Into<String>) -> Self {
        Self::new("SESSION_UNAVAILABLE", message)
    }

    pub fn capability_not_implemented(message: impl Into<String>) -> Self {
        Self::new("CAPABILITY_NOT_IMPLEMENTED", message)
    }

    pub fn permission_denied(message: impl Into<String>) -> Self {
        Self::new("PERMISSION_DENIED", message)
    }

    pub fn target_lost(message: impl Into<String>) -> Self {
        Self::new("TARGET_LOST", message)
    }

    pub fn credential_unavailable(message: impl Into<String>) -> Self {
        Self::new("CREDENTIAL_UNAVAILABLE", message)
    }
}

impl fmt::Display for PlatformError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for PlatformError {}
