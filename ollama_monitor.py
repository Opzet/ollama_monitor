import requests
import time
import psutil
import json
import socket
import logging
import threading
import sqlite3
import os
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template, send_from_directory
from waitress import serve
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_socketio import SocketIO

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    filename='ollama_monitor.log'
)
logger = logging.getLogger('ollama_monitor')

# Configuration parameters
OLLAMA_HOST = "http://localhost:11434"
MONITOR_INTERVAL = 5  # Monitoring interval (seconds)
WEB_HOST = "0.0.0.0"
WEB_PORT = 8080
DB_FILE = "ollama_metrics.db"

class OllamaMetricsDB:
    def __init__(self, db_file=DB_FILE):
        """Initialize database connection"""
        self.db_file = db_file
        self._create_tables()
    
    def _create_tables(self):
        """Create necessary tables"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        
        # System metrics table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS system_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            server_status INTEGER,
            cpu_percent REAL,
            memory_percent REAL,
            disk_percent REAL,
            network_bytes_sent INTEGER,
            network_bytes_recv INTEGER,
            ollama_cpu_percent REAL,
            ollama_memory_percent REAL,
            ollama_connections INTEGER
        )
        ''')
        
        # Request logs table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS request_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            client_ip TEXT,
            model_name TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            response_time REAL,
            status_code INTEGER,
            endpoint TEXT
        )
        ''')
        
        # Models table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            model_name TEXT,
            model_size TEXT,
            parameter_size TEXT,
            modified_at TEXT,
            model_family TEXT
        )
        ''')
        
        # Create indexes for faster queries
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_request_logs_timestamp ON request_logs (timestamp)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_request_logs_client_ip ON request_logs (client_ip)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_request_logs_model_name ON request_logs (model_name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_system_metrics_timestamp ON system_metrics (timestamp)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_models_timestamp ON models (timestamp)')

        conn.commit()
        conn.close()
    
    def save_system_metrics(self, metrics):
        """Save system metrics"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()

        ollama_process = metrics.get('ollama_process', {})

        cursor.execute('''
        INSERT INTO system_metrics (
            timestamp, server_status, cpu_percent, memory_percent, 
            disk_percent, network_bytes_sent, network_bytes_recv,
            ollama_cpu_percent, ollama_memory_percent, ollama_connections
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            metrics['timestamp'],
            1 if metrics['server_status'] else 0,
            metrics['system']['cpu_percent'],
            metrics['system']['memory_percent'],
            metrics['system']['disk_percent'],
            metrics['system']['network_bytes_sent'],
            metrics['system']['network_bytes_recv'],
            ollama_process.get('cpu_percent', 0),
            ollama_process.get('memory_percent', 0),
            ollama_process.get('connections', 0)  # Ensure connections are saved
        ))

        conn.commit()
        conn.close()
    
    def save_models(self, timestamp, models):
        """Save model information"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        
        for model in models:
            details = model.get('details', {})
            cursor.execute('''
            INSERT INTO models (
                timestamp, model_name, model_size, parameter_size, 
                modified_at, model_family
            ) VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                timestamp,
                model.get('name', ''),
                str(model.get('size', 0)),
                details.get('parameter_size', ''),
                model.get('modified_at', ''),
                details.get('family', '')
            ))
        
        conn.commit()
        conn.close()
    
    def save_request_log(self, log_data):
        """Save request logs"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        
        cursor.execute('''
        INSERT INTO request_logs (
            timestamp, client_ip, model_name, input_tokens, 
            output_tokens, response_time, status_code, endpoint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            log_data['timestamp'],
            log_data['client_ip'],
            log_data['model_name'],
            log_data['input_tokens'],
            log_data['output_tokens'],
            log_data['response_time'],
            log_data['status_code'],
            log_data['endpoint']
        ))
        
        conn.commit()
        conn.close()
    
    def get_recent_system_metrics(self, hours=24):
        """Get recent system metrics"""
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute('''
        SELECT * FROM system_metrics
        WHERE timestamp > datetime('now', ?)
        ORDER BY timestamp
        ''', (f'-{hours} hours',))
        
        rows = cursor.fetchall()
        result = [dict(row) for row in rows]
        
        conn.close()
        return result
    
    def get_recent_requests(self, hours=24):
        """Get recent request logs"""
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute('''
        SELECT * FROM request_logs
        WHERE timestamp > datetime('now', ?)
        ORDER BY timestamp DESC
        ''', (f'-{hours} hours',))
        
        rows = cursor.fetchall()
        result = [dict(row) for row in rows]
        
        conn.close()
        return result
    
    def get_client_ip_stats(self, hours=24):
        """Get client IP statistics"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()

        cursor.execute('''
        SELECT client_ip, 
               COUNT(*) as request_count,
               MAX(timestamp) as last_request  -- Include last request timestamp
        FROM request_logs
        WHERE timestamp > datetime('now', ?)
        GROUP BY client_ip
        ORDER BY request_count DESC
        ''', (f'-{hours} hours',))

        result = cursor.fetchall()
        conn.close()
        return result
    
    def get_model_usage_stats(self, hours=24):
        """Get model usage statistics"""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()

        cursor.execute('''
        SELECT model_name, 
               COUNT(*) as request_count,
               SUM(input_tokens) as total_input_tokens,
               SUM(output_tokens) as total_output_tokens,
               AVG(response_time) as avg_response_time,
               MAX(timestamp) as last_request  -- Include last request timestamp
        FROM request_logs
        WHERE timestamp > datetime('now', ?)
        GROUP BY model_name
        ORDER BY request_count DESC
        ''', (f'-{hours} hours',))

        result = cursor.fetchall()
        conn.close()
        return result
    
    def get_latest_models(self):
        """Get the latest model list"""
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute('''
        SELECT * FROM models
        WHERE timestamp = (SELECT MAX(timestamp) FROM models)
        ''')
        
        rows = cursor.fetchall()
        result = [dict(row) for row in rows]
        
        conn.close()
        return result

