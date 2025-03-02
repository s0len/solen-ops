#!/bin/bash
set -euo pipefail

# Function to log messages with timestamps
log() {
    echo "$(date +'%Y-%m-%d %H:%M:%S') - $1"
}

# Check if the database path is provided
if [[ $# -ne 1 ]]; then
    echo "Usage: $0 /path/to/database.db"
    exit 1
fi

DB_PATH="$1"
NEW_PAGE_SIZE=262144   # 256KB
NEW_CACHE_SIZE=-131072 # 128MB (adjust as needed)

log "Starting SQLite page size modification for: $DB_PATH"

# Verify the database file exists
if [[ ! -f "$DB_PATH" ]]; then
    log "Error: Database file does not exist at $DB_PATH"
    exit 1
fi

# Check the current page size
current_page_size=$(sqlite3 "$DB_PATH" 'PRAGMA page_size;')
log "Current page size: $current_page_size bytes"

if [[ "$current_page_size" -eq "$NEW_PAGE_SIZE" ]]; then
    log "Page size is already set to $NEW_PAGE_SIZE bytes. No changes needed."
    exit 0
fi

log "Changing page size to $NEW_PAGE_SIZE bytes..."

# Define new database path
NEW_DB_PATH="${DB_PATH}.new"

# Backup the original database
cp "$DB_PATH" "${DB_PATH}.bak"
log "Backup created at ${DB_PATH}.bak"

# Capture current permissions and ownership
owner=$(stat -c '%u' "$DB_PATH")
group=$(stat -c '%g' "$DB_PATH")
perms=$(stat -c '%a' "$DB_PATH")

# Create a new database with the desired page size
sqlite3 "$NEW_DB_PATH" "PRAGMA page_size = $NEW_PAGE_SIZE; VACUUM;"
log "New database created with page size $NEW_PAGE_SIZE bytes."

# Dump data from the old database and import into the new one
sqlite3 "$DB_PATH" .dump | sqlite3 "$NEW_DB_PATH"
log "Data migrated to the new database."

# Replace the old database with the new one
mv "$NEW_DB_PATH" "$DB_PATH"
log "Replaced old database with the new one."

# Restore original permissions and ownership
chown "$owner":"$group" "$DB_PATH"
chmod "$perms" "$DB_PATH"
log "Restored original permissions and ownership."

# Set the new cache size
sqlite3 "$DB_PATH" "PRAGMA cache_size = $NEW_CACHE_SIZE;"
log "Set cache size to $NEW_CACHE_SIZE."

# Optimize the new database
sqlite3 "$DB_PATH" 'VACUUM;'
log "Vacuumed the new database to optimize it."

log "SQLite page size modification completed successfully for $DB_PATH."
