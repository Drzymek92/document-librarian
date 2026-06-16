import rpy2.robjects as ro

from scripts.logger import get_logger

logger = get_logger("gdrive_auth")

_authenticated = False


def authenticate_gdrive() -> None:
    global _authenticated
    if _authenticated:
        return

    logger.info("Authenticating with Google Drive...")

    # Try saved credentials first (gargle cache); only open browser if none found.
    r_code = """
    suppressMessages(library(googledrive))

    auth_success <- tryCatch({
      drive_auth(email = TRUE)
      TRUE
    }, error = function(e) {
      FALSE
    })

    if (!auth_success) {
      message("No saved credentials found - opening browser for authentication...")
      drive_auth()
    }
    """
    ro.r(r_code)
    _authenticated = True
    logger.info("GDrive authentication successful")