class OllamaMonitor:
    def __init__(self, host=OLLAMA_HOST, interval=MONITOR_INTERVAL):
        """
        Initialize Ollama monitor
        
        Parameters:
            host: URL of the Ollama service
            interval: Check interval (seconds)
        """
        self.host = host
        self.interval = interval
        self.api_endpoint = f"{host}/api"
        self.db = OllamaMetricsDB()
        self.running = True
        self.default_model = None
    
    def get_models(self):
        """Get all available models"""
        try:
            response = requests.get(f"{self.api_endpoint}/tags")
            if response.status_code == 200:
                models = response.json().get('models', [])
                # Update default model
                if models and not self.default_model:
                    self.default_model = models[0].get('name')
                return models
            else:
                logger.error(f"Failed to get model list: {response.status_code}")
                return []
        except Exception as e:
            logger.error(f"Exception while getting model list: {str(e)}")
            return []
    
    def get_model_details(self, model_name):
        """Get model details"""
        try:
            data = {"model": model_name}
            response = requests.post(f"{self.api_endpoint}/show", json=data)
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Failed to get model details: {response.status_code}")
                return {}
        except Exception as e:
            logger.error(f"Exception while getting model details: {str(e)}")
            return {}
    
    def get_server_status(self):
        """Check server status"""
        try:
            # Use /api/tags endpoint to check server status, more reliable
            response = requests.get(f"{self.api_endpoint}/tags")
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Exception while checking server status: {str(e)}")
            return False
    
    def get_system_metrics(self):
        """Get system resource metrics"""
        metrics = {
            "cpu_percent": psutil.cpu_percent(interval=1),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_percent": psutil.disk_usage('/').percent,
        }
        
        # Get network usage data
        network = psutil.net_io_counters()
        metrics["network_bytes_sent"] = network.bytes_sent
        metrics["network_bytes_recv"] = network.bytes_recv
        
        return metrics
    
    def get_ollama_process_info(self):
        """Get resource usage of the Ollama process"""
        for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
            if 'ollama' in proc.info['name'].lower():
                try:
                    return {
                        "pid": proc.info['pid'],
                        "cpu_percent": proc.info['cpu_percent'],
                        "memory_percent": proc.info['memory_percent'],
                        "connections": len(proc.net_connections(kind='inet'))  # Use 'inet' for network connections
                    }
                except psutil.AccessDenied:
                    logger.warning(f"Access denied when retrieving connections for process {proc.info['pid']}")
                    return {
                        "pid": proc.info['pid'],
                        "cpu_percent": proc.info['cpu_percent'],
                        "memory_percent": proc.info['memory_percent'],
                        "connections": -1  # Default to 0 if access is denied
                    }
        return None
    
    def test_model_generation(self, model_name=None):
        """Test model generation capability"""
        if not model_name and self.default_model:
            model_name = self.default_model
        
        if not model_name:
            logger.warning("No available model to test generation capability")
            return None
        
        try:
            start_time = time.time()
            prompt = "Hello, world!"
            data = {
                "model": model_name,
                "prompt": prompt,
                "stream": False
            }
            response = requests.post(f"{self.api_endpoint}/generate", json=data)
            response_time = time.time() - start_time
            
            if response.status_code == 200:
                result = response.json()
                log_data = {
                    "timestamp": datetime.now().isoformat(),
                    "client_ip": "127.0.0.1",  # Internal test
                    "model_name": model_name,
                    "input_tokens": result.get('prompt_eval_count', 0),  # Ensure input tokens are extracted
                    "output_tokens": result.get('eval_count', 0),  # Ensure output tokens are extracted
                    "response_time": response_time,
                    "status_code": response.status_code,
                    "endpoint": "/api/generate"
                }
                self.db.save_request_log(log_data)
                return response_time
            else:
                logger.error(f"Model generation test failed: {response.status_code}")
                return None
        except Exception as e:
            logger.error(f"Exception during model generation test: {str(e)}")
            return None
    
    def run(self):
        """Run monitoring loop"""
        logger.info("Ollama monitoring service started")
        
        while self.running:
            try:
                timestamp = datetime.now().isoformat()
                
                # Check server status
                server_status = self.get_server_status()
                
                # Collect system metrics
                metrics = {
                    "timestamp": timestamp,
                    "server_status": server_status,
                    "system": self.get_system_metrics(),
                    "ollama_process": self.get_ollama_process_info() or {},
                }
                
                # Save system metrics
                self.db.save_system_metrics(metrics)
                
                # Emit real-time metrics
                socketio.emit('update_metrics', metrics)
                
                # Wait for the next interval
                time.sleep(self.interval)
            except Exception as e:
                logger.error(f"Exception in monitoring loop: {str(e)}")
                time.sleep(10)  # Short pause before retrying in case of error
    
    def stop(self):
        """Stop monitoring loop"""
        self.running = False

