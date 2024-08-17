import os
import subprocess
import json
import webbrowser
from flask import Flask, request, jsonify, Response
import logging
import psutil
import time
from pynput import mouse, keyboard
import random

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# File to store the state
STATE_FILE = 'waterwall_state.json'

# Global variables
last_activity_time = time.time()
idle_threshold = 5  # Consider user away if no activity for 5 seconds
intervalTime = 2000  # Reduced default refresh interval for faster updates
process_cache = {}  # Cache to store process information and reduce query overhead

# Check if the script is run as root
def check_root():
    if os.geteuid() != 0:
        logger.error("This script must be run as root. Please use 'sudo' to run the script.")
        logger.error("To preserve your aliases, run the script with: sudo -E python waterwall.py")
        exit(1)

# Load the state from the file
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {}

# Save the state to the file
def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)

# Get a list of all running processes (with caching)
def get_processes():
    global process_cache
    current_time = time.time()
    if not process_cache or current_time - process_cache['timestamp'] > 1:  # Refresh cache every 1 second
        process_cache = {'timestamp': current_time, 'processes': []}
        for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'num_threads', 'io_counters']):
            try:
                process_cache['processes'].append(p.info)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
    return process_cache['processes']

# Block traffic for a specific process
def block_process(pid):
    subprocess.run(["iptables", "-A", "OUTPUT", "-m", "owner", "--uid-owner", str(pid), "-j", "DROP"])

# Unblock traffic for a specific process
def unblock_process(pid):
    subprocess.run(["iptables", "-D", "OUTPUT", "-m", "owner", "--uid-owner", str(pid), "-j", "DROP"])

# Set traffic limit for a process (as a percentage of total bandwidth)
def set_traffic_limit(pid, percentage):
    bandwidth_limit = int(1024 * 1024 * percentage / 100)  # Convert to bytes per second
    subprocess.run(["iptables", "-A", "OUTPUT", "-m", "owner", "--uid-owner", str(pid), "-m", "limit", "--limit-burst", str(bandwidth_limit), "-j", "ACCEPT"])

# Get the current traffic usage for a process
def get_traffic_usage(pid):
    try:
        p = psutil.Process(pid)
        io_counters = p.io_counters()
        return io_counters.read_bytes + io_counters.write_bytes
    except psutil.NoSuchProcess:
        logger.warning(f"Process with PID {pid} no longer exists.")
        return 0
    except psutil.AccessDenied:
        raise PermissionError("Permission denied. Please run with sudo.")

# Function to detect user activity
def on_move(x, y):
    global last_activity_time
    last_activity_time = time.time()

def on_click(x, y, button, pressed):
    global last_activity_time
    last_activity_time = time.time()

def on_press(key):
    global last_activity_time
    last_activity_time = time.time()

# Start mouse and keyboard listeners
mouse_listener = mouse.Listener(on_move=on_move, on_click=on_click)
keyboard_listener = keyboard.Listener(on_press=on_press)
mouse_listener.start()
keyboard_listener.start()

# Function to check if the user is away
def is_user_away():
    return time.time() - last_activity_time > idle_threshold

# Function to throttle system processes
def throttle_processes():
    for proc in psutil.process_iter(attrs=['pid', 'name', 'cpu_percent', 'memory_percent']):
        try:
            # Throttle CPU usage to a minimal level
            p = psutil.Process(proc.info['pid'])
            p.nice(psutil.IDLE_PRIORITY_CLASS)
            logger.info(f"Throttled process: {proc.info['name']} (PID: {proc.info['pid']})")
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

