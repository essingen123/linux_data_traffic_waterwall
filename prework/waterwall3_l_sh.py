import os
import subprocess
import psutil
import json
import webbrowser
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__)

# File to store the state
STATE_FILE = 'waterwall_state.json'

# Check if the script is run as root
def check_root():
    if os.geteuid() != 0:
        print("This script must be run as root. Please use 'sudo' to run the script.")
        print("To preserve your aliases, run the script with: sudo -E python waterwall.py")
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
    return [p.info for p in psutil.process_iter(['pid', 'name'])]

# Block traffic for a specific process
def block_process(pid):
    subprocess.call(f"iptables -A OUTPUT -m owner --uid-owner {pid} -j DROP", shell=True)

# Unblock traffic for a specific process
def unblock_process(pid):
    subprocess.call(f"iptables -D OUTPUT -m owner --uid-owner {pid} -j DROP", shell=True)

# Set traffic limit for a process (as a percentage of total bandwidth)
def set_traffic_limit(pid, percentage):
    bandwidth_limit = int(1024 * 1024 * percentage / 100)  # Convert to bytes per second
    subprocess.call(f"iptables -A OUTPUT -m owner --uid-owner {pid} -m limit --limit-burst {bandwidth_limit} -j ACCEPT", shell=True)

# Get the current traffic usage for a process
def get_traffic_usage(pid):
    try:
        p = psutil.Process(pid)
        io_counters = p.io_counters()
        return io_counters.bytes_sent + io_counters.bytes_recv
    except psutil.AccessDenied:
        raise PermissionError("Permission denied. Please run with sudo.")

@app.route('/processes', methods=['GET'])
def list_processes():
    try:
        processes = get_processes()
        process_info = []
        state = load_state()
        for p in processes:
            pid = p['pid']
            name = p['name']
            traffic_usage = get_traffic_usage(pid)
            process_info.append({'pid': pid, 'name': name, 'traffic_usage': traffic_usage, 'blocked': state.get(pid, {}).get('blocked', False), 'limit': state.get(pid, {}).get('limit', None)})
        return jsonify(process_info)
    except PermissionError as e:
        return jsonify({'error': str(e)}), 500

@app.route('/block', methods=['POST'])
def block():
    pid = request.json.get('pid')
    block_process(pid)
    state = load_state()
    state[pid] = {'blocked': True, 'limit': None}
    save_state(state)
    return jsonify({'status': 'success'})

@app.route('/unblock', methods=['POST'])
def unblock():
    pid = request.json.get('pid')
    unblock_process(pid)
    state = load_state()
    state[pid] = {'blocked': False, 'limit': None}
    save_state(state)
    return jsonify({'status': 'success'})

@app.route('/limit', methods=['POST'])
def limit():
    pid = request.json.get('pid')
    percentage = request.json.get('percentage')
    set_traffic_limit(pid, percentage)
    state = load_state()
    state[pid] = {'blocked': False, 'limit': percentage}
    save_state(state)
    return jsonify({'status': 'success'})

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/static/<path:path>')
def static_files(path):
    return send_from_directory('static', path)

def create_static_files():
    os.makedirs('static', exist_ok=True)

    # Create index.html
    with open('static/index.html', 'w') as f:
        f.write('''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WaterWall Control</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body {
            font-family: Arial, sans-serif;
        }
        table {
            width: 100%;
            border-collapse: collapse;
        }
        table, th, td {
            border: 1px solid black;
        }
        th, td {
            padding: 8px;
            text-align: left;
        }
        canvas {
            max-width: 100%;
            height: auto;
        }
    </style>
</head>
<body>
    <h1>WaterWall Control</h1>
    <table id="processTable">
        <thead>
            <tr>
                <th>PID</th>
                <th>Name</th>
                <th>Traffic Usage</th>
                <th>Actions</th>
            </tr>
        </thead>
        <tbody>
        </tbody>
    </table>
    <canvas id="trafficChart"></canvas>
    <script>
        async function fetchProcesses() {
            const response = await fetch('/processes');
            const processes = await response.json();
            if (processes.error) {
                alert(processes.error);
                return;
            }
            const tableBody = document.querySelector('#processTable tbody');
            tableBody.innerHTML = '';
            processes.forEach(process => {
                const row = document.createElement('tr');
                row.innerHTML = `
                    <td>${process.pid}</td>
                    <td>${process.name}</td>
                    <td>${process.traffic_usage} bytes</td>
                    <td>
                        <button ${process.blocked ? 'disabled' : ''} onclick="blockProcess(${process.pid})">Block</button>
                        <button ${!process.blocked ? 'disabled' : ''} onclick="unblockProcess(${process.pid})">Unblock</button>
                        <input type="number" id="limit${process.pid}" min="0" max="100" placeholder="Limit %" value="${process.limit || ''}">
                        <button onclick="setLimit(${process.pid})">Set Limit</button>
                    </td>
                `;
                tableBody.appendChild(row);
            });
            updateChart(processes);
        }

        async function blockProcess(pid) {
            await fetch('/block', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ pid })
            });
            fetchProcesses();
        }

        async function unblockProcess(pid) {
            await fetch('/unblock', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ pid })
            });
            fetchProcesses();
        }

        async function setLimit(pid) {
            const limit = document.getElementById(`limit${pid}`).value;
            await fetch('/limit', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ pid, percentage: limit })
            });
            fetchProcesses();
        }

        function updateChart(processes) {
            const labels = processes.map(p => p.name);
            const data = processes.map(p => p.traffic_usage);
            const ctx = document.getElementById('trafficChart').getContext('2d');
            new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: labels,
                    datasets: [{
                        label: 'Traffic Usage (bytes)',
                        data: data,
                        backgroundColor: 'rgba(75, 192, 192, 0.2)',
                        borderColor: 'rgba(75, 192, 192, 1)',
                        borderWidth: 1
                    }]
                },
                options: {
                    scales: {
                        y: {
                            beginAtZero: true
                        }
                    }
                }
            });
        }

        fetchProcesses();
        setInterval(fetchProcesses, 5000); // Refresh every 5 seconds
    </script>
</body>
</html>
''')

def elevate_permissions():
    print("Elevating permissions...")
    subprocess.call(['sudo', 'python', __file__])

if __name__ == '__main__':
    check_root()
    create_static_files()
    webbrowser.open('http://127.0.0.1:5000')
    app.run(debug=True)
