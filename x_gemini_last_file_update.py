import os
import re
import hashlib

# File to skip updating
SKIP_FILE = "x_gemini_last_file_update.py"
CHECKSUM_LOG_FILE = "x_gemini_last_file_update_checksum_log.txt"


# Function to calculate the checksum of a file
def calculate_checksum(file_path):
    with open(file_path, "rb") as file:
        return hashlib.md5(file.read()).hexdigest()


# Read the last edited file path from the text file
with open("x_gemini_last_file_update_message.txt", "r") as file:
    last_edited_file = file.read().strip()

# Skip if the file is the one to be skipped
if last_edited_file == SKIP_FILE:
    print(f"Skipping update for {SKIP_FILE}")
    exit(0)

# Read the checksum log
checksum_log = {}
if os.path.exists(CHECKSUM_LOG_FILE):
    with open(CHECKSUM_LOG_FILE, "r") as file:
        for line in file:
            file_path, checksum = line.strip().split(":")
            checksum_log[file_path] = checksum

# Calculate the current checksum of the last edited file
current_checksum = calculate_checksum(last_edited_file)

# Check if the file has already been updated
if (
    last_edited_file in checksum_log
    and checksum_log[last_edited_file] == current_checksum
):
    print(f"{last_edited_file} has already been updated. Skipping.")
    exit(0)

# Read the contents of the last edited Python file
with open(last_edited_file, "r") as file:
    code = file.read()

# Use a regex pattern to find the def statements in the file
def_pattern = r"def\s+(\w+)\s*\((.*?)\):"
defs = re.findall(def_pattern, code)

# Print the found def statements
for def_name, params in defs:
    print(f"Found def statement: def {def_name}({params})")

# Define the new code for specific functions
new_code = {
    "process_stream": """
@app.route('/process_stream')
def process_stream():
    def generate():
        while True:
            processes = get_processes()
            process_info = []
            # ... (rest of the code to build process_info)

            # Send the processes array directly
            yield f"data: {json.dumps(process_info)}\\n\\n"  # Fixed: Send array directly
            time.sleep(intervalTime / 1000)

    return Response(generate(), mimetype='text/event-stream')
""",
    "generateTimeLabels": """
function generateTimeLabels() {
    const max_history_length = 60; // Fixed: Define max_history_length in JavaScript
    const now = new Date();
    const labels = [];
    for (let i = 0; i < max_history_length; i++) {
        // ... (rest of the generateTimeLabels function)
    }
    return labels;
}
""",
}

# Replace the def statements with new code if available
for def_name, params in defs:
    if def_name in new_code:
        print(f"Replacing def statement: def {def_name}({params})")
        code = re.sub(def_pattern, new_code[def_name], code, count=1)

# Write the modified code back to the file
with open(last_edited_file, "w") as file:
    file.write(code)

# Update the checksum log
checksum_log[last_edited_file] = current_checksum
with open(CHECKSUM_LOG_FILE, "w") as file:
    for file_path, checksum in checksum_log.items():
        file.write(f"{file_path}:{checksum}\n")

print(f"{last_edited_file} has been updated successfully.")
