import os
import hmac
import hashlib
from flask import Flask, request, jsonify, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import pytz
import psycopg2
from psycopg2.extras import DictCursor
import requests
import json
import anthropic
import re

app = Flask(__name__)

# Configuration
TEXTBELT_API_KEY = os.environ.get('TEXTBELT_API_KEY')
TEXTBELT_URL = 'https://textbelt.com/text'
APP_URL = os.environ.get('APP_URL', 'https://your-app-name.herokuapp.com')
DATABASE_URL = os.environ['DATABASE_URL']
CLAUDE_API_KEY = os.environ.get('CLAUDE_API_KEY')

claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Create users table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                phone_number TEXT PRIMARY KEY,
                emergency_contact TEXT,
                last_response DATE
            )
        ''')
        
        # Create daily_goals table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS daily_goals (
                id SERIAL PRIMARY KEY,
                phone_number TEXT REFERENCES users(phone_number),
                date DATE,
                goals JSONB,
                completion_status JSONB
            )
        ''')
        
        # Check if the index exists before creating it
        cur.execute('''
            SELECT 1
            FROM pg_indexes
            WHERE indexname = 'idx_daily_goals_phone_date'
        ''')
        
        if cur.fetchone() is None:
            # Create the index if it doesn't exist
            cur.execute('''
                CREATE UNIQUE INDEX idx_daily_goals_phone_date 
                ON daily_goals (phone_number, date)
            ''')
            print("Created index idx_daily_goals_phone_date")
        else:
            print("Index idx_daily_goals_phone_date already exists")
        
        conn.commit()
        print("Database initialization completed successfully")
    except Exception as e:
        conn.rollback()
        print(f"Error during database initialization: {str(e)}")
    finally:
        cur.close()
        conn.close()

init_db()

def send_sms(to_number, message):
    payload = {
        'phone': to_number,
        'message': message,
        'key': TEXTBELT_API_KEY
    }
    try:
        response = requests.post('https://textbelt.com/text', data={
            'phone': to_number,
            'message': message,
            'key': TEXTBELT_API_KEY
        })
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error sending SMS: {str(e)}")
        return {"success": False, "error": str(e)}

def get_user_goals(phone_number, days=1):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute('''
        SELECT date, goals, completion_status 
        FROM daily_goals 
        WHERE phone_number = %s 
        AND date >= CURRENT_DATE - INTERVAL '%s days'
        ORDER BY date DESC
    ''', (phone_number, days))
    goals = cur.fetchall()
    cur.close()
    conn.close()
    return goals

def parse_goals(message):
    # Simple parsing: assumes goals are separated by newlines or commas
    goals = re.split(r'[,\n]', message)
    return [goal.strip() for goal in goals if goal.strip()]

def generate_daily_message(phone_number):
    yesterday_goals = get_user_goals(phone_number)
    
    if yesterday_goals:
        yesterday = yesterday_goals[0]
        goals_list = yesterday['goals']
        goals_summary = "\n".join(goals_list)
        prompt = f"""Human: You are an AI assistant for a goal-tracking SMS service. Yesterday's goals were:

{goals_summary}

Ask the user if they met these goals and what their goals are for today. Be brief and encouraging. Limit your response to 160 characters.

Assistant: """
    else:
        prompt = """Human: You are an AI assistant for a goal-tracking SMS service. This is the user's first day. Ask them what their goals are for today. Be brief and encouraging. Limit your response to 160 characters.

Assistant: """

    response = claude_client.completions.create(
        model="claude-2",
        prompt=prompt,
        max_tokens_to_sample=200,
        temperature=0.7
    )
    
    return response.completion.strip()

def process_user_response(phone_number, message):
    yesterday_goals = get_user_goals(phone_number)
    
    if yesterday_goals:
        yesterday = yesterday_goals[0]
        goals_list = yesterday['goals']
        goals_summary = "\n".join(goals_list)
    else:
        goals_summary = "No previous goals found."

    prompt = f"""Human: You are an AI assistant for a goal-tracking SMS service. The user's previous goals were:

{goals_summary}

The user's response is:

{message}

Analyze the response to determine:
1. Which goals were met (if any)
2. Any new goals mentioned
3. A brief, encouraging response to the user (max 160 characters)

Format your response as JSON with keys: "met_goals", "new_goals", and "response".

Assistant: """

    response = claude_client.completions.create(
        model="claude-2",
        prompt=prompt,
        max_tokens_to_sample=500,
        temperature=0.7
    )
    
    try:
        result = json.loads(response.completion.strip())
        return result['response']
    except json.JSONDecodeError:
        print(f"Error decoding JSON from Claude's response: {response.completion.strip()}")
        return {"met_goals": [], "new_goals": [], "response": "I couldn't process your response. Can you please rephrase it?"}
    

def update_user_goals(phone_number, met_goals, new_goals):
    today = datetime.now().date()
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        # Update completion status for yesterday's goals
        yesterday = today - timedelta(days=1)
        cur.execute("""
            UPDATE daily_goals 
            SET completion_status = daily_goals.completion_status || %s::jsonb
            WHERE phone_number = %s AND date = %s
        """, (json.dumps({goal: True for goal in met_goals}), phone_number, yesterday))

        # Insert new goals for today
        cur.execute("""
            INSERT INTO daily_goals (phone_number, date, goals, completion_status)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (phone_number, date) 
            DO UPDATE SET goals = daily_goals.goals || %s::jsonb
        """, (phone_number, today, json.dumps(new_goals), json.dumps({}), json.dumps(new_goals)))
        
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Error updating user goals: {str(e)}")
    finally:
        cur.close()
        conn.close()

