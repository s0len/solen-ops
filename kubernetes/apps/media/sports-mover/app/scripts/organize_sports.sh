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

    # Extract the important parts using a more flexible pattern
    # Capture class, year, round, location, session
    local moto_class=""
    local year=""
    local round=""
    local location=""
    local session=""
    local extension="${file##*.}"

    # Match the basic parts (class, year, round, location)
    if [[ $filename =~ ^(Moto(?:GP|[23]))\.([0-9]{4})\.Round([0-9]{2})\.([^\.]+) ]]; then
        moto_class="${BASH_REMATCH[1]}"
        year="${BASH_REMATCH[2]}"
        round="${BASH_REMATCH[3]}"
        location="${BASH_REMATCH[4]}"

        echo "Base elements matched:"
        echo "- Class: $moto_class"
        echo "- Year: $year"
        echo "- Round: $round"
        echo "- Location: $location"

        # Now extract the session type
        if [[ $filename =~ \.([^\.]+)\.(WEB-DL|TNT|720p|1080p) ]]; then
            session="${BASH_REMATCH[1]}"
            echo "- Session matched: $session"
        else
            # Try alternative pattern for session
            if [[ $filename =~ \.(Race|Sprint|Qualifying|Practice)[^\.]*\. ]]; then
                session="${BASH_REMATCH[1]}"
                echo "- Session matched (alt): $session"

                # Check for Q1/Q2 designations
                if [[ $filename =~ Qualifying\.(Q[12]) ]]; then
                    session="Qualifying ${BASH_REMATCH[1]}"
                    echo "- Session refined: $session"
                fi
            fi
        fi

        # Determine episode number based on session type
        local episode=""
        case "$session" in
        "Practice"*"1" | "Practice"*"One") episode="1" ;;
        "Practice"*"2" | "Practice"*"Two") episode="2" ;;
        "Qualifying Q1" | "Qualifying.Q1") episode="3" ;;
        "Qualifying Q2" | "Qualifying.Q2") episode="4" ;;
        "Qualifying") episode="3" ;; # Default to Q1 if not specified
        "Sprint") episode="5" ;;
        "Race") episode="6" ;;
        *)
            # If we still can't determine, try direct filename matching
            if [[ $filename =~ Qualifying\.Q1 ]]; then
                session="Qualifying Q1"
                episode="3"
            elif [[ $filename =~ Qualifying\.Q2 ]]; then
                session="Qualifying Q2"
                episode="4"
            else
                episode="0"
            fi
            ;;
        esac

        echo "- Episode number: $episode"

        # Special case handling for the TNT part in Race files
        if [[ $filename =~ Race\.TNT ]]; then
            session="Race"
            episode="6"
        fi

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

# Process existing files and directories
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