# Create Flask application
app = Flask(__name__, static_folder='static')
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Initialize SocketIO
socketio = SocketIO(app)

# Emit system metrics in real-time
def emit_system_metrics():
    db = OllamaMetricsDB()
    while True:
        metrics = db.get_recent_system_metrics(hours=1)  # Get the last hour of data
        if metrics:
            socketio.emit('update_metrics', metrics[-1])  # Send the latest data point
        socketio.sleep(5)  # Emit updates every 5 seconds

# Ensure static folder exists
if not os.path.exists('static'):
    os.makedirs('static')

# Create CSS and JS files
with open('static/style.css', 'w', encoding='utf-8') as f:
    f.write('''
body {
    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
    margin: 0;
    padding: 0;
    background-color: #f5f7fa;
    color: #333;
}

.container {
    max-width: 1200px;
    margin: 0 auto;
    padding: 20px;
}

header {
    background-color: #2c3e50;
    color: white;
    padding: 15px 0;
    margin-bottom: 20px;
}

header h1 {
    margin: 0;
    padding: 0 20px;
}

.card {
    background-color: white;
    border-radius: 8px;
    box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
    margin-bottom: 20px;
    padding: 20px;
}

.card h2 {
    margin-top: 0;
    color: #2c3e50;
    border-bottom: 1px solid #eee;
    padding-bottom: 10px;
}

.chart-container {
    height: 300px;
    margin-bottom: 20px;
}

table {
    width: 100%;
    border-collapse: collapse;
}

table th, table td {
    padding: 12px 15px;
    text-align: left;
    border-bottom: 1px solid #ddd;
}

table th {
    background-color: #f8f9fa;
    color: #2c3e50;
}

tr:hover {
    background-color: #f5f5f5;
}

.dashboard {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 20px;
    margin-bottom: 20px;
}

.stat-card {
    background-color: white;
    border-radius: 8px;
    box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
    padding: 20px;
    text-align: center;
}

.stat-value {
    font-size: 2rem;
    font-weight: bold;
    color: #3498db;
    margin: 10px 0;
}

.stat-title {
    color: #7f8c8d;
    font-size: 1rem;
}

.status-indicator {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    margin-right: 5px;
}

.status-up {
    background-color: #2ecc71;
}

.status-down {
    background-color: #e74c3c;
}

.nav-tabs {
    display: flex;
    border-bottom: 1px solid #ddd;
    margin-bottom: 20px;
}

.nav-tabs .tab {
    padding: 10px 15px;
    cursor: pointer;
    margin-right: 5px;
    border: 1px solid transparent;
    border-radius: 4px 4px 0 0;
}

.nav-tabs .tab.active {
    background-color: white;
    border: 1px solid #ddd;
    border-bottom-color: white;
    margin-bottom: -1px;
}

.tab-content > div {
    display: none;
}

.tab-content > div.active {
    display: block;
}

@media (max-width: 768px) {
    .dashboard {
        grid-template-columns: 1fr;
    }
    
    table {
        font-size: 0.9rem;
    }
    
    table th, table td {
        padding: 8px 10px;
    }
}
''')

