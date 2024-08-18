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
from collections import deque

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
graph_refresh_enabled = True  # Flag to control graph refreshing
historical_data = {}  # Store historical data for each process
max_history_length = 60  # Keep 60 data points (e.g., 2 minutes of data with 2-second intervals)
total_bandwidth_usage = 0  # Keep track of total bandwidth usage

# Wow factor: Inspiring quotes
quotes = [
    "The only way to do great work is to love what you do. - Steve Jobs",
    "Strive not to be a success, but rather to be of value. - Albert Einstein",
    "The mind is everything. What you think you become. - Buddha",
    "Believe you can and you're halfway there. - Theodore Roosevelt",
    "The only person you are destined to become is the person you decide to be. - Ralph Waldo Emerson",
    "Life is what happens when you're busy making other plans. - John Lennon",
    "Your time is limited, so don't waste it living someone else's life. - Steve Jobs",
    "The future belongs to those who believe in the beauty of their dreams. - Eleanor Roosevelt",
    "The best and most beautiful things in the world cannot be seen or even touched - they must be felt with the heart. - Helen Keller",
    "The greatest glory in living lies not in never falling, but in rising every time we fall. - Nelson Mandela"
]

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

# Get a list of all running processes
def get_processes():
    global process_cache, historical_data, total_bandwidth_usage
    current_time = time.time()
    if not process_cache or current_time - process_cache['timestamp'] > 1:  # Refresh cache every 1 second
        process_cache = {'timestamp': current_time, 'processes': []}
        total_bandwidth_usage = 0
        for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'num_threads', 'io_counters']):
            try:
                process_info = p.info
                pid = process_info['pid']
                io_counters = process_info['io_counters']
                traffic_usage = io_counters.read_bytes + io_counters.write_bytes if io_counters else 0
                traffic_usage_mb = traffic_usage / (1024 * 1024)

                # Update historical data
                if pid not in historical_data:
                    historical_data[pid] = deque(maxlen=max_history_length)
                historical_data[pid].append(traffic_usage_mb)

                process_cache['processes'].append(process_info)
                total_bandwidth_usage += traffic_usage_mb

            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
    return process_cache['processes']

# Network Management Functions (using iptables)
def block_process(pid):
    subprocess.run(["iptables", "-A", "OUTPUT", "-m", "owner", "--uid-owner", str(pid), "-j", "DROP"])

def unblock_process(pid):
    subprocess.run(["iptables", "-D", "OUTPUT", "-m", "owner", "--uid-owner", str(pid), "-j", "DROP"])

def set_traffic_limit(pid, percentage):
    # Calculate bandwidth limit in bytes per second
    bandwidth_limit = int(1024 * 1024 * percentage / 100) 
    subprocess.run(["iptables", "-A", "OUTPUT", "-m", "owner", "--uid-owner", str(pid), "-m", "limit", "--limit-bytes", str(bandwidth_limit)+"/s", "-j", "ACCEPT"])

# User Activity Monitoring
def on_move(x, y):
    global last_activity_time
    last_activity_time = time.time()

def on_click(x, y, button, pressed):
    global last_activity_time
    last_activity_time = time.time()

def on_press(key):
    global last_activity_time
    last_activity_time = time.time()

# Start listeners
mouse_listener = mouse.Listener(on_move=on_move, on_click=on_click)
keyboard_listener = keyboard.Listener(on_press=on_press)
mouse_listener.start()
keyboard_listener.start()

def is_user_away():
    return time.time() - last_activity_time > idle_threshold

# Process Throttling (using nice)
def throttle_processes():
    for proc in psutil.process_iter(attrs=['pid', 'name', 'cpu_percent', 'memory_percent']):
        try:
            p = psutil.Process(proc.info['pid'])
            p.nice(psutil.IDLE_PRIORITY_CLASS)  # Set to lowest priority
            logger.info(f"Throttled process: {proc.info['name']} (PID: {proc.info['pid']})")
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

# Flask API Endpoints
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
                'limit': state.get(str(pid), {}).get('limit', None),
                'historical_data': list(historical_data.get(pid, []))
            })

        # Sorting Logic (Improved)
        sort_by = request.args.get('sort_by', 'traffic_usage')
        sort_order = request.args.get('sort_order', 'desc')

        if sort_by == 'traffic_usage':
            process_info.sort(key=lambda x: x['traffic_usage_mb'], reverse=(sort_order == 'desc'))
        elif sort_by == 'name':
            process_info.sort(key=lambda x: x['name'], reverse=(sort_order == 'desc'))
        elif sort_by == 'pid':
            process_info.sort(key=lambda x: x['pid'], reverse=(sort_order == 'desc'))

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