def send_daily_message():
    print(f"send_daily_message started at {datetime.now()}")
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute('SELECT phone_number FROM users')
    users = cur.fetchall()
    cur.close()
    conn.close()

    for user in users:
        message = generate_daily_message(user['phone_number'])
        print(message)
        result = send_sms(user['phone_number'], message)
        print(f"SMS sent to {user['phone_number']}: {result}")

    print(f"send_daily_message completed at {datetime.now()}")

def store_user_response(phone_number, message):
    today = datetime.now().date()
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Check if there's an entry for today
    cur.execute("SELECT id FROM goals WHERE phone_number = %s AND date = %s", (phone_number, today))
    existing = cur.fetchone()
    
    if existing:
        # Update existing entry
        cur.execute("UPDATE goals SET goals = %s WHERE id = %s", (message, existing[0]))
    else:
        # Create new entry
        cur.execute("INSERT INTO goals (phone_number, date, goals) VALUES (%s, %s, %s)", (phone_number, today, message))
    
    conn.commit()
    cur.close()
    conn.close()

REGISTER_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Register for GoalMaster AI</title>
    <style>
        body {
            font-family: 'Arial', sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            color: #ffffff;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
        }
        .container {
            background: rgba(255, 255, 255, 0.1);
            padding: 2rem;
            border-radius: 10px;
            box-shadow: 0 8px 32px 0 rgba(31, 38, 135, 0.37);
            backdrop-filter: blur(4px);
            border: 1px solid rgba(255, 255, 255, 0.18);
            width: 90%;
            max-width: 400px;
        }
        h1 {
            text-align: center;
            color: #4cc9f0;
            margin-bottom: 1.5rem;
        }
        form {
            display: flex;
            flex-direction: column;
        }
        input {
            margin-bottom: 1rem;
            padding: 0.5rem;
            border: none;
            border-radius: 5px;
            background: rgba(255, 255, 255, 0.2);
            color: #ffffff;
        }
        input::placeholder {
            color: rgba(255, 255, 255, 0.7);
        }
        button {
            background: #4cc9f0;
            color: #1a1a2e;
            border: none;
            padding: 0.7rem;
            border-radius: 5px;
            cursor: pointer;
            font-weight: bold;
            transition: background 0.3s ease;
        }
        button:hover {
            background: #3a86ff;
        }
        .features {
            margin-top: 2rem;
            text-align: center;
        }
        .features h2 {
            color: #4cc9f0;
        }
        .features ul {
            list-style-type: none;
            padding: 0;
        }
        .features li {
            margin-bottom: 0.5rem;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Accountability AI</h1>
        <form method="POST">
            <input type="tel" name="phone" required placeholder="Your Phone Number...">
            <input type="tel" name="emergency_contact" required placeholder="Emergency Contact Number...">
            <button type="submit">Register</button>
        </form>
        <div class="features">
            <h2>First AI-Powered SMS Accountability App</h2>
            <ul>
                <li>- Daily AI-generated check-ins</li>
                <li>- Personalized goal tracking</li>
                <li>- Link to a friend for ensured-accountability</li>
                <li>- Seamless SMS integration</li>
            </ul>
        </div>
    </div>
</body>
</html>
"""

@app.route("/register", methods=['GET', 'POST'])
def register():
    if request.method == 'GET':
        return render_template_string(REGISTER_TEMPLATE)
    
    phone_number = request.form.get('phone')
    emergency_contact = request.form.get('emergency_contact')

    if not phone_number or not emergency_contact:
        return "Missing phone number or emergency contact", 400

    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        cur.execute('INSERT INTO users (phone_number, emergency_contact) VALUES (%s, %s)',
                    (phone_number, emergency_contact))
        conn.commit()
        send_sms(phone_number, "You've been registered for SMS Goal Tracker! We'll start tracking your goals tomorrow. Reply STOP to opt-out at any time.")
        return "Registration successful! You'll receive a confirmation SMS shortly.", 201
    except psycopg2.IntegrityError:
        conn.rollback()
        return "This phone number is already registered.", 400
    except Exception as e:
        conn.rollback()
        return f"An error occurred: {str(e)}", 500
    finally:
        cur.close()
        conn.close()

@app.route('/test_daily_message')
def test_daily_message():
    send_daily_message()
    return "Daily message sent", 200

@app.route("/sms_reply", methods=['POST'])
def sms_reply():
    data = request.json
    phone_number = data['fromNumber']
    message_body = data['text']

    try:
        result = process_user_response(phone_number, message_body)
        update_user_goals(phone_number, result['met_goals'], result['new_goals'])
    except Exception as e:
        print(f"Error processing SMS reply: {str(e)}")
        result = {
            "response": "I'm sorry, I encountered an error processing your message. Please try again later."
        }
    
    # Always attempt to send an SMS response
    sms_result = send_sms(phone_number, result['response'])
    print(f"SMS sent to {phone_number}: {sms_result}")

    return '', 204

scheduler = BackgroundScheduler()
scheduler.add_job(send_daily_message, 'cron', hour=8, minute=30, timezone='US/Eastern')
scheduler.start()

if __name__ == "__main__":
    app.run(debug=True)