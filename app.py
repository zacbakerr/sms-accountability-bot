import os
import hmac
import hashlib
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import pytz
import psycopg2
from psycopg2.extras import DictCursor
import requests
import json
import anthropic

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
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            phone_number TEXT PRIMARY KEY,
            emergency_contact TEXT,
            last_response DATE
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS goals (
            id SERIAL PRIMARY KEY,
            phone_number TEXT REFERENCES users(phone_number),
            date DATE,
            goals TEXT,
            completion_status TEXT
        )
    ''')
    conn.commit()
    cur.close()
    conn.close()

init_db()

def send_sms(to_number, message):
    payload = {
        'phone': to_number,
        'message': message,
        'key': TEXTBELT_API_KEY,
        'replyWebhookUrl': f"{APP_URL}/sms_reply",
        'sender': 'GoalTracker'
    }
    try:
        response = requests.post(TEXTBELT_URL, data=payload)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error sending SMS: {str(e)}")
        return {"success": False, "error": str(e)}

def get_user_goals(phone_number, days=7):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute('''
        SELECT date, goals, completion_status 
        FROM goals 
        WHERE phone_number = %s 
        AND date >= CURRENT_DATE - INTERVAL '%s days'
        ORDER BY date DESC
    ''', (phone_number, days))
    goals = cur.fetchall()
    cur.close()
    conn.close()
    return goals

def generate_claude_message(phone_number):
    goals = get_user_goals(phone_number)
    goals_history = "\n".join([f"Date: {goal['date']}, Goals: {goal['goals']}, Status: {goal['completion_status']}" for goal in goals])
    
    prompt = f"""\n\nHuman: You are an AI assistant for a goal-tracking SMS service. Your task is to engage with the user about their goals in a friendly, motivational manner. Here's the user's recent goal history:

{goals_history}

Based on this history, craft a message that:
1. Acknowledges their recent progress or challenges
2. Asks about their goals for today
3. Provides encouragement or advice based on their past performance
4. Asks if they completed yesterday's goals (if applicable)

Keep the message concise (maximum 160 characters) and conversational, as it will be sent via SMS.
"""

    response = claude_client.completions.create(
        model="claude-2",
        prompt=prompt,
        max_tokens_to_sample=200,
        temperature=0.7
    )
    
    return response.completion.strip()

def send_daily_message():
    print(f"send_daily_message started at {datetime.now()}")
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute('SELECT phone_number FROM users')
    users = cur.fetchall()
    cur.close()
    conn.close()

    for user in users:
        message = generate_claude_message(user['phone_number'])
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

@app.route('/test_daily_message')
def test_daily_message():
    send_daily_message()
    return "Daily message sent", 200

@app.route("/sms_reply", methods=['POST'])
def sms_reply():
    data = request.json
    phone_number = data['fromNumber']
    message_body = data['text']

    store_user_response(phone_number, message_body)

    response_message = generate_claude_message(phone_number)

    send_sms(phone_number, response_message)
    return '', 204

scheduler = BackgroundScheduler()
scheduler.add_job(send_daily_message, 'cron', hour=8, minute=30, timezone='US/Eastern')
scheduler.start()

if __name__ == "__main__":
    app.run(debug=True)