from flask import Flask, request

app = Flask(__name__)

# Home page route
@app.route('/')
def home():
    return """
    <h1>Britton Method Automation System</h1>
    <p>Welcome! Your real estate automation is live 🚀</p>
    <a href='/run'>Run Automation</a>
    """

# Run automation route
@app.route('/run')
def run_automation():
    # Example placeholder: your automation will go here
    # For now, just return a message
    return "Automation running... (replace this with your scripts!)"

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)