with open('static/script.js', 'w') as f:
    f.write('''
// Run after the page loads
document.addEventListener('DOMContentLoaded', function() {
    // Initialize charts
    initCharts();
    
    // Set up tab switching
    setupTabs();
    
    // Set up auto-refresh
    setInterval(refreshData, 5000); // Refresh every 5 seconds
    
    // Initial data load
    refreshData();
});

// Set up tab switching
function setupTabs() {
    const tabs = document.querySelectorAll('.tab');
    const tabContents = document.querySelectorAll('.tab-content > div');
    
    tabs.forEach(tab => {
        tab.addEventListener('click', () => {
            // Remove all active classes
            tabs.forEach(t => t.classList.remove('active'));
            tabContents.forEach(tc => tc.classList.remove('active'));
            
            // Add active class to the current tab and content
            tab.classList.add('active');
            const target = tab.getAttribute('data-target');
            document.getElementById(target).classList.add('active');
            
            // If switching to the charts tab, redraw charts
            if (target === 'charts') {
                window.dispatchEvent(new Event('resize'));
            }
        });
    });
}

// Initialize all charts
function initCharts() {
    // CPU usage chart
    const cpuCtx = document.getElementById('cpuChart').getContext('2d');
    window.cpuChart = new Chart(cpuCtx, {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                label: 'System CPU Usage (%)',
                data: [],
                borderColor: '#3498db',
                backgroundColor: 'rgba(52, 152, 219, 0.1)',
                borderWidth: 2,
                pointRadius: 0,
                fill: true
            }, {
                label: 'Ollama CPU Usage (%)',
                data: [],
                borderColor: '#e74c3c',
                backgroundColor: 'rgba(231, 76, 60, 0.1)',
                borderWidth: 2,
                pointRadius: 0,
                fill: true
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                x: {
                    ticks: {
                        maxTicksLimit: 10
                    }
                },
                y: {
                    beginAtZero: true,
                    max: 100
                }
            },
            plugins: {
                tooltip: {
                    mode: 'index',
                    intersect: false
                },
                legend: {
                    position: 'top'
                }
            }
        }
    });
    
    // Memory usage chart
    const memoryCtx = document.getElementById('memoryChart').getContext('2d');
    window.memoryChart = new Chart(memoryCtx, {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                label: 'System Memory Usage (%)',
                data: [],
                borderColor: '#2ecc71',
                backgroundColor: 'rgba(46, 204, 113, 0.1)',
                borderWidth: 2,
                pointRadius: 0,
                fill: true
            }, {
                label: 'Ollama Memory Usage (%)',
                data: [],
                borderColor: '#9b59b6',
                backgroundColor: 'rgba(155, 89, 182, 0.1)',
                borderWidth: 2,
                pointRadius: 0,
                fill: true
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                x: {
                    ticks: {
                        maxTicksLimit: 10
                    }
                },
                y: {
                    beginAtZero: true,
                    max: 100
                }
            },
            plugins: {
                tooltip: {
                    mode: 'index',
                    intersect: false
                },
                legend: {
                    position: 'top'
                }
            }
        }
    });
    
    // Network traffic chart
    const networkCtx = document.getElementById('networkChart').getContext('2d');
    window.networkChart = new Chart(networkCtx, {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                label: 'Sent Traffic (MB)',
                data: [],
                borderColor: '#f39c12',
                backgroundColor: 'rgba(243, 156, 18, 0.1)',
                borderWidth: 2,
                pointRadius: 0,
                fill: true
            }, {
                label: 'Received Traffic (MB)',
                data: [],
                borderColor: '#16a085',
                backgroundColor: 'rgba(22, 160, 133, 0.1)',
                borderWidth: 2,
                pointRadius: 0,
                fill: true
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                x: {
                    ticks: {
                        maxTicksLimit: 10
                    }
                },
                y: {
                    beginAtZero: true
                }
            },
            plugins: {
                tooltip: {
                    mode: 'index',
                    intersect: false
                },
                legend: {
                    position: 'top'
                }
            }
        }
    });
    
    // Token usage chart
    const tokensCtx = document.getElementById('tokensChart').getContext('2d');
    window.tokensChart = new Chart(tokensCtx, {
        type: 'bar',
        data: {
            labels: [],
            datasets: [{
                label: 'Input Tokens',
                data: [],
                backgroundColor: 'rgba(52, 152, 219, 0.7)',
                borderColor: 'rgba(52, 152, 219, 1)',
                borderWidth: 1
            }, {
                label: 'Output Tokens',
                data: [],
                backgroundColor: 'rgba(46, 204, 113, 0.7)',
                borderColor: 'rgba(46, 204, 113, 1)',
                borderWidth: 1
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                x: {
                    stacked: false
                },
                y: {
                    beginAtZero: true
                }
            },
            plugins: {
                tooltip: {
                    mode: 'index',
                    intersect: false
                },
                legend: {
                    position: 'top'
                }
            }
        }
    });
}

// Refresh all data
function refreshData() {
    fetchSystemMetrics();
    fetchRequestStats();
    fetchModelStats();
    fetchIpStats();
    fetchLatestRequests();
    updateServerStatus();
}

// Fetch system metrics data
function fetchSystemMetrics() {
    fetch('/api/metrics/system')
        .then(response => response.json())
        .then(data => {
            updateSystemCharts(data);
            updateSystemStats(data);
        })
        .catch(error => console.error('Failed to fetch system metrics:', error));
}

// Fetch request statistics data
function fetchRequestStats() {
    fetch('/api/stats/requests')
        .then(response => response.json())
        .then(data => {
            document.getElementById('totalRequests').innerText = data.total_requests;
            document.getElementById('avgResponseTime').innerText = data.avg_response_time.toFixed(2) + 's';
            document.getElementById('totalInputTokens').innerText = data.total_input_tokens.toLocaleString();  // Update input tokens
            document.getElementById('totalOutputTokens').innerText = data.total_output_tokens.toLocaleString();  // Update output tokens
        })
        .catch(error => console.error('Failed to fetch request statistics:', error));
}

// Fetch model statistics data
function fetchModelStats() {
    fetch('/api/stats/models')
        .then(response => response.json())
        .then(data => {
            const tableBody = document.getElementById('modelStatsBody');
            tableBody.innerHTML = '';
            
            // Update token usage chart
            const labels = [];
            const inputTokens = [];
            const outputTokens = [];
            
            data.forEach(model => {
                // Add table row
                const row = document.createElement('tr');
                row.innerHTML = `
                    <td>${model.model_name}</td>
                    <td>${model.request_count}</td>
                    <td>${model.total_input_tokens.toLocaleString()}</td>
                    <td>${model.total_output_tokens.toLocaleString()}</td>
                    <td>${model.avg_response_time.toFixed(2)}s</td>
                `;
                tableBody.appendChild(row);
                
                // Update chart data
                labels.push(model.model_name);
                inputTokens.push(model.total_input_tokens);
                outputTokens.push(model.total_output_tokens);
            });
            
            // Update token chart
            window.tokensChart.data.labels = labels;
            window.tokensChart.data.datasets[0].data = inputTokens;
            window.tokensChart.data.datasets[1].data = outputTokens;
            window.tokensChart.update();
        })
        .catch(error => console.error('Failed to fetch model statistics:', error));
}

// Fetch IP statistics data
function fetchIpStats() {
    fetch('/api/stats/ips')
        .then(response => response.json())
        .then(data => {
            const tableBody = document.getElementById('ipStatsBody');
            tableBody.innerHTML = '';
            
            data.forEach(ip => {
                const row = document.createElement('tr');
                row.innerHTML = `
                    <td>${ip.client_ip}</td>
                    <td>${ip.request_count}</td>
                `;
                tableBody.appendChild(row);
            });
        })
        .catch(error => console.error('Failed to fetch IP statistics:', error));
}

// Fetch recent request logs
function fetchLatestRequests() {
    fetch('/api/logs/requests')
        .then(response => response.json())
        .then(data => {
            const tableBody = document.getElementById('requestLogsBody');
            tableBody.innerHTML = '';
            
            data.slice(0, 20).forEach(request => {
                const row = document.createElement('tr');
                row.innerHTML = `
                    <td>${request.timestamp}</td>
                    <td>${request.client_ip}</td>
                    <td>${request.model_name}</td>
                    <td>${request.input_tokens}</td>
                    <td>${request.output_tokens}</td>
                    <td>${request.response_time.toFixed(2)}s</td>
                    <td>${request.status_code}</td>
                    <td>${request.endpoint}</td>
                `;
                tableBody.appendChild(row);
            });
        })
        .catch(error => console.error('Failed to fetch request logs:', error));
}

// Update server status
function updateServerStatus() {
    fetch('/api/status')
        .then(response => response.json())
        .then(data => {
            const statusElement = document.getElementById('serverStatus');
            if (data.server_status) {
                statusElement.innerHTML = '<span class="status-indicator status-up"></span>Running';
                statusElement.style.color = '#2ecc71';
            } else {
                statusElement.innerHTML = '<span class="status-indicator status-down"></span>Stopped';
                statusElement.style.color = '#e74c3c';
            }
        })
        .catch(error => {
            console.error('Failed to fetch server status:', error);
            const statusElement = document.getElementById('serverStatus');
            statusElement.innerHTML = '<span class="status-indicator status-down"></span>Connection Failed';
            statusElement.style.color = '#e74c3c';
        });
}

// Update system charts
function updateSystemCharts(data) {
    // Keep only the most recent 24 hours of data points (assuming 1 data point per minute, max 1440 points)
    const maxDataPoints = 1440;
    
    // Extract recent data points
    const recentData = data.slice(-maxDataPoints);
    
    // Format time labels
    const timeLabels = recentData.map(d => {
        const date = new Date(d.timestamp);
        return date.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
    });
    
    // Extract CPU data
    const cpuData = recentData.map(d => d.cpu_percent);
    const ollamaCpuData = recentData.map(d => d.ollama_cpu_percent);
    
    // Extract memory data
    const memoryData = recentData.map(d => d.memory_percent);
    const ollamaMemoryData = recentData.map(d => d.ollama_memory_percent);
    
    // Extract network data and convert to MB
    const networkSentData = recentData.map(d => d.network_bytes_sent / (1024 * 1024));
    const networkRecvData = recentData.map(d => d.network_bytes_recv / (1024 * 1024));
    
    // Update CPU chart
    window.cpuChart.data.labels = timeLabels;
    window.cpuChart.data.datasets[0].data = cpuData;
    window.cpuChart.data.datasets[1].data = ollamaCpuData;
    window.cpuChart.update();
    
    // Update memory chart
    window.memoryChart.data.labels = timeLabels;
    window.memoryChart.data.datasets[0].data = memoryData;
    window.memoryChart.data.datasets[1].data = ollamaMemoryData;
    window.memoryChart.update();
    
    // Update network chart
    window.networkChart.data.labels = timeLabels;
    window.networkChart.data.datasets[0].data = networkSentData;
    window.networkChart.data.datasets[1].data = networkRecvData;
    window.networkChart.update();
}

// Update system statistics
function updateSystemStats(data) {
    if (data.length > 0) {
        const latest = data[data.length - 1];
        document.getElementById('cpuUsage').innerText = latest.cpu_percent.toFixed(1) + '%';
        document.getElementById('memoryUsage').innerText = latest.memory_percent.toFixed(1) + '%';
        document.getElementById('diskUsage').innerText = latest.disk_percent.toFixed(1) + '%';
        document.getElementById('ollamaConnections').innerText = latest.ollama_connections;  // Update Ollama connections
    }
}

const socket = io();

// Listen for real-time updates
socket.on('update_metrics', function(data) {
    updateSystemStats([data]); // Update the stats with the latest data
    updateSystemCharts([data]); // Update the charts with the latest data
});
''')
# Template rendering
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory('static', path)

