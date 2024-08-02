import os
from flask import Flask, request, jsonify, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
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
                goals TEXT[],
                completion_status BOOLEAN[]
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
        'key': TEXTBELT_API_KEY,
        'replyWebhookUrl': f"{APP_URL}/sms_reply"
    }
    try:
        response = requests.post(TEXTBELT_URL, data=payload)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error sending SMS: {str(e)}")
        return {"success": False, "error": str(e)}

def get_user_goals(phone_number, date):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute('''
        SELECT goals, completion_status 
        FROM daily_goals 
        WHERE phone_number = %s AND date = %s
    ''', (phone_number, date))
    goals = cur.fetchone()
    cur.close()
    conn.close()
    return goals

def store_user_goals(phone_number, date, goals):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO daily_goals (phone_number, date, goals, completion_status)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (phone_number, date) 
        DO UPDATE SET goals = EXCLUDED.goals
    ''', (phone_number, date, goals, [False] * len(goals)))
    conn.commit()
    cur.close()
    conn.close()

def update_goal_completion(phone_number, date, completion_status):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        UPDATE daily_goals
        SET completion_status = %s
        WHERE phone_number = %s AND date = %s
    ''', (completion_status, phone_number, date))
    conn.commit()
    cur.close()
    conn.close()

def send_morning_message():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute('SELECT phone_number FROM users')
    users = cur.fetchall()
    cur.close()
    conn.close()

    for user in users:
        message = "Good morning! What are your goals for today? Please separate them with commas."
        send_sms(user['phone_number'], message)

def send_evening_followup():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute('SELECT phone_number FROM users')
    users = cur.fetchall()
    cur.close()
    conn.close()

    yesterday = datetime.now().date() - timedelta(days=1)
    for user in users:
        yesterday_goals = get_user_goals(user['phone_number'], yesterday)
        if yesterday_goals and yesterday_goals['goals']:
            goals_list = ', '.join(yesterday_goals['goals'])
            message = f"Yesterday, your goals were: {goals_list}. Did you meet them? Please respond with Yes/No for each goal, separated by commas."
            send_sms(user['phone_number'], message)

def check_inactivity_and_notify():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    
    # Get users who haven't responded in the last 3 days
    three_days_ago = datetime.now().date() - timedelta(days=3)
    cur.execute('''
        SELECT u.phone_number, u.emergency_contact, u.last_response
        FROM users u
        WHERE u.last_response < %s OR u.last_response IS NULL
    ''', (three_days_ago,))
    
    inactive_users = cur.fetchall()
    
    for user in inactive_users:
        # Prepare message for emergency contact
        days_inactive = (datetime.now().date() - user['last_response']).days if user['last_response'] else 'several'
        message = f"Emergency Alert: {user['phone_number']} has been inactive for {days_inactive} days on their goal tracking app."
        
        # Send message to emergency contact
        send_sms(user['emergency_contact'], message)
        
        print(f"Sent inactivity alert for {user['phone_number']} to {user['emergency_contact']}")
    
    cur.close()
    conn.close()

@app.route("/sms_reply", methods=['POST'])
def sms_reply():
    data = request.json
    phone_number = data['fromNumber']
    message_body = data['text']

    today = datetime.now().date()
    yesterday = today - timedelta(days=1)

    # Check if this is a response to the morning message (setting goals)
    if "," in message_body and not any(word in message_body.lower() for word in ['yes', 'no']):
        goals = [goal.strip() for goal in message_body.split(',')]
        store_user_goals(phone_number, today, goals)
        response = "Thanks for sharing your goals. I've saved them and will check in with you later!"
    
    # Check if this is a response to the evening follow-up (goal completion)
    elif any(word in message_body.lower() for word in ['yes', 'no']):
        yesterday_goals = get_user_goals(phone_number, yesterday)
        if yesterday_goals:
            responses = [r.strip().lower() for r in message_body.split(',')]
            completion_status = [r == 'yes' for r in responses]
            update_goal_completion(phone_number, yesterday, completion_status)
            response = "Thanks for the update! Keep up the good work and let's focus on today's goals."
        else:
            response = "I'm sorry, I couldn't find your goals from yesterday. Let's focus on setting new goals for today!"
    
    else:
        response = "I'm sorry, I didn't understand your message. Please make sure to separate your goals with commas, or respond with Yes/No for each goal when asked about completion."

    send_sms(phone_number, response)

    # Update last_response date
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('UPDATE users SET last_response = %s WHERE phone_number = %s', (datetime.now().date(), phone_number))
    conn.commit()
    cur.close()
    conn.close()

    return '', 204

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
        send_sms(phone_number, "You've been registered for SMS Goal Tracker! We'll start tracking your goals tomorrow.")
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
    send_morning_message()
    return "Daily message sent", 200

scheduler = BackgroundScheduler()
scheduler.add_job(send_morning_message, 'cron', hour=8, minute=30, timezone='US/Eastern')
scheduler.add_job(send_evening_followup, 'cron', hour=20, minute=0, timezone='US/Eastern')
scheduler.add_job(check_inactivity_and_notify, 'cron', hour=9, minute=0, timezone='US/Eastern')
scheduler.start()

if __name__ == "__main__":
    app.run(debug=True)