# Server-Sent Events (SSE) for Real-time Updates
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
                    'limit': state.get(str(pid), {}).get('limit', None),
                    'historical_data': list(historical_data.get(pid, []))
                })

            # Send the processes array directly
            yield f"data: {json.dumps(process_info)}\n\n"  # Fixed: Send array directly
            time.sleep(intervalTime / 1000)

    return Response(generate(), mimetype='text/event-stream')

# HTML for the Web Interface
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
            transition: transform 0.3s ease, opacity 0.3s ease;
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
        .message-box {
            position: fixed;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            background-color: white;
            padding: 20px;
            border: 1px solid black;
            z-index: 10001;
            display: none; /* Initially hidden */
        }
        .message-box button {
            margin-top: 10px;
        }
        #quote {
            font-style: italic;
            color: #666;
            margin-bottom: 20px;
        }
        .notification-cue {
            position: fixed;
            bottom: 20px;
            right: 20px;
            z-index: 10002;
        }
        .notification {
            background-color: #f8f8f8;
            border: 1px solid #ccc;
            padding: 10px;
            margin-bottom: 5px;
            border-radius: 5px;
            animation: fadeInOut 5s ease-in-out;
            opacity: 0; /* Initially hidden */
        }
        @keyframes fadeInOut {
            0% { opacity: 0; transform: translateY(20px); }
            10% { opacity: 1; transform: translateY(0); }
            90% { opacity: 1; transform: translateY(0); }
            100% { opacity: 0; transform: translateY(20px); }
        }
        #bandwidthUsageBar {
            width: 0%;
            height: 10px;
            background-color: #9b4dca;
            margin-bottom: 10px;
            border-radius: 5px;
            transition: width 0.5s ease-in-out;
        }
        .button-group {
            display: flex;
            gap: 5px;
        }
        .button-group button {
            flex: 1;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>WaterWall Control</h1>
        <div id="quote"></div>
        <div class="button-row">
            <button onclick="fetchProcesses()">Refresh Process List</button>
            <button onclick="toggleDarkMode()">Toggle Dark Mode</button>
            <button onclick="analyzeCyberSpaceHaze()">AI Analyze</button>
            <button onclick="intruderWaterPlay()">Intruder Water Play</button>
            <button onclick="confettiEffect()">Confetti</button>
            <button onclick="synthesizeAudio()">Synthesize Audio</button>
            <button onclick="changeBackgroundColor()">Random Color</button>
            <button onclick="toggleChart()">Show/Hide Graph</button>
            <button onclick="toggleGraphRefresh()">Toggle Graph Refresh</button>
        </div>
        <div id="analysisTextarea" class="analysis-textarea"></div>
        <div id="feedbackArea"></div>
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
        <div id="bandwidthUsage">
            <label for="bandwidthUsageBar">Bandwidth Usage:</label>
            <div id="bandwidthUsageBar"></div>
        </div>
        <div id="trafficChartContainer">
            <canvas id="trafficChart"></canvas>
        </div>
        <div id="errorMessage" class="error-message"></div>
        <div id="processList"></div>
        <div class="loading">
            <div class="spinner"></div>
        </div>
        <div id="messageBox" class="message-box">
            <div id="messageContent"></div>
            <button onclick="closeMessageBox()">Close</button>
        </div>
        <div class="notification-cue"></div>
    </div>
      <script>
        const $ = document.querySelector.bind(document);
        const $$ = document.querySelectorAll.bind(document);

        let intervalTime = 2000;
        let intervalId;
        let isDarkMode = false;
        let chart;
        let sortCriteria = 'traffic_desc';
        let graphRefreshEnabled = true; // Track graph refresh status
        const quotes = [ // Added quotes array in JavaScript
            "The only way to do great work is to love what you do. - Steve Jobs",
            "Strive not to be a success, but rather to be of value. - Albert Einstein",
            "The mind is everything. What you think you become. - Buddha",
            "Believe you can and you're halfway there. - Theodore Roosevelt",
            "The only person you are destined to become is the person you decide to be. - Ralph Waldo Emerson",
            "Life is what happens when you're busy making other plans. - John Lennon",
            "Your time is limited, so don't waste it living someone else's life. - Steve Jobs",
            "The future belongs to those who believe in the beauty of their dreams. - Eleanor Roosevelt",
            "The best and most beautiful things in the world cannot be seen or even touched - they must be felt with the heart. - Helen Keller",
            "The greatest glory in living lies not in never falling, but in rising every time we fall. - Nelson Mandela"
        ];

        async function fetchProcesses() {
            $('.loading').style.display = 'block'; // Show loading indicator
            try {
                const response = await fetch(`/processes?sort=${sortCriteria}`);
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                const processes = await response.json();
                updateProcessList(processes);
                if (graphRefreshEnabled) {
                    updateChart(); // Update chart with latest data
                }
                $('#errorMessage').style.display = 'none';
            } catch (error) {
                console.error('Error fetching processes:', error);
                $('#errorMessage').textContent = `Error: ${error.message}. Please check your connection and try again.`;
                $('#errorMessage').style.display = 'block';
            } finally {
                $('.loading').style.display = 'none'; // Hide loading indicator
            }
        }

        function updateProcessList(processes) {
            const processList = $('#processList');
            processList.innerHTML = ''; // Clear previous list

            // Sort processes based on the selected criteria
            processes.sort((a, b) => {
                if (sortCriteria === 'traffic_desc') {
                    return b.traffic_usage_mb - a.traffic_usage_mb;
                } else if (sortCriteria === 'traffic_asc') {
                    return a.traffic_usage_mb - b.traffic_usage_mb;
                } else if (sortCriteria === 'name_asc') {
                    return a.name.localeCompare(b.name);
                } else if (sortCriteria === 'name_desc') {
                    return b.name.localeCompare(a.name);
                } else if (sortCriteria === 'pid_asc') {
                    return a.pid - b.pid;
                } else if (sortCriteria === 'pid_desc') {
                    return b.pid - a.pid;
                }
            });

            processes.forEach(process => {
                const card = document.createElement('div');
                card.className = 'process-card';
                card.id = `process-${process.pid}`;
                card.innerHTML = `
                    <h3>${process.name} (PID: ${process.pid})</h3>
                    <p>Traffic Usage: ${(process.traffic_usage_mb).toFixed(2)} MB</p>
                    <p>Status: ${process.blocked ? 'Blocked' : 'Active'}</p>
                    <div class="button-group">
                        <button onclick="toggleBlock(${process.pid}, ${process.blocked})">${process.blocked ? 'Unblock' : 'Block'}</button>
                        <button onclick="setLimit(${process.pid}, 75)">Limit 75%</button>
                        <button onclick="setLimit(${process.pid}, 50)">Limit 50%</button>
                        <button onclick="setLimit(${process.pid}, 25)">Limit 25%</button>
                    </div>
                `;
                processList.appendChild(card);
            });
        }

        function toggleChart() {
            const chartContainer = $('#trafficChartContainer');
            if (chartContainer.style.display === 'none') {
                chartContainer.style.display = 'block';
                updateChart(); // Refresh chart when shown
            } else {
                chartContainer.style.display = 'none';
            }
        }

        function toggleGraphRefresh() {
            graphRefreshEnabled = !graphRefreshEnabled;
            const feedbackArea = $('#feedbackArea');
            feedbackArea.textContent = graphRefreshEnabled ? 'Graph refreshing enabled.' : 'Graph refreshing disabled.';
        }

        function updateChart() {
    if (!graphRefreshEnabled) {
        return; // Don't update the chart if refreshing is disabled
    }

    const ctx = $('#trafficChart').getContext('2d');

    // Fetch latest process data
    fetch('/processes')
        .then(response => response.json())
        .then(processes => {
            const labels = generateTimeLabels();
            const datasets = processes.map(p => ({
                label: p.name,
                data: p.historical_data, // Use historical data for the line chart
                borderColor: `rgba(${Math.floor(Math.random() * 256)}, ${Math.floor(Math.random() * 256)}, ${Math.floor(Math.random() * 256)}, 1)`, // Use Math.random() for random colors
                borderWidth: 1,
                fill: false
            }));

            if (chart) {
                chart.destroy();
            }

            chart = new Chart(ctx, {
                type: 'line', // Changed chart type to 'line' for historical data
                data: {
                    labels: labels,
                    datasets: datasets
                },
                options: {
                    scales: {
                        y: {
                            beginAtZero: true,
                            title: {
                                display: true,
                                text: 'Traffic Usage (MB)'
                            }
                        },
                        x: {
                            title: {
                                display: true,
                                text: 'Time'
                            }
                        }
                    },
                    plugins: {
                        legend: {
                            display: true
                        },
                        title: {
                            display: true,
                            text: 'Historical Process Traffic Usage'
                        }
                    },
                    responsive: true,
                    maintainAspectRatio: false
                }
            });
        })
        .catch(error => {
            console.error('Error fetching process data for chart:', error);
        });
}

        function generateTimeLabels() {
            const max_history_length = 60; // Fixed: Define max_history_length in JavaScript
            const now = new Date();
            const labels = [];
            for (let i = 0; i < max_history_length; i++) {
                const time = new Date(now.getTime() - (i * intervalTime));
                labels.unshift(`${time.getHours()}:${time.getMinutes()}:${time.getSeconds()}`);
            }
            return labels;
        }

        async function toggleBlock(pid, currentlyBlocked) {
            $('.loading').style.display = 'block'; // Show loading indicator
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
                showMessage(`${currentlyBlocked ? 'Unblocked' : 'Blocked'} process with PID ${pid}`);
                await fetchProcesses(); // Refresh process list
            } catch (error) {
                console.error(`Error ${currentlyBlocked ? 'unblocking' : 'blocking'} process:`, error);
                $('#errorMessage').textContent = `Error: ${error.message}. Please try again.`;
                $('#errorMessage').style.display = 'block';
            } finally {
                $('.loading').style.display = 'none'; // Hide loading indicator
            }
        }

        async function setLimit(pid, percentage) {
            $('.loading').style.display = 'block'; // Show loading indicator
            try {
                const response = await fetch('/limit', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ pid, percentage: percentage })
                });
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                showMessage(`Set traffic limit for process with PID ${pid} to ${percentage}%`);
                await fetchProcesses(); // Refresh process list
            } catch (error) {
                console.error('Error setting limit:', error);
                $('#errorMessage').textContent = `Error: ${error.message}. Please try again.`;
                $('#errorMessage').style.display = 'block';
            } finally {
                $('.loading').style.display = 'none'; // Hide loading indicator
            }
        }

        function setIntervalTime() {
            clearInterval(intervalId);
            intervalTime = parseInt($('#intervalInput').value, 10);
            intervalId = setInterval(fetchProcesses, intervalTime); // No need for setInterval with SSE
            showMessage(`Refresh interval set to ${intervalTime}ms`);
        }

        function changeSort() {
            sortCriteria = $('#sortSelect').value;
            fetchProcesses(); // Manually refresh when sorting changes
        }

        function toggleDarkMode() {
            isDarkMode = !isDarkMode;
            document.body.style.backgroundColor = isDarkMode ? '#333' : '#fff';
            document.body.style.color = isDarkMode ? '#fff' : '#000';
            $$('.button').forEach(button => {
                button.style.backgroundColor = isDarkMode ? '#555' : '';
                button.style.color = isDarkMode ? '#fff' : '';
            });
            showMessage(`Dark mode ${isDarkMode ? 'enabled' : 'disabled'}`);
        }

        function analyzeCyberSpaceHaze() {
            const textarea = $('#analysisTextarea');
            textarea.style.display = 'block';
            // Use HTML line breaks for formatting
            textarea.innerHTML = "Analyzing Cyber Space Haze...<br>1. Quantum Flux: Stable<br>2. Nebula Density: 78%<br>3. Void Resonance: Harmonic<br>4. Starlight Interference: Minimal<br>5. Plasma Conductivity: Optimal<br>Conclusion: The Cyber Space Haze is currently in a favorable state for data transmission.";
            showMessage('Cyber Space Haze analysis complete!');
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
            showMessage(`Background color changed to ${randomColor}`);
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
            showMessage('Confetti time!');
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

            showMessage("Synthesizing LFO Envelope Equalizer Noise Vocoder FFT Data Stream...");
        }

        function showMessage(message) {
            $('#messageContent').textContent = message;
            $('#messageBox').style.display = 'block';
        }

        function closeMessageBox() {
            $('#messageBox').style.display = 'none';
        }
        function displayRandomQuote() {
            const quoteElement = $('#quote');
            const randomIndex = Math.floor(Math.random() * quotes.length);
            quoteElement.textContent = quotes[randomIndex];
        }
   
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    check_root()
    webbrowser.open('http://127.0.0.1:5000')
    app.run(debug=True)
