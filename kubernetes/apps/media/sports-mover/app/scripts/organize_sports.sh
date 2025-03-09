#!/bin/bash

# Set source and destination directories
SRC_DIR="/data/torrents/sport"
DEST_DIR="/data/media/sport"

# Function to organize motorcycle racing files
organize_moto() {
    local file="$1"
    local filename=$(basename "$file")

    # Debug output
    echo "Processing file: $file"

    # Handle both files and directories
    if [[ -d "$file" ]]; then
        # For directories, look for matching video files inside
        find "$file" -type f -name "*.mkv" -o -name "*.mp4" | while read video_file; do
            echo "Found video file in directory: $video_file"
            organize_moto "$video_file"
        done
        return 0
    fi

    # Modified regex pattern to match your file naming convention more flexibly
    if [[ $file =~ (Moto(?:GP|[23]))\.([0-9]{4})\.Round([0-9]{2})\.([A-Za-z]+)\.([A-Za-z]+) ]]; then
        local moto_class="${BASH_REMATCH[1]}"
        local year="${BASH_REMATCH[2]}"
        local round="${BASH_REMATCH[3]}"
        local location="${BASH_REMATCH[4]}"
        local session="${BASH_REMATCH[5]}"
        local extension="${file##*.}"

        echo "Matched components:"
        echo "- Class: $moto_class"
        echo "- Year: $year"
        echo "- Round: $round"
        echo "- Location: $location"
        echo "- Session: $session"
        echo "- Extension: $extension"

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
    else
        echo "File doesn't match expected Moto pattern (detailed): $file"
        # Print the filename for debugging
        echo "Filename: $filename"
        return 1
    fi
}

# Process existing files and directories with more direct approach
find "$SRC_DIR" -type f -name "*.mkv" | grep -E '(Moto(GP|[23]))' | while read file; do
    echo "Found MKV file: $file"
    organize_moto "$file"
done

# Alternative regex pattern for directory search
find "$SRC_DIR" -type d | grep -E '(Moto(GP|[23]))' | while read dir; do
    echo "Found directory: $dir"
    organize_moto "$dir"
done

echo "Initial file processing completed. Starting file monitoring..."

# Monitor for new files and directories
while true; do
    echo "Waiting for new files..."
    # Use a simpler approach since inotifywait might have issues
    sleep 60

    # Check for new files periodically
    find "$SRC_DIR" -type f -name "*.mkv" -mmin -2 | grep -E '(Moto(GP|[23]))' | while read file; do
        echo "Found new MKV file: $file"
        organize_moto "$file"
    done
done
