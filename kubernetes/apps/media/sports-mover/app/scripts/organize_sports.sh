#!/bin/bash

# Set source and destination directories
SRC_DIR="/data/torrents/sport"
DEST_DIR="/data/media/sport"

# Function to organize motorcycle racing files
organize_moto() {
    local file="$1"
    local filename=$(basename "$file")

    # Handle both files and directories
    if [[ -d "$file" ]]; then
        # For directories, look for matching video files inside
        find "$file" -type f -name "*.mkv" -o -name "*.mp4" | while read video_file; do
            organize_moto "$video_file"
        done
        return 0
    fi

    # Extract moto class, year, round, location and session type using regex
    if [[ $filename =~ (Moto(?:GP|[23]))\.([0-9]{4})\.Round([0-9]{2})\.([^\.]+)\.([^\.]+) ]]; then
        local moto_class="${BASH_REMATCH[1]}"
        local year="${BASH_REMATCH[2]}"
        local round="${BASH_REMATCH[3]}"
        local location="${BASH_REMATCH[3]%% *}"
        local session="${BASH_REMATCH[5]%%.*}" # Extract session name before first dot
        local extension="${filename##*.}"

        # Clean up location if needed
        location="${BASH_REMATCH[4]}"

        # Determine episode number based on session type
        local episode=""
        case "$session" in
        "Practice"*"1" | "Practice"*"One") episode="1" ;;
        "Practice"*"2" | "Practice"*"Two") episode="2" ;;
        "Qualifying"*"1" | "Qualifying"*"One") episode="3" ;;
        "Qualifying"*"2" | "Qualifying"*"Two") episode="4" ;;
        "Qualifying") episode="3" ;; # Default to Q1 if not specified
        "Sprint") episode="5" ;;
        "Race") episode="6" ;;
        *) episode="0" ;;
        esac

        # Create target directories
        local season_dir="$DEST_DIR/$moto_class $year"
        local round_dir="$season_dir/$round $location"
        mkdir -p "$round_dir"

        # Create the target filename
        local target_file="$round_dir/${round}x${episode} ${session}.${extension}"

        echo "Moving: $file to $target_file"
        # Create hardlink instead of moving
        ln "$file" "$target_file" || cp "$file" "$target_file"

        return 0
    fi

    # If we get here, the file didn't match our pattern
    echo "File doesn't match expected Moto pattern: $file"
    return 1
}

# Process existing files and directories
find "$SRC_DIR" -type f -o -type d | grep -E '(Moto(GP|[23]))\.([0-9]{4})' | while read item; do
    organize_moto "$item"
done

# Monitor for new files and directories
inotifywait -m -r -e create -e moved_to "$SRC_DIR" | while read path action file; do
    if [[ $file =~ Moto(GP|[23])\.[0-9]{4} ]]; then
        # Allow a small delay for file to finish copying
        sleep 5
        organize_moto "$path/$file"
    fi
done