@app.route('/processes', methods=['GET'])
def list_processes():
    try:
        processes = get_processes()
        process_info = []
        state = load_state()
        for p in processes:
            pid = p['pid']
            name = p['name']
            cpu_percent = p['cpu_percent']
            memory_percent = p['memory_percent']
            num_threads = p['num_threads']
            io_counters = p['io_counters']
            traffic_usage = io_counters.read_bytes + io_counters.write_bytes if io_counters else 0
            traffic_usage_mb = traffic_usage / (1024 * 1024)
            process_info.append({
                'pid': pid,
                'name': name,
                'cpu_percent': cpu_percent,
                'memory_percent': memory_percent,
                'num_threads': num_threads,
                'traffic_usage': traffic_usage,
                'traffic_usage_mb': traffic_usage_mb,
                'blocked': state.get(str(pid), {}).get('blocked', False),
                'limit': state.get(str(pid), {}).get('limit', None)
            })

        # Sort processes
        sort_column = request.args.get('sort', 'traffic_usage')
        sort_order = request.args.get('order', 'desc')

        # Map sort_column to the correct field
        sort_mapping = {
            'traffic_desc': 'traffic_usage',
            'traffic_asc': 'traffic_usage',
            'name_asc': 'name',
            'name_desc': 'name',
            'pid_asc': 'pid',
            'pid_desc': 'pid'
        }

        # Use the mapped field for sorting
        sort_key = sort_mapping.get(sort_column, 'traffic_usage')  # Default to traffic_usage if not found

        process_info.sort(key=lambda x: x[sort_key], reverse=(sort_order == 'desc'))

        return jsonify(process_info)
    except PermissionError as e:
        logger.error(str(e))
        return jsonify({'error': str(e)}), 500

@app.route('/block', methods=['POST'])
def block():
    pid = request.json.get('pid')
    block_process(pid)
    state = load_state()
    state[str(pid)] = {'blocked': True, 'limit': None}
    save_state(state)
    return jsonify({'status': 'success'})

@app.route('/unblock', methods=['POST'])
def unblock():
    pid = request.json.get('pid')
    unblock_process(pid)
    state = load_state()
    state[str(pid)] = {'blocked': False, 'limit': None}
    save_state(state)
    return jsonify({'status': 'success'})

@app.route('/limit', methods=['POST'])
def limit():
    pid = request.json.get('pid')
    percentage = request.json.get('percentage')
    set_traffic_limit(pid, percentage)
    state = load_state()
    state[str(pid)] = {'blocked': False, 'limit': percentage}
    save_state(state)
    return jsonify({'status': 'success'})

@app.route('/user_status', methods=['GET'])
def user_status():
    return jsonify({'away': is_user_away()})

@app.route('/throttle', methods=['POST'])
def throttle():
    throttle_processes()
    return jsonify({'status': 'success'})

@app.route('/process_stream')
def process_stream():
    def generate():
        while True:
            processes = get_processes()
            process_info = []
            state = load_state()
            for p in processes:
                pid = p['pid']
                name = p['name']
                cpu_percent = p['cpu_percent']
                memory_percent = p['memory_percent']
                num_threads = p['num_threads']
                io_counters = p['io_counters']
                traffic_usage = io_counters.read_bytes + io_counters.write_bytes if io_counters else 0
                traffic_usage_mb = traffic_usage / (1024 * 1024)
                process_info.append({
                    'pid': pid,
                    'name': name,
                    'cpu_percent': cpu_percent,
                    'memory_percent': memory_percent,
                    'num_threads': num_threads,
                    'traffic_usage': traffic_usage,
                    'traffic_usage_mb': traffic_usage_mb,
                    'blocked': state.get(str(pid), {}).get('blocked', False),
                    'limit': state.get(str(pid), {}).get('limit', None)
                })
            yield f"data: {json.dumps(process_info)}\n\n"
            time.sleep(intervalTime / 1000)

    return Response(generate(), mimetype='text/event-stream')

