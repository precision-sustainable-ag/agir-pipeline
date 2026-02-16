# Standardized exit codes for all pipeline stages.
EXIT_SUCCESS = 0       # All images processed
EXIT_PARTIAL = 1       # Some images failed
EXIT_FAILURE = 2       # All images failed
EXIT_CONFIG_ERROR = 3  # Setup/config error

# Exit code to status string mapping for run reports
EXIT_CODE_TO_STATUS = {
    EXIT_SUCCESS: "success",
    EXIT_PARTIAL: "partial",
    EXIT_FAILURE: "failure",
    EXIT_CONFIG_ERROR: "failure",
}

# Standardized status codes
ITEM_OK = "ok"
ITEM_FAILED = "failed"
ITEM_SKIPPED = "skipped"
