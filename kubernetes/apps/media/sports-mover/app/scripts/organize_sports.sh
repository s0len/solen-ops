#!/bin/bash

# Set source and destination directories
SRC_DIR="/data/torrents/sport"
DEST_DIR="/data/media/sport"

# Function to organize motorcycle racing files
organize_moto() {
    local file="$1"
    local filename=$(basename "$file")

    # Debug output
    echo "Processing file: $file with direct string parsing"

    # Handle both files and directories
    if [[ -d "$file" ]]; then
        # For directories, look for matching video files inside
        find "$file" -type f -name "*.mkv" | while read video_file; do
            echo "Found video file in directory: $video_file"
            organize_moto "$video_file"
        done
        return 0
    fi

    # Simple checks to ensure this is a file we want to process
    if [[ ! $filename == Moto* ]] || [[ ! $filename == *.mkv ]]; then
        echo "Not a Moto video file: $filename"
        return 1
    fi

    # Direct string parsing without regex
    # Format: Moto[GP|2|3].YEAR.RoundXX.LOCATION.SESSION...

    # Get class (MotoGP, Moto2, Moto3)
    if [[ $filename == MotoGP* ]]; then
        moto_class="MotoGP"
    elif [[ $filename == Moto2* ]]; then
        moto_class="Moto2"
    elif [[ $filename == Moto3* ]]; then
        moto_class="Moto3"
    else
        echo "Unknown Moto class in filename: $filename"
        return 1
    fi

    # Get year (next part after the first dot)
    IFS='.' read -ra PARTS <<<"$filename"
    if [[ ${#PARTS[@]} -lt 4 ]]; then
        echo "Not enough parts in filename: $filename"
        return 1
    fi

    year="${PARTS[1]}"
    echo "Year: $year"

    # Get round number and location
    round="${PARTS[2]#Round}" # Remove 'Round' prefix
    location="${PARTS[3]}"
    echo "Round: $round, Location: $location"

    # Determine session and episode
    session=""
    episode=""

    # Check for Race
    if [[ $filename == *Race* ]]; then
        session="Race"
        episode="6"
    # Check for Sprint
    elif [[ $filename == *Sprint* ]]; then
        session="Sprint"
        episode="5"
    # Check for Qualifying
    elif [[ $filename == *Qualifying* ]]; then
        if [[ $filename == *Q1* ]]; then
            session="Qualifying Q1"
            episode="3"
        elif [[ $filename == *Q2* ]]; then
            session="Qualifying Q2"
            episode="4"
        else
            session="Qualifying"
            episode="3"
        fi
    # Check for Practice
    elif [[ $filename == *Practice* ]]; then
        if [[ $filename == *"1"* ]] || [[ $filename == *"One"* ]]; then
            session="Practice 1"
            episode="1"
        elif [[ $filename == *"2"* ]] || [[ $filename == *"Two"* ]]; then
            session="Practice 2"
            episode="2"
        else
            session="Practice"
            episode="0"
        fi
    else
        session="Unknown"
        episode="0"
    fi

    echo "Session: $session, Episode: $episode"

    # Get file extension
    extension="${filename##*.}"

    # Create target directories
    local season_dir="$DEST_DIR/$moto_class $year"
    local round_dir="$season_dir/$round $location"
    mkdir -p "$round_dir"

    # Create the target filename
    local target_file="$round_dir/${round}x${episode} ${session}.${extension}"

    echo "Moving: $file to $target_file"
    # Create hardlink instead of moving
    ln "$file" "$target_file" || cp "$file" "$target_file"
    echo "Successfully processed file!"

    return 0
}

# Process existing files (direct approach)
echo "Looking for MotoGP files..."
find "$SRC_DIR" -name "*.mkv" | grep -E 'Moto(GP|2|3)' | while read file; do
    echo "Found MKV file: $file"
    organize_moto "$file"
done

echo "Initial file processing completed. Starting file monitoring..."

# Monitor for new files and directories
while true; do
    echo "Waiting for new files..."
    # Use a simpler approach since inotifywait might have issues
    sleep 60

    # Check for new files periodically
    find "$SRC_DIR" -name "*.mkv" -mmin -2 | grep -E 'Moto(GP|2|3)' | while read file; do
        echo "Found new MKV file: $file"
        organize_moto "$file"
    done
done