@app.route('/')
def index():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WaterWall Control</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/milligram/1.4.1/milligram.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body {
            padding: 20px;
            transition: background-color 0.5s, color 0.5s;
        }
        .container {
            max-width: 120rem;
        }
        table {
            width: 100%;
        }
        canvas {
            max-width: 100%;
            height: auto;
            margin-bottom: 20px;
        }
        #intervalInput {
            width: 100px;
            margin-right: 10px;
        }
        .button-row {
            margin-bottom: 20px;
        }
        .button-row button {
            margin-right: 10px;
            margin-bottom: 10px;
        }
        #analysisTextarea {
            display: none;
            width: 100%;
            height: 200px;
            margin-top: 20px;
        }
        .loading {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.5);
            z-index: 10000;
        }
        .loading .spinner {
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            width: 50px;
            height: 50px;
            border: 5px solid #f3f3f3;
            border-top: 5px solid #9b4dca;
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }
        @keyframes spin {
            0% { transform: translate(-50%, -50%) rotate(0deg); }
            100% { transform: translate(-50%, -50%) rotate(360deg); }
        }
        .process-card {
            border: 1px solid #e1e1e1;
            padding: 15px;
            margin-bottom: 15px;
            border-radius: 5px;
            animation: bounce 0.5s ease-in-out infinite alternate;
        }
        @keyframes bounce {
            0% { transform: translateY(0); }
            100% { transform: translateY(-5px); }
        }
        .process-card h3 {
            margin-bottom: 10px;
        }
        .error-message {
            color: #721c24;
            background-color: #f8d7da;
            border: 1px solid #f5c6cb;
            padding: 10px;
            margin-bottom: 15px;
            border-radius: 5px;
            display: none;
        }
        .analysis-textarea {
            display: none;
            width: 100%;
            height: 200px;
            margin-top: 20px;
            resize: vertical; /* Allow vertical resizing of the textarea */
            border: 1px solid #ccc; /* Add a border for better visibility */
            padding: 10px; /* Add some padding for better readability */
            font-family: monospace; /* Use a monospace font for the analysis output */
        }
        #trafficChartContainer {
            display: none; /* Initially hide the chart container */
            max-width: 100%;
            height: 400px; /* Set a fixed height for the chart container */
            margin-bottom: 20px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>WaterWall Control</h1>
        <div class="button-row">
            <button onclick="refreshData()">Refresh</button>
            <button onclick="toggleDarkMode()">Toggle Dark Mode</button>
            <button onclick="analyzeCyberSpaceHaze()">AI Analyze</button>
            <button onclick="intruderWaterPlay()">Intruder Water Play</button>
            <button onclick="confettiEffect()">Confetti</button>
            <button onclick="synthesizeAudio()">Synthesize Audio</button>
            <button onclick="changeBackgroundColor()">Random Color</button>
            <button onclick="toggleChart()">Show/Hide Graph</button>
        </div>
        <div id="analysisTextarea" class="analysis-textarea"></div> 
        <div class="row">
            <div class="column">
                <label for="intervalInput">Refresh Interval (ms):</label>
                <input type="number" id="intervalInput" value="2000">
                <button onclick="setIntervalTime()">Set Interval</button>
            </div>
            <div class="column">
                <label for="sortSelect">Sort by:</label>
                <select id="sortSelect" onchange="changeSort()">
                    <option value="traffic_desc">Traffic (High to Low)</option>
                    <option value="traffic_asc">Traffic (Low to High)</option>
                    <option value="name_asc">Name (A to Z)</option>
                    <option value="name_desc">Name (Z to A)</option>
                    <option value="pid_asc">PID (Low to High)</option>
                    <option value="pid_desc">PID (High to Low)</option>
                </select>
            </div>
        </div>
        <div id="trafficChartContainer">
            <canvas id="trafficChart"></canvas>
        </div>
        <div id="errorMessage" class="error-message"></div>
        <div id="processList"></div>
        <div class="loading">
            <div class="spinner"></div>
        </div>
    </div>
    <script>
        const $ = document.querySelector.bind(document);
        const $$ = document.querySelectorAll.bind(document);

        let intervalTime = 2000;
        let intervalId;
        let isDarkMode = false;
        let chart;
        let sortCriteria = 'traffic_desc';

        async function fetchProcesses() {
            $('.loading').style.display = 'block';
            try {
                const response = await fetch(`/processes?sort=${sortCriteria}`);
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                const processes = await response.json();
                updateProcessList(processes);
                updateChart(processes);
                $('#errorMessage').style.display = 'none';
            } catch (error) {
                console.error('Error fetching processes:', error);
                $('#errorMessage').textContent = `Error: ${error.message}. Please check your connection and try again.`;
                $('#errorMessage').style.display = 'block';
            } finally {
                $('.loading').style.display = 'none';
            }
        }

        function updateProcessList(processes) {
            const processList = $('#processList');
            processList.innerHTML = '';
            processes.forEach(process => {
                const card = document.createElement('div');
                card.className = 'process-card';
                card.innerHTML = `
                    <h3>${process.name} (PID: ${process.pid})</h3>
                    <p>Traffic Usage: ${(process.traffic_usage_mb).toFixed(2)} MB</p>
                    <p>Status: ${process.blocked ? 'Blocked' : 'Active'}</p>
                    <button onclick="toggleBlock(${process.pid}, ${process.blocked})">${process.blocked ? 'Unblock' : 'Block'}</button>
                    <input type="number" id="limit${process.pid}" min="0" max="100" placeholder="Limit %" value="${process.limit || ''}">
                    <button onclick="setLimit(${process.pid})">Set Limit</button>
                `;
                processList.appendChild(card);
            });
        }

        function updateChart(processes) {
            const ctx = $('#trafficChart').getContext('2d');
            const labels = processes.map(p => p.name);
            const data = processes.map(p => p.traffic_usage_mb);

            if (chart) {
                chart.destroy();
            }

            chart = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: labels,
                    datasets: [{
                        label: 'Traffic Usage (MB)',
                        data: data,
                        backgroundColor: 'rgba(155, 77, 202, 0.6)',
                        borderColor: 'rgba(155, 77, 202, 1)',
                        borderWidth: 1
                    }]
                },
                options: {
                    scales: {
                        y: {
                            beginAtZero: true,
                            title: {
                                display: true,
                                text: 'Traffic Usage (MB)'
                            },
                            // Set a suggested maximum value for the y-axis
                            suggestedMax: 100 // Adjust this value as needed
                        }
                    },
                    plugins: {
                        legend: {
                            display: false
                        },
                        title: {
                            display: true,
                            text: 'Process Traffic Usage'
                        }
                    },
                    responsive: true,
                    maintainAspectRatio: false
                }
            });
        }

        async function toggleBlock(pid, currentlyBlocked) {
            $('.loading').style.display = 'block';
            try {
                const action = currentlyBlocked ? 'unblock' : 'block';
                const response = await fetch(`/${action}`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ pid })
                });
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                await fetchProcesses(); // No need to manually refresh with SSE
            } catch (error) {
                console.error(`Error ${currentlyBlocked ? 'unblocking' : 'blocking'} process:`, error);
                $('#errorMessage').textContent = `Error: ${error.message}. Please try again.`;
                $('#errorMessage').style.display = 'block';
            } finally {
                $('.loading').style.display = 'none';
            }
        }

        async function setLimit(pid) {
            const limit = $(`#limit${pid}`).value;
            $('.loading').style.display = 'block';
            try {
                const response = await fetch('/limit', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ pid, percentage: limit })
                });
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                await fetchProcesses(); // No need to manually refresh with SSE
            } catch (error) {
                console.error('Error setting limit:', error);
                $('#errorMessage').textContent = `Error: ${error.message}. Please try again.`;
                $('#errorMessage').style.display = 'block';
            } finally {
                $('.loading').style.display = 'none';
            }
        }

        function setIntervalTime() {
            clearInterval(intervalId);
            intervalTime = parseInt($('#intervalInput').value, 10);
            intervalId = setInterval(fetchProcesses, intervalTime); // No need for setInterval with SSE
        }

        function changeSort() {
            sortCriteria = $('#sortSelect').value;
            fetchProcesses(); // Manually refresh when sorting changes
        }

        function toggleDarkMode() {
            isDarkMode = !isDarkMode;
            document.body.style.backgroundColor = isDarkMode ? '#333' : '#fff';
            document.body.style.color = isDarkMode ? '#fff' : '#333';
            $$('.button').forEach(button => {
                button.style.backgroundColor = isDarkMode ? '#555' : '';
                button.style.color = isDarkMode ? '#fff' : '';
            });
        }

        function analyzeCyberSpaceHaze() {
            const textarea = $('#analysisTextarea');
            textarea.style.display = 'block';
            // Use HTML line breaks for formatting
            textarea.innerHTML = "Analyzing Cyber Space Haze...<br>1. Quantum Flux: Stable<br>2. Nebula Density: 78%<br>3. Void Resonance: Harmonic<br>4. Starlight Interference: Minimal<br>5. Plasma Conductivity: Optimal<br>Conclusion: The Cyber Space Haze is currently in a favorable state for data transmission.";
        }

        function intruderWaterPlay() {
            showMessage("Intruder detected! Initiating WaterPlay protocol...");
            document.body.style.animation = 'shake 0.5s';
            setTimeout(() => {
                document.body.style.animation = '';
            }, 500);
        }

        function changeBackgroundColor() {
            const randomColor = '#' + Math.floor(Math.random() * 16777215).toString(16);
            document.body.style.backgroundColor = randomColor;
        }

        function confettiEffect() {
            const confettiCount = 200;
            const confettiColors = ['#ff0000', '#00ff00', '#0000ff', '#ffff00', '#ff00ff', '#00ffff'];

            for (let i = 0; i < confettiCount; i++) {
                const confetti = document.createElement('div');
                confetti.style.position = 'fixed';
                confetti.style.width = '10px';
                confetti.style.height = '10px';
                confetti.style.backgroundColor = confettiColors[Math.floor(Math.random() * confettiColors.length)];
                confetti.style.left = Math.random() * 100 + 'vw';
                confetti.style.top = '-10px';
                confetti.style.borderRadius = '50%';
                confetti.style.zIndex = '9999';
                document.body.appendChild(confetti);

                const animation = confetti.animate([
                    { transform: 'translateY(0) rotate(0deg)', opacity: 1 },
                    { transform: `translateY(100vh) rotate(${Math.random() * 360}deg)`, opacity: 0 }
                ], {
                    duration: Math.random() * 3000 + 2000,
                    easing: 'cubic-bezier(0.25, 0.46, 0.45, 0.94)'
                });

                animation.onfinish = () => confetti.remove();
            }
        }

        function synthesizeAudio() {
            const audioContext = new (window.AudioContext || window.webkitAudioContext)();
            const oscillator = audioContext.createOscillator();
            const gainNode = audioContext.createGain();

            oscillator.type = 'sine';
            oscillator.frequency.setValueAtTime(440, audioContext.currentTime);
            oscillator.connect(gainNode);
            gainNode.connect(audioContext.destination);

            gainNode.gain.setValueAtTime(0, audioContext.currentTime);
            gainNode.gain.linearRampToValueAtTime(1, audioContext.currentTime + 0.01);

            oscillator.start(audioContext.currentTime);
            gainNode.gain.exponentialRampToValueAtTime(0.001, audioContext.currentTime + 1);
            oscillator.stop(audioContext.currentTime + 1);

            alert("Synthesizing LFO Envelope Equalizer Noise Vocoder FFT Data Stream...");
        }

        function showMessage(message) {
            // Create a simple message box
            const messageBox = document.createElement('div');
            messageBox.style.position = 'fixed';
            messageBox.style.top = '50%';
            messageBox.style.left = '50%';
            messageBox.style.transform = 'translate(-50%, -50%)';
            messageBox.style.backgroundColor = 'white';
            messageBox.style.padding = '20px';
            messageBox.style.border = '1px solid black';
            messageBox.style.zIndex = '10001'; // Ensure it's above the loading spinner
            messageBox.textContent = message;
            document.body.appendChild(messageBox);

            // Add a close button
            const closeButton = document.createElement('button');
            closeButton.textContent = 'Close';
            closeButton.style.marginTop = '10px';
            closeButton.onclick = () => messageBox.remove();
            messageBox.appendChild(closeButton);
        }

        function toggleChart() {
            const chartContainer = $('#trafficChartContainer');
            if (chartContainer.style.display === 'none') {
                chartContainer.style.display = 'block';
            } else {
                chartContainer.style.display = 'none';
            }
        }

        // Initialize EventSource for real-time updates
        const eventSource = new EventSource('/process_stream');

        eventSource.onmessage = (event) => {
            const processes = JSON.parse(event.data);
            updateProcessList(processes);
            updateChart(processes);
        };

        // Initial data fetch
        fetchProcesses();
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    check_root()
    webbrowser.open('http://127.0.0.1:5000')
    app.run(debug=True)