# Create template file
@app.route('/templates/index.html')
def get_index_template():
    html = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LLM AI Monitoring System</title>
    <link rel="stylesheet" href="/static/style.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body>
    <header>
        <div class="container">
            <h1>LLM AI Monitoring System</h1>
        </div>
    </header>
    
    <div class="container">
        <div class="dashboard">
            <div class="stat-card">
                <div class="stat-title">Server Status</div>
                <div id="serverStatus" class="stat-value"><span class="status-indicator"></span>Checking...</div>
            </div>
            <div class="stat-card">
                <div class="stat-title">Total Requests</div>
                <div id="totalRequests" class="stat-value">0</div>
            </div>
            <div class="stat-card">
                <div class="stat-title">Average Response Time</div>
                <div id="avgResponseTime" class="stat-value">0s</div>
            </div>
            <div class="stat-card">
                <div class="stat-title">CPU Usage</div>
                <div id="cpuUsage" class="stat-value">0%</div>
            </div>
            <div class="stat-card">
                <div class="stat-title">Memory Usage</div>
                <div id="memoryUsage" class="stat-value">0%</div>
            </div>
            <div class="stat-card">
                <div class="stat-title">Disk Usage</div>
                <div id="diskUsage" class="stat-value">0%</</div>
            </div>
            <div class="stat-card">
                <div class="stat-title">Ollama Connections</div>
                <div id="ollamaConnections" class="stat-value">0</div>
            </div>
            <div class="stat-card">
                <div class="stat-title">Total Input Tokens</div>
                <div id="totalInputTokens" class="stat-value">0</div>
            </div>
            <div class="stat-card">
                <div class="stat-title">Total Output Tokens</div>
                <div id="totalOutputTokens" class="stat-value">0</div>
            </div>
        </div>
        
        <div class="nav-tabs">
            <div class="tab active" data-target="charts">Chart Monitoring</div>
            <div class="tab" data-target="models">Model Statistics</div>
            <div class="tab" data-target="clients">Client Statistics</div>
            <div class="tab" data-target="requests">Request Logs</div>
        </div>
        
        <div class="tab-content">
            <div id="charts" class="active">
                <div class="card">
                    <h2>CPU Usage</h2>
                    <div class="chart-container">
                        <canvas id="cpuChart"></canvas>
                    </div>
                </div>
                
                <div class="card">
                    <h2>Memory Usage</h2>
                    <div class="chart-container">
                        <canvas id="memoryChart"></canvas>
                    </div>
                </div>
                
                <div class="card">
                    <h2>Network Traffic</h2>
                    <div class="chart-container">
                        <canvas id="networkChart"></canvas>
                    </div>
                </div>
                
                <div class="card">
                    <h2>Token Usage</h2>
                    <div class="chart-container">
                        <canvas id="tokensChart"></canvas>
                    </div>
                </div>
            </div>
            
            <div id="models">
                <div class="card">
                    <h2>Model Usage Statistics</h2>
                    <table>
                        <thead>
                            <tr>
                                <th>Model Name</th>
                                <th>Request Count</th>
                                <th>Input Tokens</th>
                                <th>Output Tokens</th>
                                <th>Average Response Time</th>
                            </tr>
                        </thead>
                        <tbody id="modelStatsBody">
                            <tr>
                                <td colspan="5">Loading...</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
            
            <div id="clients">
                <div class="card">
                    <h2>Client IP Statistics</h2>
                    <table>
                        <thead>
                            <tr>
                                <th>Client IP</th>
                                <th>Request Count</th>
                            </tr>
                        </thead>
                        <tbody id="ipStatsBody">
                            <tr>
                                <td colspan="2">Loading...</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
            
            <div id="requests">
                <div class="card">
                    <h2>Recent Request Logs</h2>
                    <table>
                        <thead>
                            <tr>
                                <th>Time</th>
                                <th>Client IP</th>
                                <th>Model</th>
                                <th>Input Tokens</th>
                                <th>Output Tokens</th>
                                <th>Response Time</th>
                                <th>Status Code</th>
                                <th>Endpoint</th>
                            </tr>
                        </thead>
                        <tbody id="requestLogsBody">
                            <tr>
                                <td colspan="8">Loading...</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>
    
    <script src="/static/script.js"></script>
