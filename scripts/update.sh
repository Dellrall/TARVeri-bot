#!/usr/bin/env bash
# ==============================================================================
# TARVeri — Safe Backend Updater with Pre-Update DB Backup & Auto-Rollback
# Supports custom update streams/branches via CLI args or .env configuration.
# ==============================================================================

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_ROOT}"

# Load environment configuration if available (safely without executing unquoted spaces)
if [ -f "${PROJECT_ROOT}/.env" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        # Trim leading whitespace
        line="${line#"${line%%[![:space:]]*}"}"
        # Skip empty lines and comments
        [[ -z "$line" || "$line" =~ ^# ]] && continue
        if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=[[:space:]]*(.*)$ ]]; then
            key="${BASH_REMATCH[1]}"
            val="${BASH_REMATCH[2]}"
            # Strip surrounding quotes if present, along with trailing comments
            if [[ "$val" =~ ^\"(.*)\"[[:space:]]*(#.*)?$ ]]; then
                val="${BASH_REMATCH[1]}"
            elif [[ "$val" =~ ^\x27(.*)\x27[[:space:]]*(#.*)?$ ]]; then
                val="${BASH_REMATCH[1]}"
            else
                val="${val%%#*}"
                val="${val%"${val##*[![:space:]]}"}"
            fi
            export "$key=$val"
        fi
    done < "${PROJECT_ROOT}/.env"
fi

# Configuration
VENV_DIR="${TARVERI_VENV_DIR:-${PROJECT_ROOT}/.venv}"
PYTHON_BIN="${TARVERI_PYTHON_BIN:-${VENV_DIR}/bin/python}"
PYTEST_BIN="${TARVERI_PYTEST_BIN:-${VENV_DIR}/bin/pytest}"
PIP_BIN="${TARVERI_PIP_BIN:-${VENV_DIR}/bin/pip}"
BACKUP_DIR="${TARVERI_BACKUP_DIR:-${PROJECT_ROOT}/backups}"
DB_PATH="${TARVERI_DB_PATH:-tarveri.db}"
SERVICE_NAME="${TARVERI_SERVICE_NAME:-tarveri}"

# Colors for terminal output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Parse CLI arguments
CHECK_ONLY=false
CLI_STREAM=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check)
            CHECK_ONLY=true
            shift
            ;;
        --stream|--branch)
            CLI_STREAM="$2"
            shift 2
            ;;
        -*)
            log_error "Unknown option: $1"
            echo "Usage: $0 [--check] [--stream <branch_name>] [<branch_name>]"
            exit 1
            ;;
        *)
            if [ -z "${CLI_STREAM}" ]; then
                CLI_STREAM="$1"
            fi
            shift
            ;;
    esac
done

# Verify virtual environment
if [ ! -d "${VENV_DIR}" ] || [ ! -x "${PYTHON_BIN}" ]; then
    log_error "Virtual environment not found at ${VENV_DIR}. Please set it up first."
    exit 1
fi

# Check if git is installed
if ! command -v git >/dev/null 2>&1; then
    log_error "Git is not installed on this system. Please install git (e.g. 'sudo apt install git') to enable automated updates."
    exit 1
fi

DEFAULT_REPO_URL="https://github.com/TARBot-OSS/TARVeri-bot.git"
REPO_URL="${TARVERI_REPO_URL:-${DEFAULT_REPO_URL}}"

# Auto-initialize git if .git directory is missing
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    log_warn "No .git repository found in ${PROJECT_ROOT}."
    log_info "Initializing git tracking from ${REPO_URL}..."
    git init --quiet
    git remote add origin "${REPO_URL}" 2>/dev/null || git remote set-url origin "${REPO_URL}"
    log_info "Fetching upstream branch metadata..."
    git fetch origin --quiet || true
fi

# Auto-migrate legacy remote URL from personal account to TARBot-OSS org
CURRENT_REMOTE_URL="$(git remote get-url origin 2>/dev/null || true)"
if echo "${CURRENT_REMOTE_URL}" | grep -Eq "Dellrall/(Student-verifier|TARVeri)"; then
    if echo "${CURRENT_REMOTE_URL}" | grep -q "^git@"; then
        MIGRATED_REMOTE_URL="git@github.com:TARBot-OSS/TARVeri-bot.git"
    else
        MIGRATED_REMOTE_URL="https://github.com/TARBot-OSS/TARVeri-bot.git"
    fi
    log_info "Migrating legacy remote origin (${CURRENT_REMOTE_URL}) to: ${MIGRATED_REMOTE_URL}"
    git remote set-url origin "${MIGRATED_REMOTE_URL}"
fi

# Fetch and prune remote tracking references
log_info "Syncing remote metadata (pruning deleted branches)..."
git fetch --prune origin --quiet || true

# Determine update stream (Priority: CLI argument > .env variable > current upstream/HEAD)
CONFIGURED_STREAM="${CLI_STREAM:-${TARVERI_UPDATE_STREAM:-${TARVERI_UPDATE_BRANCH:-auto}}}"
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")"
if [ "${CURRENT_BRANCH}" = "HEAD" ]; then
    CURRENT_BRANCH="main"
fi

if [ -n "${CONFIGURED_STREAM}" ] && [ "${CONFIGURED_STREAM}" != "auto" ]; then
    TARGET_BRANCH="${CONFIGURED_STREAM#origin/}"
    TARGET_REMOTE="origin/${TARGET_BRANCH}"
else
    # Check if upstream tracking branch exists and is valid on origin
    UPSTREAM_TRACKING="$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null || true)"
    if [ -n "${UPSTREAM_TRACKING}" ] && git rev-parse --verify --quiet "${UPSTREAM_TRACKING}" >/dev/null 2>&1; then
        TARGET_REMOTE="${UPSTREAM_TRACKING}"
        TARGET_BRANCH="${TARGET_REMOTE#origin/}"
    else
        # If current branch has no valid remote upstream or remote was pruned, fallback to origin/main
        if [ "${CURRENT_BRANCH}" != "main" ]; then
            log_warn "Current branch '${CURRENT_BRANCH}' does not exist on remote 'origin' (merged or deleted)."
            log_info "Automatically redirecting update stream to 'origin/main'..."
        fi
        TARGET_REMOTE="origin/main"
        TARGET_BRANCH="main"
    fi
fi

# Verify target remote ref exists on origin
if ! git rev-parse --verify --quiet "${TARGET_REMOTE}" >/dev/null 2>&1; then
    if [ "${TARGET_BRANCH}" != "main" ]; then
        log_warn "Target remote branch '${TARGET_REMOTE}' was not found on origin."
        log_info "Falling back to default stream 'origin/main'..."
        TARGET_REMOTE="origin/main"
        TARGET_BRANCH="main"
        git fetch origin main --quiet || true
    fi
fi

log_info "Target update stream: ${TARGET_REMOTE} (Local branch: ${CURRENT_BRANCH})"

LOCAL_HASH="$(git rev-parse HEAD 2>/dev/null || echo "uninitialized")"
REMOTE_HASH="$(git rev-parse "${TARGET_REMOTE}" 2>/dev/null || true)"

if [ -z "${REMOTE_HASH}" ]; then
    log_error "Remote branch '${TARGET_REMOTE}' not found on remote. Check branch name or network connection."
    exit 1
fi

if [ "${LOCAL_HASH}" != "uninitialized" ]; then
    BEHIND_COUNT="$(git rev-list --count HEAD.."${TARGET_REMOTE}" 2>/dev/null || echo "0")"
    if [ "${LOCAL_HASH}" = "${REMOTE_HASH}" ] && [ "${CURRENT_BRANCH}" = "${TARGET_BRANCH}" ]; then
        log_success "TARVeri is already on the latest version of stream '${TARGET_REMOTE}' (${LOCAL_HASH:0:7})."
        if [ "${CHECK_ONLY}" = true ]; then
            exit 0
        fi
    fi

    if [ "${BEHIND_COUNT}" -gt 0 ] || [ "${CURRENT_BRANCH}" != "${TARGET_BRANCH}" ]; then
        log_warn "Upstream '${TARGET_REMOTE}' is ${BEHIND_COUNT} commit(s) ahead (${LOCAL_HASH:0:7} -> ${REMOTE_HASH:0:7})."
    fi
else
    BEHIND_COUNT="all"
    log_warn "Repository was not previously git-tracked. Ready to sync with '${TARGET_REMOTE}' (${REMOTE_HASH:0:7})."
fi

if [ "${CHECK_ONLY}" = true ]; then
    if [ "${BEHIND_COUNT}" = "all" ] || [ "${BEHIND_COUNT}" -gt 0 ] || [ "${CURRENT_BRANCH}" != "${TARGET_BRANCH}" ]; then
        echo -e "\n🔔 ${YELLOW}Update available!${NC} Run './scripts/update.sh ${TARGET_BRANCH}' on this server to apply."
    fi
    exit 0
fi

# ------------------------------------------------------------------------------
# STEP 1: Pre-Update Database Backup
# ------------------------------------------------------------------------------
UPDATES_BACKUP_DIR="${BACKUP_DIR}/updates"
mkdir -p "${UPDATES_BACKUP_DIR}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
SNAPSHOT_PATH="${UPDATES_BACKUP_DIR}/tarveri_pre_update_${TIMESTAMP}.db"

log_info "[1/5] Creating pre-update database backup in ${UPDATES_BACKUP_DIR}..."
if [ -f "${DB_PATH}" ]; then
    ${PYTHON_BIN} -c "
import os, sqlite3, sys
try:
    src = sqlite3.connect('${DB_PATH}')
    dst = sqlite3.connect('${SNAPSHOT_PATH}')
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    print('Snapshot created successfully.')
except Exception as e:
    print(f'Backup error: {e}', file=sys.stderr)
    sys.exit(1)

# Rotate update backups keeping strictly the 5 most recent in updates/
updates_dir = '${UPDATES_BACKUP_DIR}'
max_update_backups = 5
if os.path.exists(updates_dir):
    files = [
        os.path.join(updates_dir, f)
        for f in os.listdir(updates_dir)
        if os.path.isfile(os.path.join(updates_dir, f)) and f.endswith('.db')
    ]
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    if len(files) > max_update_backups:
        for old_f in files[max_update_backups:]:
            try:
                os.remove(old_f)
                print(f'Pruned older pre-update snapshot: {os.path.basename(old_f)}')
            except Exception:
                pass
"
    log_success "Database snapshot saved to ${SNAPSHOT_PATH} (retained up to 5 most recent in updates/)"
else
    log_info "No existing database file at ${DB_PATH}. Skipping backup."
fi

# ------------------------------------------------------------------------------
# STEP 2: Pull Upstream Changes & Switch Stream If Requested
# ------------------------------------------------------------------------------
log_info "[2/5] Updating code to stream ${TARGET_REMOTE}..."
PREV_BRANCH="${CURRENT_BRANCH}"
PREV_COMMIT="${LOCAL_HASH}"

rollback() {
    log_error "Update failed! Initiating rollback..."
    if [ "${PREV_COMMIT}" != "uninitialized" ]; then
        git checkout "${PREV_BRANCH}" 2>/dev/null || true
        git reset --hard "${PREV_COMMIT}" 2>/dev/null || true
    fi
    
    if [ -f "${SNAPSHOT_PATH}" ] && [ -f "${DB_PATH}" ]; then
        log_info "Restoring database from ${SNAPSHOT_PATH}..."
        cp -f "${SNAPSHOT_PATH}" "${DB_PATH}"
    fi
    
    log_warn "Rollback complete."
    exit 1
}

# Trap unexpected errors to trigger rollback
trap rollback ERR

if [ "${LOCAL_HASH}" = "uninitialized" ]; then
    log_info "Linking working tree to '${TARGET_REMOTE}'..."
    git checkout -B "${TARGET_BRANCH}" "${TARGET_REMOTE}"
else
    if [ "${CURRENT_BRANCH}" != "${TARGET_BRANCH}" ]; then
        log_info "Switching branch: '${CURRENT_BRANCH}' -> '${TARGET_BRANCH}'..."
        if git show-ref --verify --quiet "refs/heads/${TARGET_BRANCH}"; then
            git checkout "${TARGET_BRANCH}"
        else
            git checkout -b "${TARGET_BRANCH}" --track "${TARGET_REMOTE}"
        fi
    fi
    git pull origin "${TARGET_BRANCH}" --ff-only
fi


NEW_COMMIT="$(git rev-parse HEAD)"
log_success "Successfully pulled code (now at commit ${NEW_COMMIT:0:7} on branch '${TARGET_BRANCH}')."

# ------------------------------------------------------------------------------
# STEP 3: Sync Python Dependencies
# ------------------------------------------------------------------------------
log_info "[3/5] Updating dependencies..."
if [ ! -f "${PIP_BIN}" ] || [ ! -x "${PYTHON_BIN}" ]; then
    log_warn "Virtual environment not found at '${VENV_DIR}'. Initializing a fresh virtualenv..."
    python3 -m venv "${VENV_DIR}"
    "${PIP_BIN}" install --upgrade pip --quiet
fi

${PIP_BIN} install -r requirements.txt --upgrade --quiet

# ------------------------------------------------------------------------------
# STEP 4: Run Automated Pre-flight Tests
# ------------------------------------------------------------------------------
log_info "[4/5] Running automated pre-flight test suite..."
if [ -x "${PYTEST_BIN}" ]; then
    ${PYTEST_BIN} -q
    log_success "All pre-flight tests passed successfully!"
else
    log_warn "pytest not found in virtualenv. Running syntax compile check instead..."
    ${PYTHON_BIN} -m compileall -q tarveri tarveri_bot.py
fi

# Disable error trap now that tests passed
trap - ERR

# ------------------------------------------------------------------------------
# STEP 5: Restart Service
# ------------------------------------------------------------------------------
log_info "[5/5] Checking and restarting service '${SERVICE_NAME}'..."

RESTARTED=false
if command -v systemctl >/dev/null 2>&1; then
    # 1. Check user service (systemctl --user)
    if systemctl --user is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
        log_info "Restarting systemd user service '${SERVICE_NAME}'..."
        systemctl --user restart "${SERVICE_NAME}"
        log_success "Systemd user service '${SERVICE_NAME}' restarted."
        RESTARTED=true
    fi
    # 2. Check system-wide service
    if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
        log_info "Restarting system-wide service '${SERVICE_NAME}'..."
        if [ "$EUID" -eq 0 ]; then
            systemctl restart "${SERVICE_NAME}"
        else
            sudo systemctl restart "${SERVICE_NAME}" 2>/dev/null || systemctl restart "${SERVICE_NAME}"
        fi
        log_success "System service '${SERVICE_NAME}' restarted."
        RESTARTED=true
    fi
fi

if [ "${RESTARTED}" = false ]; then
    log_info "No active systemd service '${SERVICE_NAME}' detected."
    log_info "If running manually (e.g. in tmux or screen), restart your bot process now:"
    echo -e "      ${GREEN}python tarveri_bot.py${NC}"
fi

echo -e "\n=============================================================================="
log_success "TARVeri update on stream '${TARGET_REMOTE}' (${NEW_COMMIT:0:7}) completed cleanly!"
log_info "Pre-update backup preserved at: ${SNAPSHOT_PATH}"
echo -e "==============================================================================\n"