</body>
</html>
    '''
    with open('templates/index.html', 'w') as f:
        f.write(html)
    return html

# Ensure the templates folder exists
if not os.path.exists('templates'):
    os.makedirs('templates')
    get_index_template()

# API routes
@app.route('/api/status')
def api_status():
    monitor = app.config['MONITOR']
    return jsonify({
        "server_status": monitor.get_server_status(),
        "timestamp": datetime.now().isoformat()
    })

@app.route('/api/metrics/system')
def api_system_metrics():
    db = OllamaMetricsDB()
    hours = request.args.get('hours', 24, type=int)
    metrics = db.get_recent_system_metrics(hours)
    return jsonify(metrics)

@app.route('/api/logs/requests')
def api_request_logs():
    db = OllamaMetricsDB()
    hours = request.args.get('hours', 24, type=int)
    limit = request.args.get('limit', 100, type=int)  # Add limit for pagination
    offset = request.args.get('offset', 0, type=int)  # Add offset for pagination
    logs = db.get_recent_requests(hours)
    paginated_logs = logs[offset:offset + limit]  # Apply pagination
    return jsonify(paginated_logs)

@app.route('/api/stats/models')
def api_model_stats():
    db = OllamaMetricsDB()
    hours = request.args.get('hours', 24, type=int)
    stats = db.get_model_usage_stats(hours)
    result = []
    for row in stats:
        result.append({
            "model_name": row[0],
            "request_count": row[1],
            "total_input_tokens": row[2] or 0,
            "total_output_tokens": row[3] or 0,
            "avg_response_time": row[4] or 0,
            "last_request": row[5]  # Use the new last_request field
        })
    return jsonify(result)

@app.route('/api/stats/ips')
def api_ip_stats():
    db = OllamaMetricsDB()
    hours = request.args.get('hours', 24, type=int)
    stats = db.get_client_ip_stats(hours)
    result = []
    for row in stats:
        result.append({
            "client_ip": row[0],
            "request_count": row[1],
            "last_request": row[2]  # Use the new last_request field
        })
    return jsonify(result)

@app.route('/api/stats/requests')
def api_request_stats():
    db = OllamaMetricsDB()
    hours = request.args.get('hours', 24, type=int)
    logs = db.get_recent_requests(hours)

    total_requests = len(logs)
    total_input_tokens = sum(log['input_tokens'] for log in logs)
    total_output_tokens = sum(log['output_tokens'] for log in logs)

    # Calculate average response time
    response_times = [log['response_time'] for log in logs if log['response_time'] is not None]
    avg_response_time = sum(response_times) / len(response_times) if response_times else 0

    # Find the most active endpoint
    endpoint_counts = {}
    for log in logs:
        endpoint = log['endpoint']
        endpoint_counts[endpoint] = endpoint_counts.get(endpoint, 0) + 1
    most_active_endpoint = max(endpoint_counts, key=endpoint_counts.get, default=None)

    return jsonify({
        "total_requests": total_requests,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "avg_response_time": avg_response_time,
        "most_active_endpoint": most_active_endpoint
    })

# Ollama API proxy
@app.route('/ollama/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def proxy_ollama(path):
    db = OllamaMetricsDB()
    start_time = time.time()
    client_ip = request.remote_addr

    url = f"{OLLAMA_HOST}/{path}"
    headers = {key: value for (key, value) in request.headers if key != 'Host'}

    try:
        if request.method == 'GET':
            resp = requests.get(url, headers=headers, params=request.args, timeout=10)
        elif request.method == 'POST':
            json_data = request.get_json(silent=True)
            resp = requests.post(url, headers=headers, json=json_data, timeout=10)
        elif request.method == 'PUT':
            resp = requests.put(url, headers=headers, data=request.get_data(), timeout=10)
        elif request.method == 'DELETE':
            resp = requests.delete(url, headers=headers, timeout=10)
        else:
            return jsonify({"error": "Method not allowed"}), 405

        response_headers = [(name, value) for (name, value) in resp.raw.headers.items()]
        return resp.content, resp.status_code, response_headers
    except requests.exceptions.RequestException as e:
        logger.error(f"Proxy request exception: {str(e)}")
        return jsonify({"error": "Upstream API request failed", "details": str(e)}), 502
    except Exception as e:
        logger.error(f"Unexpected proxy error: {str(e)}")
        return jsonify({"error": "Unexpected error occurred", "details": str(e)}), 500

def run_monitor():
    """Run monitoring thread"""
    monitor = OllamaMonitor()
    threading.Thread(target=monitor.run, daemon=True).start()
    return monitor

def run_web_server(monitor):
    """Run web server"""
    app.config['MONITOR'] = monitor

    # Get the local machine's hostname and IP address
    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)

    # Log and print startup information
    startup_message = f"""
    ============================================
    LLM AI Monitoring System is starting...
    Hostname: {hostname}
    Local IP: {local_ip}
    Web Server: http://{WEB_HOST}:{WEB_PORT}
    ============================================
    """
    logger.info(startup_message.strip())
    print(startup_message.strip())

    # Start the web server
    logger.info(f"Web server is starting at http://{WEB_HOST}:{WEB_PORT}")
    serve(app, host=WEB_HOST, port=WEB_PORT, threads=10)

# Add system monitoring daemon functionality
def write_systemd_service():
    """Create system service file"""
    service_content = f'''[Unit]
Description=Ollama Monitor
After=network.target

[Service]
User={os.getlogin()}
WorkingDirectory={os.getcwd()}
ExecStart=/usr/bin/python3 {os.path.abspath(__file__)}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
'''
    
    service_path = '/tmp/ollama-monitor.service'
    with open(service_path, 'w') as f:
        f.write(service_content)
    
    print(f"System service file generated: {service_path}")
    print("To install this service, run as root:")
    print(f"sudo cp {service_path} /etc/systemd/system/")
    print("sudo systemctl daemon-reload")
    print("sudo systemctl enable ollama-monitor")
    print("sudo systemctl start ollama-monitor")

from threading import Thread

def start_realtime_updates():
    thread = Thread(target=emit_system_metrics)
    thread.daemon = True
    thread.start()

if __name__ == "__main__":
    # Start monitoring
    monitor = run_monitor()

    # Start real-time updates
    start_realtime_updates()

    # Start the web server
    run_web_server(monitor)